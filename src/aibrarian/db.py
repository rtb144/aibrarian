"""Postgres access: a tiny migration runner plus typed repository helpers.

Migrations are plain ``NNN_name.sql`` files shipped inside the package and
tracked in ``aib.schema_migrations``. They run automatically on service start
(``aibrarian migrate`` runs them explicitly).
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from importlib import resources
from typing import Any

import psycopg
from psycopg.rows import dict_row

from .config import Settings
from .models import MediaFile, Segment, Suggestion


def _json_default(o):
    """Make odd metadata values (e.g. pypdf IndirectObject, bytes, sets)
    JSON-safe instead of crashing a whole scan."""
    get = getattr(o, "get_object", None)
    if callable(get):
        try:
            r = get()
            if r is not o:
                return r
        except Exception:
            pass
    if isinstance(o, bytes):
        return o.decode("utf-8", "replace")
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    return str(o)


def _dumps(obj, **kw):
    kw.setdefault("default", _json_default)
    return json.dumps(obj, **kw)


MIGRATIONS_PKG = "aibrarian.migrations"


def list_migrations() -> list[tuple[str, str]]:
    """Return ``(version, sql)`` pairs sorted by filename."""
    files = resources.files(MIGRATIONS_PKG)
    out = [
        (f.name.removesuffix(".sql"), f.read_text(encoding="utf-8"))
        for f in files.iterdir()
        if f.name.endswith(".sql")
    ]
    return sorted(out)


def is_noop(source: str | None, target: str | None, op: str | None) -> bool:
    return op == "none" or not target or str(target) == str(source)


class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn

    @classmethod
    def from_settings(cls, s: Settings) -> Database:
        return cls(s.database_url)

    @contextmanager
    def conn(self) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self.dsn, row_factory=dict_row, autocommit=False) as c:
            yield c

    # -- migrations ---------------------------------------------------------
    def migrate(self) -> list[str]:
        applied: list[str] = []
        with self.conn() as c:
            c.execute("CREATE SCHEMA IF NOT EXISTS aib")
            c.execute(
                "CREATE TABLE IF NOT EXISTS aib.schema_migrations "
                "(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            c.execute("SELECT pg_advisory_xact_lock(724411)")
            done = {r["version"] for r in c.execute("SELECT version FROM aib.schema_migrations")}
            for version, sql in list_migrations():
                if version in done:
                    continue
                c.execute(sql)
                c.execute("INSERT INTO aib.schema_migrations(version) VALUES (%s)", (version,))
                applied.append(version)
            c.commit()
        return applied

    # -- media + state machine -------------------------------------------
    def upsert_media(self, files: Sequence[MediaFile], batch: int = 500) -> list[dict[str, Any]]:
        """Insert/refresh files. New files and files whose fingerprint changed are
        (re)set to ``discovered`` and get a history event. Returns
        ``[{id, path, created, reset}]``."""
        out: list[dict[str, Any]] = []
        if not files:
            return out
        sql = """
        INSERT INTO aib.media_file
            (path, root, kind, size_bytes, mtime, fingerprint, title, year, season, episode)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (path) DO UPDATE SET
            size_bytes=EXCLUDED.size_bytes, mtime=EXCLUDED.mtime,
            fingerprint=EXCLUDED.fingerprint, title=EXCLUDED.title, year=EXCLUDED.year,
            season=EXCLUDED.season, episode=EXCLUDED.episode, missing=FALSE, last_seen=now(),
            stage = CASE WHEN aib.media_file.fingerprint <> EXCLUDED.fingerprint
                         THEN 'discovered' ELSE aib.media_file.stage END,
            stage_attempts = CASE WHEN aib.media_file.fingerprint <> EXCLUDED.fingerprint
                         THEN 0 ELSE aib.media_file.stage_attempts END
        RETURNING id, path, (xmax = 0) AS created,
                  (SELECT m.fingerprint FROM aib.media_file m WHERE m.id=aib.media_file.id)
                    AS old_fp, fingerprint
        """
        with self.conn() as c:
            for i, f in enumerate(files, 1):
                row = c.execute(sql, (str(f.path), f.root, f.kind, f.size_bytes, f.mtime,
                                      f.fingerprint, f.title, f.year, f.season,
                                      f.episode)).fetchone()
                created = bool(row["created"])
                reset = (not created) and row["old_fp"] != row["fingerprint"]
                if created or reset:
                    c.execute(
                        "INSERT INTO aib.media_event (media_id, action, from_stage, to_stage,"
                        " outcome, started_at) VALUES (%s,%s,NULL,'discovered',%s,now())",
                        (row["id"], "discover", "ok" if created else "reset"))
                out.append({"id": row["id"], "path": row["path"],
                            "created": created, "reset": reset})
                if i % batch == 0:  # short transactions: never hold thousands of row locks
                    c.commit()
            c.commit()
        return out

    def known_files(self, root: str) -> dict[str, tuple[int, float, str]]:
        """path -> (size, mtime, fingerprint) for one root (one query)."""
        with self.conn() as c:
            rows = c.execute("SELECT path, size_bytes, mtime, fingerprint FROM aib.media_file "
                             "WHERE root=%s", (root,)).fetchall()
        return {r["path"]: (r["size_bytes"], r["mtime"], r["fingerprint"]) for r in rows}

    @contextmanager
    def try_lock(self, key: int) -> Iterator[bool]:
        """Session-level advisory lock; yields False if someone else holds it."""
        with psycopg.connect(self.dsn, autocommit=True) as c:
            got = c.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0]
            try:
                yield bool(got)
            finally:
                if got:
                    c.execute("SELECT pg_advisory_unlock(%s)", (key,))

    def mark_missing(self, root: str, present_paths: Sequence[str]) -> int:
        with self.conn() as c:
            cur = c.execute(
                "UPDATE aib.media_file SET missing=TRUE "
                "WHERE root=%s AND NOT missing AND NOT (path = ANY(%s))",
                (root, list(present_paths)),
            )
            c.commit()
            return cur.rowcount

    def get_media(self, media_id: int) -> dict[str, Any] | None:
        with self.conn() as c:
            return c.execute("SELECT * FROM aib.media_file WHERE id=%s", (media_id,)).fetchone()

    def find_media_by_title(self, title: str, limit: int = 10) -> list[dict[str, Any]]:
        """Trigram-ranked fuzzy title lookup (pg_trgm)."""
        with self.conn() as c:
            return c.execute(
                "SELECT *, similarity(title, %s) AS sim FROM aib.media_file "
                "WHERE NOT missing AND (title %% %s OR title ILIKE %s) "
                "ORDER BY sim DESC, title LIMIT %s",
                (title, title, f"%{title}%", limit),
            ).fetchall()

    def duplicate_candidates(self) -> list[dict[str, Any]]:
        with self.conn() as c:
            return c.execute(
                "SELECT id, path, size_bytes, mtime, title, year, duration_s, media_info, "
                "audio_layout, stage FROM aib.media_file WHERE NOT missing AND kind='video' "
                "ORDER BY id").fetchall()

    def get_file_hash(self, path: str) -> dict[str, Any] | None:
        with self.conn() as c:
            return c.execute("SELECT * FROM aib.file_hash WHERE path=%s", (path,)).fetchone()

    def put_file_hash(self, path: str, size: int, mtime: float, sample: str | None,
                      sha256: str | None) -> None:
        with self.conn() as c:
            c.execute(
                """INSERT INTO aib.file_hash (path, size_bytes, mtime, sample, sha256)
                   VALUES (%s,%s,%s,%s,%s) ON CONFLICT (path) DO UPDATE SET
                   size_bytes=EXCLUDED.size_bytes, mtime=EXCLUDED.mtime, sample=EXCLUDED.sample,
                   sha256=EXCLUDED.sha256, computed_at=now()""",
                (path, size, mtime, sample, sha256))
            c.commit()

    def searchable_titles(self) -> list[dict[str, Any]]:
        """Media that have transcript segments (what search/clip can actually hit)."""
        with self.conn() as c:
            return c.execute(
                "SELECT m.id AS media_id, coalesce(m.title, "
                "regexp_replace(m.path, '^.*/|\\.[^.]*$', '', 'g')) AS title, m.year "
                "FROM aib.media_file m WHERE NOT m.missing AND EXISTS "
                "(SELECT 1 FROM aib.segment s WHERE s.media_id = m.id) ORDER BY 2").fetchall()

    def get_media_by_path(self, path: str) -> dict[str, Any] | None:
        with self.conn() as c:
            return c.execute("SELECT * FROM aib.media_file WHERE path=%s", (path,)).fetchone()

    def list_media(self, stage: str | None = None, kind: str | None = None,
                   limit: int = 100, offset: int = 0,
                   audio_layout: str | None = None) -> list[dict[str, Any]]:
        with self.conn() as c:
            return c.execute(
                "SELECT id, path, root, kind, title, year, season, episode, stage, "
                "duration_s, jellyfin_id, audio_layout, audio_language FROM aib.media_file "
                "WHERE NOT missing "
                "AND (%s::text IS NULL OR stage=%s) AND (%s::text IS NULL OR kind=%s) "
                "AND (%s::text IS NULL OR audio_layout=%s) "
                "ORDER BY id LIMIT %s OFFSET %s",
                (stage, stage, kind, kind, audio_layout, audio_layout, limit, offset)).fetchall()

    def reprobe_candidates(self, limit: int, only_missing: bool = True,
                           after_id: int = 0) -> list[dict[str, Any]]:
        """Videos past ``discovered`` (any stage) needing fresh metadata, by id cursor."""
        with self.conn() as c:
            return c.execute(
                "SELECT * FROM aib.media_file WHERE kind='video' AND NOT missing "
                "AND stage <> 'discovered' AND (NOT %s OR media_info IS NULL) AND id > %s "
                "ORDER BY id LIMIT %s", (only_missing, after_id, limit)).fetchall()

    def record_event(self, media_id: int, action: str, stage: str, outcome: str,
                     started_at: Any, details: dict[str, Any] | None = None,
                     error: str | None = None, flow_run_id: str | None = None,
                     task_run_id: str | None = None) -> None:
        """Bookkeeping event that does not change the item's stage."""
        with self.conn() as c:
            c.execute(
                """INSERT INTO aib.media_event (media_id, action, from_stage, to_stage, outcome,
                       started_at, error, details, flow_run_id, task_run_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (media_id, action, stage, stage, outcome, started_at, error,
                 _dumps(details or {}), flow_run_id, task_run_id))
            c.commit()

    def quarantine_media(self, media_id: int, old_path: str, new_path: str, started_at: Any,
                         details: dict[str, Any]) -> None:
        """Repoint a quarantined copy, mark it missing + skipped, log a media_event."""
        with self.conn() as c:
            row = c.execute("SELECT stage FROM aib.media_file WHERE id=%s", (media_id,)).fetchone()
            prev = row["stage"] if row else None
            c.execute("UPDATE aib.media_file SET path=%s, missing=TRUE, stage='skipped', "
                      "last_error=%s, locked_by=NULL, stage_changed_at=now() WHERE id=%s",
                      (new_path, f"quarantined duplicate of {details.get('keeper')}", media_id))
            c.execute("""INSERT INTO aib.media_event (media_id, action, from_stage, to_stage,
                           outcome, started_at, details) VALUES (%s,'quarantine',%s,'skipped',
                           'ok',%s,%s)""",
                      (media_id, prev, started_at,
                       _dumps(details | {"from": old_path, "to": new_path})))
            c.commit()

    def restore_media(self, media_id: int, quarantine_path: str, path: str, stage: str,
                      started_at: Any) -> None:
        with self.conn() as c:
            c.execute("UPDATE aib.media_file SET path=%s, missing=FALSE, stage=%s, "
                      "last_error=NULL, stage_changed_at=now() WHERE id=%s AND path=%s",
                      (path, stage, media_id, quarantine_path))
            c.execute("""INSERT INTO aib.media_event (media_id, action, from_stage, to_stage,
                           outcome, started_at, details) VALUES (%s,'restore','skipped',%s,
                           'ok',%s,%s)""",
                      (media_id, stage, started_at,
                       _dumps({"from": quarantine_path, "to": path})))
            c.commit()

    def purge_media(self, media_id: int, path: str, started_at: Any,
                    details: dict[str, Any]) -> None:
        """A quarantined copy was permanently deleted: stage stays skipped."""
        with self.conn() as c:
            c.execute("UPDATE aib.media_file SET purged_at=now(), missing=TRUE, "
                      "last_error=%s WHERE id=%s AND path=%s",
                      (f"purged from quarantine ({details.get('age_days')} days)", media_id, path))
            c.execute("""INSERT INTO aib.media_event (media_id, action, from_stage, to_stage,
                           outcome, started_at, details) VALUES (%s,'purged','skipped','skipped',
                           'ok',%s,%s)""",
                      (media_id, started_at, _dumps(details | {"path": path})))
            c.commit()

    DEDUP_COLS = ("ts", "action", "group_no", "media_id", "keeper_path", "file_path",
                  "quarantine_path", "size_bytes", "sha256", "sidecars_copied", "reason",
                  "source")

    def record_dedup_action(self, row: dict[str, Any]) -> None:
        """Insert one aib.dedup_action row; duplicates (same action/path/ts) are ignored."""
        vals = [_dumps(row.get(k) or []) if k == "sidecars_copied" else row.get(k)
                for k in self.DEDUP_COLS]
        with self.conn() as c:
            c.execute(f"INSERT INTO aib.dedup_action ({', '.join(self.DEDUP_COLS)}) VALUES "
                      f"(COALESCE(%s, now()),{','.join(['%s'] * (len(vals) - 1))}) "
                      "ON CONFLICT DO NOTHING", vals)
            c.commit()

    def dedup_actions(self, action: str | None = None, since: Any = None, until: Any = None,
                      q: str | None = None, limit: int = 50,
                      offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        where = ("(%(a)s::text IS NULL OR action=%(a)s) AND (%(s)s::timestamptz IS NULL OR "
                 "ts>=%(s)s) AND (%(u)s::timestamptz IS NULL OR ts<%(u)s) AND (%(q)s::text "
                 "IS NULL OR file_path ILIKE %(ql)s OR keeper_path ILIKE %(ql)s)")
        p = {"a": action, "s": since, "u": until, "q": q, "ql": f"%{q}%",
             "lim": limit, "off": offset}
        with self.conn() as c:
            rows = c.execute(f"SELECT * FROM aib.dedup_action WHERE {where} "
                             "ORDER BY ts DESC, id DESC LIMIT %(lim)s OFFSET %(off)s",
                             p).fetchall()
            total = c.execute(f"SELECT count(*) AS n FROM aib.dedup_action WHERE {where}",
                              p).fetchone()["n"]
        return rows, total

    def dedup_totals(self) -> dict[str, dict[str, int]]:
        with self.conn() as c:
            rows = c.execute("SELECT action, count(*) AS n, COALESCE(sum(size_bytes),0) AS b "
                             "FROM aib.dedup_action GROUP BY action").fetchall()
        return {r["action"]: {"files": r["n"], "bytes": int(r["b"])} for r in rows}

    LIB_COLS = ("ts", "run_id", "action", "domain", "suggestion_id", "old_path", "new_path",
                "title", "author", "series", "series_index", "confidence", "reason", "source",
                "details")

    def record_library_action(self, row: dict[str, Any]) -> int:
        vals = [_dumps(row.get(k) or {}, default=str) if k == "details" else row.get(k)
                for k in self.LIB_COLS]
        vals[3] = vals[3] or "book"
        with self.conn() as c:
            r = c.execute(f"INSERT INTO aib.library_action ({', '.join(self.LIB_COLS)}) VALUES "
                          f"(COALESCE(%s, now()),{','.join(['%s'] * (len(vals) - 1))}) "
                          "RETURNING id", vals).fetchone()
            c.commit()
        return r["id"]

    def get_library_actions(self, ids: Sequence[int] | None = None,
                            run_id: str | None = None) -> list[dict[str, Any]]:
        with self.conn() as c:
            return c.execute("SELECT * FROM aib.library_action WHERE (id = ANY(%s) OR run_id=%s)"
                             " ORDER BY id DESC", (list(ids or []), run_id)).fetchall()

    def mark_library_undone(self, action_id: int, undo_id: int) -> None:
        with self.conn() as c:
            c.execute("UPDATE aib.library_action SET undone_by=%s WHERE id=%s",
                      (undo_id, action_id))
            c.commit()

    _LIB_FILTERS = ("(%(a)s::text IS NULL OR action=%(a)s) AND "
                    "(%(d)s::text IS NULL OR domain=%(d)s) AND "
                    "(%(s)s::timestamptz IS NULL OR ts>=%(s)s) AND "
                    "(%(u)s::timestamptz IS NULL OR ts<%(u)s) AND "
                    "(%(t)s::text IS NULL OR title ILIKE %(tl)s OR old_path ILIKE %(tl)s "
                    "OR new_path ILIKE %(tl)s) AND "
                    "(%(au)s::text IS NULL OR author ILIKE %(aul)s) AND "
                    "(%(se)s::text IS NULL OR series ILIKE %(sel)s) AND "
                    "(%(r)s::text IS NULL OR run_id=%(r)s)")

    def library_actions(self, action: str | None = None, domain: str | None = None,
                        since: Any = None, until: Any = None, title: str | None = None,
                        author: str | None = None, series: str | None = None,
                        run_id: str | None = None, limit: int = 50,
                        offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        p = {"a": action, "d": domain, "s": since, "u": until, "t": title, "tl": f"%{title}%",
             "au": author, "aul": f"%{author}%", "se": series, "sel": f"%{series}%",
             "r": run_id, "lim": limit, "off": offset}
        with self.conn() as c:
            rows = c.execute(f"SELECT * FROM aib.library_action WHERE {self._LIB_FILTERS} "
                             "ORDER BY ts DESC, id DESC LIMIT %(lim)s OFFSET %(off)s",
                             p).fetchall()
            total = c.execute(f"SELECT count(*) AS n FROM aib.library_action WHERE "
                              f"{self._LIB_FILTERS}", p).fetchone()["n"]
        return rows, total

    def library_action_counts(self, since: Any = None) -> dict[str, int]:
        with self.conn() as c:
            rows = c.execute("SELECT action, count(*) AS n FROM aib.library_action WHERE "
                             "(%s::timestamptz IS NULL OR ts>=%s) GROUP BY action",
                             (since, since)).fetchall()
        return {r["action"]: r["n"] for r in rows}

    def library_facet(self, field: str, q: str = "", limit: int = 20) -> list[str]:
        if field not in ("title", "author", "series"):
            raise ValueError(field)
        with self.conn() as c:
            rows = c.execute(f"SELECT {field} AS v, count(*) AS n FROM aib.library_action "
                             f"WHERE {field} ILIKE %s GROUP BY {field} ORDER BY n DESC, v "
                             "LIMIT %s", (f"%{q}%", limit)).fetchall()
        return [r["v"] for r in rows]

    def audio_layout_counts(self) -> dict[str, int]:
        with self.conn() as c:
            rows = c.execute("SELECT coalesce(audio_layout, 'unknown') AS l, count(*) AS n "
                             "FROM aib.media_file WHERE NOT missing AND kind='video' "
                             "GROUP BY 1 ORDER BY 2 DESC").fetchall()
        return {r["l"]: r["n"] for r in rows}

    def update_path(self, old: str, new: str) -> int | None:
        """Repoint the catalog row (segments/transcripts hang off its id). Returns id."""
        with self.conn() as c:
            row = c.execute("UPDATE aib.media_file SET path=%s WHERE path=%s RETURNING id",
                            (new, old)).fetchone()
            c.commit()
        return row["id"] if row else None

    def update_media_fields(self, media_id: int, **fields: Any) -> None:
        allowed = {"duration_s", "probe", "audio_path", "jellyfin_id", "title", "year",
                   "media_info", "audio_layout", "audio_stream_index", "audio_language",
                   "probed_at"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"cannot update {bad}")
        if not fields:
            return
        cols = ", ".join(f"{k}=%s" for k in fields)
        vals = [_dumps(v) if k in ("probe", "media_info") else v for k, v in fields.items()]
        with self.conn() as c:
            c.execute(f"UPDATE aib.media_file SET {cols} WHERE id=%s", (*vals, media_id))
            c.commit()

    def claim(self, stage: str, roots: Sequence[str] | None, limit: int,
              worker: str, lease_minutes: int = 360) -> list[dict[str, Any]]:
        """Lease up to ``limit`` items sitting in ``stage``. Uses SKIP LOCKED so
        concurrent workers never get the same item; stale leases expire."""
        sql = """
        UPDATE aib.media_file SET locked_by=%s, locked_at=now()
        WHERE id IN (
            SELECT id FROM aib.media_file
            WHERE stage=%s AND NOT missing AND kind='video'
              AND (%s::text[] IS NULL OR root = ANY(%s::text[]))
              AND (locked_at IS NULL OR locked_at < now() - make_interval(mins => %s))
            ORDER BY stage_changed_at LIMIT %s
            FOR UPDATE SKIP LOCKED)
        RETURNING *
        """
        r = list(roots) if roots else None
        with self.conn() as c:
            rows = c.execute(sql, (worker, stage, r, r, lease_minutes, limit)).fetchall()
            c.commit()
        return rows

    def transition(self, media_id: int, action: str, from_stage: str, to_stage: str,
                   outcome: str, started_at: Any, *, attempt: int = 1,
                   error: str | None = None, details: dict[str, Any] | None = None,
                   flow_run_id: str | None = None, task_run_id: str | None = None) -> bool:
        """Atomically move an item and append a history event. Guarded by the
        expected ``from_stage`` so racing workers cannot double-apply. Returns
        False if the item was no longer in ``from_stage``."""
        with self.conn() as c:
            cur = c.execute(
                """UPDATE aib.media_file SET stage=%s, stage_changed_at=now(),
                       stage_attempts = CASE WHEN %s='ok' THEN 0 ELSE stage_attempts END,
                       last_error=%s,
                       failed_stage = CASE WHEN %s='failed' THEN %s ELSE NULL END,
                       locked_by=NULL, locked_at=NULL
                   WHERE id=%s AND stage=%s""",
                (to_stage, outcome, error, to_stage, from_stage, media_id, from_stage))
            if cur.rowcount == 0:
                c.rollback()
                return False
            c.execute(
                """INSERT INTO aib.media_event (media_id, action, from_stage, to_stage, outcome,
                       attempt, started_at, error, details, flow_run_id, task_run_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (media_id, action, from_stage, to_stage, outcome, attempt, started_at,
                 error[:4000] if error else None, _dumps(details or {}),
                 flow_run_id, task_run_id))
            c.commit()
        return True

    def record_attempt_failure(self, media_id: int, action: str, stage: str, started_at: Any,
                               error: str, attempt: int, flow_run_id: str | None = None,
                               task_run_id: str | None = None) -> None:
        """Log a failed attempt that will be retried (stage unchanged)."""
        with self.conn() as c:
            c.execute("UPDATE aib.media_file SET stage_attempts=stage_attempts+1, last_error=%s,"
                      " locked_by=NULL, locked_at=NULL WHERE id=%s", (error[:4000], media_id))
            c.execute(
                """INSERT INTO aib.media_event (media_id, action, from_stage, to_stage, outcome,
                       attempt, started_at, error, flow_run_id, task_run_id)
                   VALUES (%s,%s,%s,%s,'failed',%s,%s,%s,%s,%s)""",
                (media_id, action, stage, stage, attempt, started_at, error[:4000],
                 flow_run_id, task_run_id))
            c.commit()

    def retry_failed(self, media_id: int | None = None) -> int:
        """Move failed items back to the stage that failed (all, or one)."""
        with self.conn() as c:
            rows = c.execute(
                "SELECT id, failed_stage FROM aib.media_file WHERE stage='failed' "
                "AND failed_stage IS NOT NULL AND (%s::bigint IS NULL OR id=%s)",
                (media_id, media_id)).fetchall()
            for r in rows:
                c.execute("UPDATE aib.media_file SET stage=%s, stage_attempts=0, "
                          "stage_changed_at=now() WHERE id=%s", (r["failed_stage"], r["id"]))
                c.execute("INSERT INTO aib.media_event (media_id, action, from_stage, to_stage,"
                          " outcome, started_at) VALUES (%s,'retry','failed',%s,'reset',now())",
                          (r["id"], r["failed_stage"]))
            c.commit()
        return len(rows)

    # -- transcripts --------------------------------------------------------
    def save_segments(self, media_id: int, segments: Sequence[Segment], fingerprint: str,
                      language: str | None, model: str, duration_s: float | None) -> list[int]:
        with self.conn() as c:
            c.execute("DELETE FROM aib.segment WHERE media_id=%s", (media_id,))
            ids: list[int] = []
            for seg in segments:
                row = c.execute(
                    "INSERT INTO aib.segment (media_id, seq, start_s, end_s, text, words) "
                    "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                    (media_id, seg.seq, seg.start, seg.end, seg.text,
                     _dumps(seg.words) if seg.words is not None else None),
                ).fetchone()
                ids.append(row["id"])
            c.execute(
                """INSERT INTO aib.transcript (media_id, fingerprint, language, model, duration_s)
                   VALUES (%s,%s,%s,%s,%s) ON CONFLICT (media_id) DO UPDATE SET
                   fingerprint=EXCLUDED.fingerprint, language=EXCLUDED.language,
                   model=EXCLUDED.model, duration_s=EXCLUDED.duration_s, updated_at=now()""",
                (media_id, fingerprint, language, model, duration_s))
            c.commit()
        return ids

    def segments_by_ids(self, ids: Sequence[int]) -> list[dict[str, Any]]:
        with self.conn() as c:
            return c.execute(
                "SELECT s.*, m.path, m.title FROM aib.segment s "
                "JOIN aib.media_file m ON m.id=s.media_id WHERE s.id = ANY(%s)",
                (list(ids),),
            ).fetchall()

    def embed_eval_candidates(self, root: str, min_segments: int = 40) -> list[dict[str, Any]]:
        """Transcribed media of one root (read-only; embedding A/B harness)."""
        with self.conn() as c:
            return c.execute(
                "SELECT m.id, m.title, m.duration_s, count(s.id) AS segments "
                "FROM aib.media_file m JOIN aib.segment s ON s.media_id = m.id "
                "WHERE m.root = %s AND NOT m.missing GROUP BY m.id "
                "HAVING count(s.id) >= %s ORDER BY m.id", (root, min_segments)).fetchall()

    def segments_for_media(self, media_id: int) -> list[dict[str, Any]]:
        with self.conn() as c:
            return c.execute(
                "SELECT * FROM aib.segment WHERE media_id=%s ORDER BY seq", (media_id,)
            ).fetchall()

    # -- transcript language check (aib.transcript_lang) ------------------
    def transcribed_media_for_langcheck(self) -> list[dict[str, Any]]:
        with self.conn() as c:
            return c.execute(
                "SELECT m.id, m.path, m.title, t.language FROM aib.media_file m "
                "JOIN aib.transcript t ON t.media_id = m.id WHERE NOT m.missing").fetchall()

    def save_langcheck(self, rows: Sequence[dict[str, Any]]) -> int:
        """Upsert results; a previous manual review is kept."""
        cols = ("path", "media_id", "title", "source", "detected_lang", "confidence",
                "stored_lang", "mismatch", "non_english", "repetitive", "repeat_ratio",
                "top_line_share", "segments", "flags", "needs_review", "snippet")
        sql = (f"INSERT INTO aib.transcript_lang ({', '.join(cols)}, checked_at) VALUES "
               f"({', '.join(['%s'] * len(cols))}, now()) ON CONFLICT (path) DO UPDATE SET "
               + ", ".join(f"{k}=EXCLUDED.{k}" for k in cols[1:]) + ", checked_at=now()")
        with self.conn() as c:
            with c.cursor() as cur:
                cur.executemany(sql, [tuple(
                    bool(r.get(k)) if k in ("mismatch", "non_english", "repetitive",
                                            "needs_review")
                    else list(r.get(k) or []) if k == "flags" else r.get(k) for k in cols)
                    for r in rows])
            c.commit()
        return len(rows)

    def langcheck_list(self, flagged: bool = True, non_english: bool = False,
                       review: str | None = None, q: str | None = None,
                       limit: int = 200, offset: int = 0) -> dict[str, Any]:
        where, args = [], []
        if flagged:
            where.append("needs_review")
        if non_english:
            where.append("non_english")
        if review == "none":
            where.append("review IS NULL")
        elif review:
            where.append("review = %s")
            args.append(review)
        if q:
            where.append("(title ILIKE %s OR path ILIKE %s)")
            args += [f"%{q}%", f"%{q}%"]
        w = ("WHERE " + " AND ".join(where)) if where else ""
        with self.conn() as c:
            total = c.execute(f"SELECT count(*) AS n FROM aib.transcript_lang {w}",
                              args).fetchone()["n"]
            items = c.execute(
                f"SELECT * FROM aib.transcript_lang {w} ORDER BY review NULLS FIRST, "
                "detected_lang, title LIMIT %s OFFSET %s", [*args, limit, offset]).fetchall()
            counts = c.execute(
                "SELECT count(*) AS total, count(*) FILTER (WHERE needs_review) AS needs_review, "
                "count(*) FILTER (WHERE non_english) AS non_english, "
                "count(*) FILTER (WHERE review='correct') AS correct, "
                "count(*) FILTER (WHERE review='wrong') AS wrong, max(checked_at) AS checked_at "
                "FROM aib.transcript_lang").fetchone()
        return {"total": total, "counts": counts, "items": items}

    def langcheck_requeue_candidates(self) -> list[dict[str, Any]]:
        """non_english / mismatch rows not reviewed 'correct', with their media row."""
        with self.conn() as c:
            return c.execute(
                "SELECT l.id AS lang_id, l.path AS transcript_path, l.title AS lang_title, "
                "l.detected_lang, l.stored_lang, l.review, m.id AS media_id, m.path, "
                "COALESCE(m.title, l.title) AS title, m.stage, m.media_info, "
                "m.audio_stream_index, m.audio_language, m.missing "
                "FROM aib.transcript_lang l LEFT JOIN aib.media_file m ON m.id = l.media_id "
                "WHERE (l.non_english OR l.mismatch) AND l.review IS DISTINCT FROM 'correct' "
                "ORDER BY COALESCE(m.title, l.title)").fetchall()

    def langcheck_review(self, item_id: int, review: str | None,
                         note: str | None = None) -> dict[str, Any] | None:
        with self.conn() as c:
            row = c.execute(
                "UPDATE aib.transcript_lang SET review=%s, review_note=%s, "
                "reviewed_at=CASE WHEN %s::text IS NULL THEN NULL ELSE now() END "
                "WHERE id=%s RETURNING *", (review, note, review, item_id)).fetchone()
            c.commit()
        return row

    def keyword_search(self, query: str, limit: int = 20,
                       media_id: int | None = None) -> list[dict[str, Any]]:
        """Trigram fuzzy + full-text search over segments (old catalog_db.search_text)."""
        with self.conn() as c:
            return c.execute(
                """SELECT s.id, s.media_id, s.seq, s.start_s, s.end_s, s.text, m.path, m.title,
                          GREATEST(similarity(s.text, %(q)s),
                                   ts_rank(s.tsv, plainto_tsquery('simple', %(q)s))) AS score
                   FROM aib.segment s JOIN aib.media_file m ON m.id = s.media_id
                   WHERE (s.text %% %(q)s OR s.text ILIKE '%%' || %(q)s || '%%'
                          OR s.tsv @@ plainto_tsquery('simple', %(q)s))
                     AND (%(mid)s::bigint IS NULL OR s.media_id = %(mid)s)
                   ORDER BY score DESC LIMIT %(n)s""",
                {"q": query, "mid": media_id, "n": limit}).fetchall()

    # -- suggestions --------------------------------------------------------
    def save_suggestion(self, s: Suggestion) -> int:
        """Upsert; a no-op (nothing to move) is stored with status 'noop', op 'none'."""
        noop = is_noop(s.source_path, s.target_path, s.op)
        op, status = ("none", "noop") if noop else (s.op, "open")
        with self.conn() as c:
            row = c.execute(
                """INSERT INTO aib.suggestion
                   (domain, source_path, target_path, metadata, confidence, decision, rationale,
                    media_kind, op, run_id, status)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (domain, source_path) DO UPDATE SET
                     target_path=EXCLUDED.target_path, metadata=EXCLUDED.metadata,
                     media_kind=EXCLUDED.media_kind, op=EXCLUDED.op, run_id=EXCLUDED.run_id,
                     confidence=EXCLUDED.confidence, decision=EXCLUDED.decision,
                     rationale=EXCLUDED.rationale, status=EXCLUDED.status, created_at=now()
                   RETURNING id""",
                (s.domain, s.source_path, s.target_path, _dumps(s.metadata),
                 s.confidence, s.decision, s.rationale, s.media_kind, op, s.run_id, status),
            ).fetchone()
            c.commit()
            return row["id"]

    def list_suggestions(self, domain: str | None, status: str = "open",
                         include_noop: bool = False) -> list[dict[str, Any]]:
        """Suggestions with ``status``; no-ops (status 'noop' or target == source)
        are hidden unless ``include_noop``."""
        statuses = [status] + (["noop"] if include_noop and status == "open" else [])
        with self.conn() as c:
            return c.execute(
                "SELECT * FROM aib.suggestion WHERE status = ANY(%s) "
                "AND (%s::text IS NULL OR domain=%s) "
                "AND (%s OR (op <> 'none' AND target_path IS NOT NULL "
                "AND target_path <> source_path)) ORDER BY created_at DESC",
                (statuses, domain, domain, include_noop)).fetchall()

    def get_suggestion(self, sid: int) -> dict[str, Any] | None:
        with self.conn() as c:
            return c.execute("SELECT * FROM aib.suggestion WHERE id=%s", (sid,)).fetchone()

    def set_suggestion_status(self, sid: int, status: str) -> None:
        with self.conn() as c:
            c.execute("UPDATE aib.suggestion SET status=%s WHERE id=%s", (status, sid))
            c.commit()

    def suggestion_sources(self, domain: str, include_open: bool = True) -> set[str]:
        """Source paths already handled - used to resume interrupted scans."""
        statuses = ["applied", "dismissed", "noop", "needs_review", "quarantined",
                    "undone"] + (["open"] if include_open else [])
        with self.conn() as c:
            rows = c.execute("SELECT source_path FROM aib.suggestion WHERE domain=%s "
                             "AND status = ANY(%s)", (domain, statuses)).fetchall()
        return {r["source_path"] for r in rows}

    def start_run(self, domain: str, params: dict[str, Any],
                  flow_run_id: str | None = None) -> int:
        with self.conn() as c:
            row = c.execute("INSERT INTO aib.scan_run (domain, params, flow_run_id) "
                            "VALUES (%s,%s,%s) RETURNING id",
                            (domain, _dumps(params), flow_run_id)).fetchone()
            c.commit()
            return row["id"]

    def finish_run(self, run_id: int, summary: dict[str, Any]) -> None:
        with self.conn() as c:
            c.execute("UPDATE aib.scan_run SET finished_at=now(), summary=%s WHERE id=%s",
                      (_dumps(summary), run_id))
            c.commit()

    def latest_run(self, domain: str) -> dict[str, Any] | None:
        with self.conn() as c:
            run = c.execute("SELECT * FROM aib.scan_run WHERE domain=%s ORDER BY id DESC "
                            "LIMIT 1", (domain,)).fetchone()
            if run:
                run["items"] = c.execute("SELECT * FROM aib.suggestion WHERE run_id=%s "
                                         "ORDER BY id", (run["id"],)).fetchall()
            return run

    def record_file_op(self, op: str, src: str, dst: str | None, result: str,
                       error: str | None = None, suggestion_id: int | None = None) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO aib.file_op (suggestion_id, op, src, dst, result, error) "
                      "VALUES (%s,%s,%s,%s,%s,%s)", (suggestion_id, op, src, dst, result, error))
            c.commit()

    def file_ops(self, result: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        with self.conn() as c:
            return c.execute("SELECT * FROM aib.file_op WHERE (%s::text IS NULL OR result=%s) "
                             "ORDER BY id DESC LIMIT %s", (result, result, limit)).fetchall()

    def record_clip(self, media_id: int, quote: str, start: float, end: float, out: str) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO aib.clip (media_id, quote, start_s, end_s, output_path) "
                "VALUES (%s,%s,%s,%s,%s)", (media_id, quote, start, end, out))
            c.commit()

    # -- status / observability -------------------------------------------
    def stage_counts(self) -> dict[str, int]:
        with self.conn() as c:
            rows = c.execute("SELECT stage, count(*) AS n FROM aib.media_file "
                             "WHERE NOT missing AND kind='video' GROUP BY stage").fetchall()
        return {r["stage"]: r["n"] for r in rows}

    def list_by_stage(self, stage: str, limit: int = 100, offset: int = 0) -> list[dict]:
        with self.conn() as c:
            return c.execute(
                "SELECT id, path, title, stage, failed_stage, stage_attempts, last_error, "
                "stage_changed_at, audio_layout, audio_language FROM aib.media_file "
                "WHERE stage=%s AND NOT missing "
                "ORDER BY stage_changed_at DESC LIMIT %s OFFSET %s",
                (stage, limit, offset)).fetchall()

    def timeline(self, media_id: int) -> list[dict[str, Any]]:
        with self.conn() as c:
            return c.execute("SELECT * FROM aib.media_event WHERE media_id=%s ORDER BY id",
                             (media_id,)).fetchall()

    def recent_events(self, limit: int = 50, outcome: str | None = None) -> list[dict]:
        with self.conn() as c:
            return c.execute(
                "SELECT e.*, m.path FROM aib.media_event e JOIN aib.media_file m "
                "ON m.id=e.media_id WHERE (%s::text IS NULL OR e.outcome=%s) "
                "ORDER BY e.id DESC LIMIT %s", (outcome, outcome, limit)).fetchall()

    def completion_times(self, action: str = "transcribe", limit: int = 200) -> list[Any]:
        """finished_at of recent successful runs of ``action`` (oldest first) for ETA."""
        with self.conn() as c:
            rows = c.execute("SELECT finished_at FROM aib.media_event WHERE action=%s AND "
                             "outcome='ok' ORDER BY id DESC LIMIT %s", (action, limit)).fetchall()
        return [r["finished_at"] for r in reversed(rows)]

    def action_durations(self, hours: int = 24) -> list[dict[str, Any]]:
        with self.conn() as c:
            return c.execute(
                "SELECT action, outcome, count(*) AS n, avg(duration_ms)::bigint AS avg_ms, "
                "percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)::bigint AS p95_ms "
                "FROM aib.media_event WHERE finished_at > now() - make_interval(hours => %s) "
                "GROUP BY action, outcome ORDER BY action", (hours,)).fetchall()
