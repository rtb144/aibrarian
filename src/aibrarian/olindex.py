"""Local Open Library reference index built from the Delta Lake export.

Source: ``OL_DELTA_DIR`` (``/openlibrary/delta``) with Delta tables ``authors``, ``works``,
``editions`` (``records`` is ignored). Only the needed columns are read (never
``payload_json``), fragment by fragment (one parquet file at a time, in record batches),
so memory stays flat even for the 295 GB editions table.

Target: one SQLite file (``OL_INDEX_PATH``, default ``/data/olindex/openlibrary.sqlite``).
Why SQLite and not Postgres: the build is a single-writer bulk load of ~100 M rows that
needs no concurrency; SQLite with ``journal_mode=OFF``/``synchronous=OFF`` loads several
times faster than COPY + index maintenance over a socket, produces no Postgres WAL/bloat
(tens of GB), and the finished file is swapped in atomically (``os.replace``), so lookups
keep using the previous index during a multi-hour rebuild. Lookups are point/range reads
on a B-tree (sub-millisecond), opened read-only.

Tables (keys without the ``/books/`` / ``/works/`` / ``/authors/`` prefix):

* ``author(key, name)``
* ``work(key, title, subtitle, author_keys)``
* ``edition(key, title, subtitle, work_key, author_keys, isbns)``
* ``isbn(isbn13, edition_key)`` – ISBN-10 converted to ISBN-13
* ``tkey(skey, work_key, edition_key, title)`` – ``skey = author surname | normalized title``
* ``meta(k, v)`` / ``done(tbl, file)`` – build state (resume) and stats

Multiple revisions per record key: the row with the highest ``revision`` wins (upsert).
Build is resumable: finished parquet files are recorded in ``done`` and skipped on restart
as long as the Delta table versions are unchanged.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from .openlibrary import norm

TABLES = ("authors", "works", "editions")
COLUMNS = {
    "authors": ["record_key", "revision", "name", "personal_name"],
    "works": ["record_key", "revision", "title", "subtitle", "authors"],
    "editions": ["record_key", "revision", "title", "subtitle", "isbn10", "isbn13",
                 "works", "authors"],
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS done (tbl TEXT, file TEXT, rows INT, PRIMARY KEY (tbl, file));
CREATE TABLE IF NOT EXISTS author (key TEXT PRIMARY KEY, rev INT, name TEXT) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS work (key TEXT PRIMARY KEY, rev INT, title TEXT, subtitle TEXT,
    author_keys TEXT) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS edition (key TEXT PRIMARY KEY, rev INT, title TEXT, subtitle TEXT,
    work_key TEXT, author_keys TEXT, isbns TEXT) WITHOUT ROWID;
"""
DERIVED = """
DROP TABLE IF EXISTS isbn; DROP TABLE IF EXISTS tkey;
CREATE TABLE isbn (isbn13 TEXT, edition_key TEXT, PRIMARY KEY (isbn13, edition_key)) WITHOUT ROWID;
CREATE TABLE tkey (skey TEXT, work_key TEXT, edition_key TEXT, title TEXT);
"""
UPSERT = {
    "author": "INSERT INTO author VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET "
              "rev=excluded.rev, name=excluded.name WHERE excluded.rev >= author.rev",
    "work": "INSERT INTO work VALUES (?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
            "rev=excluded.rev, title=excluded.title, subtitle=excluded.subtitle, "
            "author_keys=excluded.author_keys WHERE excluded.rev >= work.rev",
    "edition": "INSERT INTO edition VALUES (?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
               "rev=excluded.rev, title=excluded.title, subtitle=excluded.subtitle, "
               "work_key=excluded.work_key, author_keys=excluded.author_keys, "
               "isbns=excluded.isbns WHERE excluded.rev >= edition.rev",
}


# -- value normalisation (types differ per export: array<string>, array<struct>, JSON) --

def _parse(v: Any) -> Any:
    if isinstance(v, (bytes, bytearray)):
        v = v.decode("utf-8", "replace")
    if isinstance(v, str):
        s = v.strip()
        if s[:1] in "[{\"":
            try:
                return json.loads(s)
            except ValueError:
                return s
        return s
    return v


def _short(k: str) -> str:
    return k.rstrip("/").rsplit("/", 1)[-1]


def as_keys(v: Any) -> list[str]:
    """``/authors/OL1A`` keys from array<string>, array<struct{key}>,
    array<struct{author: struct{key}}> or a JSON string of any of those."""
    v = _parse(v)
    if v is None or v == "":
        return []
    if isinstance(v, str):
        return [_short(x) for x in re.split(r"[,\s]+", v) if x]
    if isinstance(v, dict):
        v = [v]
    out: list[str] = []
    for x in v:
        x = _parse(x)
        if isinstance(x, dict):
            a = _parse(x.get("author"))
            x = x.get("key") or (a.get("key") if isinstance(a, dict) else a)
        if isinstance(x, str) and x:
            out.append(_short(x))
    return out


def as_strs(v: Any) -> list[str]:
    v = _parse(v)
    if v is None or v == "":
        return []
    if isinstance(v, str):
        return [x for x in re.split(r"[,;\s]+", v) if x]
    if isinstance(v, dict):
        v = list(v.values())
    return [str(x) for x in v if x not in (None, "")]


def _text(v: Any) -> str | None:
    v = _parse(v)
    if isinstance(v, dict):
        v = v.get("value")
    if isinstance(v, list):
        v = v[0] if v else None
    return str(v).replace("\x00", "").strip() or None if v is not None else None


def opf_isbns(book: Path) -> list[str]:
    """ISBNs from a sidecar ``Book.opf`` or folder ``metadata.opf`` (Calibre layout)."""
    out: list[str] = []
    for opf in (book.with_suffix(".opf"), book.parent / "metadata.opf"):
        try:
            text = opf.read_text("utf-8", "replace") if opf.is_file() else ""
        except OSError:
            continue
        for m in re.finditer(r"<dc:identifier[^>]*>([^<]+)<", text, re.I):
            i = isbn13(m.group(1).split(":")[-1], strict=True)
            if i and i not in out:
                out.append(i)
    return out


def _isbn10_ok(d: str) -> bool:
    if not re.fullmatch(r"\d{9}[\dX]", d):
        return False
    return sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(d)) % 11 == 0


def _isbn13_ok(d: str) -> bool:
    return bool(re.fullmatch(r"\d{13}", d)) and sum(
        int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(d)) % 10 == 0


def isbn13(raw: str | None, strict: bool = False) -> str | None:
    """Normalise to ISBN-13 (ISBN-10 converted). ``strict`` also checks the check digit."""
    d = re.sub(r"[^0-9X]", "", str(raw or "").upper())
    if len(d) == 10:
        if strict and not _isbn10_ok(d):
            return None
        if not d[:9].isdigit():
            return None
        core = "978" + d[:9]
        return core + str((10 - sum(int(c) * (1 if i % 2 == 0 else 3)
                                    for i, c in enumerate(core)) % 10) % 10)
    if len(d) == 13 and d.isdigit() and d[:3] in ("978", "979"):
        return d if not strict or _isbn13_ok(d) else None
    return None


def isbns_in_name(name: str) -> list[str]:
    """Check-digit-valid ISBNs in a file name such as ``1503071278.epub``."""
    out = []
    for m in re.finditer(r"(?<![0-9])(97[89][0-9-]{10,14}|[0-9][0-9-]{8,11}[0-9Xx])(?![0-9])",
                         name):
        i = isbn13(m.group(1), strict=True)
        if i and i not in out:
            out.append(i)
    return out


_ART = re.compile(r"^(the|a|an) ")


def as_str(v: Any) -> str | None:
    """Coerce arbitrary metadata (pypdf IndirectObject, bytes, lists, ...) to a clean str."""
    for _ in range(10):
        go = getattr(v, "get_object", None)
        if not callable(go):
            break
        try:
            nv = go()
        except Exception:
            return None
        if nv is v:
            break
        v = nv
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray)):
        b = bytes(v)
        for enc in (("utf-16",) if b[:2] in (b"\xfe\xff", b"\xff\xfe") else ()) + ("utf-8", "latin-1"):
            try:
                v = b.decode(enc)
                break
            except UnicodeDecodeError:
                continue
    elif isinstance(v, (list, tuple, set)):
        parts = [p for p in (as_str(x) for x in v) if p]
        v = "; ".join(parts)
    elif not isinstance(v, str):
        try:
            v = str(v)
        except Exception:
            return None
    v = v.replace("\x00", "").strip()
    return v or None


def ntitle(title: Any) -> str:
    """Normalized main title (no subtitle, no leading article)."""
    title = as_str(title)
    t = re.split(r":| - |\(", title or "", maxsplit=1)[0]
    return _ART.sub("", norm(t))


def surname(name: Any) -> str:
    name = as_str(name)
    if not name:
        return ""
    name = name.split(";")[0].split("&")[0].split(" and ")[0]
    if "," in name:
        return norm(name.split(",")[0]).split(" ")[-1] if norm(name.split(",")[0]) else ""
    toks = [t for t in norm(name).split(" ") if t not in ("jr", "sr", "ii", "iii", "phd", "md")]
    return toks[-1] if toks else ""


# -- build ------------------------------------------------------------------------

def _connect(path: Path, build: bool = False) -> sqlite3.Connection:
    if build:
        c = sqlite3.connect(path, isolation_level=None)
        for p in ("journal_mode=OFF", "synchronous=OFF", "cache_size=-1000000",
                  "temp_store=FILE", "busy_timeout=60000"):
            c.execute(f"PRAGMA {p}")
    else:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    return c


def _dataset(delta_dir: Path, table: str):
    from deltalake import DeltaTable

    dt = DeltaTable(str(delta_dir / table))
    return dt.version(), dt.to_pyarrow_dataset()


def _rows(table: str, batch) -> Iterable[tuple]:
    cols = {n: batch.column(n).to_pylist() for n in batch.schema.names}
    n = batch.num_rows
    get = lambda c: cols.get(c) or [None] * n  # noqa: E731
    keys, revs = get("record_key"), get("revision")
    if table == "authors":
        for k, r, nm, pn in zip(keys, revs, get("name"), get("personal_name"), strict=True):
            name = _text(nm) or _text(pn)
            if k and name:
                yield "author", (_short(str(k)), int(r or 0), name)
    elif table == "works":
        for k, r, t, st, a in zip(keys, revs, get("title"), get("subtitle"), get("authors"),
                                  strict=True):
            if k and _text(t):
                yield "work", (_short(str(k)), int(r or 0), _text(t), _text(st),
                               json.dumps(as_keys(a)))
    else:
        for k, r, t, st, i10, i13, w, a in zip(
                keys, revs, get("title"), get("subtitle"), get("isbn10"), get("isbn13"),
                get("works"), get("authors"), strict=True):
            if not k or not _text(t):
                continue
            isbns = []
            for x in as_strs(i13) + as_strs(i10):
                i = isbn13(x)
                if i and i not in isbns:
                    isbns.append(i)
            wk = as_keys(w)
            yield "edition", (_short(str(k)), int(r or 0), _text(t), _text(st),
                              wk[0] if wk else None, json.dumps(as_keys(a)),
                              json.dumps(isbns))


def _set(c, k: str, v: Any) -> None:
    c.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, json.dumps(v)))


def _get(c, k: str, default: Any = None) -> Any:
    r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return json.loads(r[0]) if r else default


def build_index(delta_dir: Path, out: Path, batch_size: int = 50_000, rebuild: bool = False,
                log: Callable[[str], None] = print, tables: Iterable[str] = TABLES,
                reporter: Any = None) -> dict:
    """Stream the Delta tables into ``out`` (via ``out.building``, then atomic rename).

    Skips the build when the finished index was built from the same Delta versions
    (unless ``rebuild``). Resumes a half-done ``.building`` file of the same versions."""
    delta_dir, out = Path(delta_dir), Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tables = list(tables)
    versions = {}
    sets = {}
    for t in tables:
        versions[t], sets[t] = _dataset(delta_dir, t)
    if out.exists() and not rebuild:
        with _connect(out) as c:
            if _get(c, "delta_versions") == versions and _get(c, "built_at"):
                log(f"index up to date (delta versions {versions}); nothing to do")
                return stats(out)
    os.environ.setdefault("SQLITE_TMPDIR", str(out.parent))  # big sort temp files next to the index
    tmp = out.with_name(out.name + ".building")
    if tmp.exists():
        c = _connect(tmp, build=True)
        try:
            same = _get(c, "delta_versions") == versions and not rebuild
        except sqlite3.DatabaseError:
            same = False
        if not same:
            c.close()
            tmp.unlink()
        else:
            log(f"resuming {tmp}")
    if not tmp.exists():
        c = _connect(tmp, build=True)
    try:
        return _build(c, tmp, out, tables, sets, versions, batch_size, log, reporter)
    except BaseException:
        if c.in_transaction:
            c.execute("ROLLBACK")
        c.close()
        raise


def _build(c, tmp, out, tables, sets, versions, batch_size, log, reporter=None) -> dict:
    c.executescript(SCHEMA)
    _set(c, "delta_versions", versions)
    _set(c, "build_started_at", _get(c, "build_started_at") or datetime.now(UTC).isoformat())
    t0 = time.monotonic()
    rows_done = files_done = 0
    for t in tables:
        ds = sets[t]
        names = set(ds.schema.names)
        cols = [x for x in COLUMNS[t] if x in names]
        log(f"{t}: schema " + ", ".join(f"{f.name}:{f.type}" for f in ds.schema
                                        if f.name in cols))
        frags = list(ds.get_fragments())
        done = {r[0] for r in c.execute("SELECT file FROM done WHERE tbl=?", (t,))}
        total = 0
        for k, frag in enumerate(frags, 1):
            if frag.path in done:
                continue
            ft, n = time.monotonic(), 0
            c.execute("BEGIN")
            for batch in frag.to_batches(columns=cols, batch_size=batch_size):
                buf: dict[str, list] = {}
                for tbl, row in _rows(t, batch):
                    buf.setdefault(tbl, []).append(row)
                for tbl, rows in buf.items():
                    c.executemany(UPSERT[tbl], rows)
                    n += len(rows)
            c.execute("INSERT OR REPLACE INTO done VALUES (?,?,?)", (t, frag.path, n))
            _set(c, "progress", {"table": t, "file": k, "files": len(frags),
                                 "at": datetime.now(UTC).isoformat()})
            c.execute("COMMIT")
            total += n
            rows_done += n
            files_done += 1
            if reporter:
                reporter.tick(current=f"{t} file {k}/{len(frags)}", files=files_done,
                              rows=rows_done)
            el = time.monotonic() - t0
            log(f"{t}: file {k}/{len(frags)} rows={n:,} ({n / max(time.monotonic() - ft, 1e-6):,.0f}/s)"
                f" table_total={total:,} elapsed={el / 60:.1f} min")
    log("deriving isbn and title/author keys")
    _derive(c)
    for tbl in ("author", "work", "edition", "isbn", "tkey"):
        _set(c, f"rows_{tbl}", c.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0])
    _set(c, "built_at", datetime.now(UTC).isoformat())
    _set(c, "build_seconds", round(time.monotonic() - t0))
    _set(c, "progress", {"table": "done"})
    c.execute("ANALYZE")
    c.close()
    os.replace(tmp, out)
    log(f"index ready: {out} ({out.stat().st_size / 1e9:.1f} GB)")
    return stats(out)


def _derive(c: sqlite3.Connection) -> None:
    c.create_function("surname", 1, surname, deterministic=True)
    c.create_function("ntitle", 1, ntitle, deterministic=True)
    c.executescript(DERIVED)
    c.execute("BEGIN")
    c.execute("INSERT OR IGNORE INTO isbn SELECT j.value, e.key FROM edition e, json_each(e.isbns) j")
    # works by author surname, plus editions that have no work
    c.execute("INSERT INTO tkey SELECT surname(a.name) || '|' || ntitle(w.title), w.key, NULL, "
              "w.title FROM work w, json_each(w.author_keys) j JOIN author a ON a.key = j.value")
    c.execute("INSERT INTO tkey SELECT surname(a.name) || '|' || ntitle(e.title), NULL, e.key, "
              "e.title FROM edition e, json_each(e.author_keys) j JOIN author a ON a.key = j.value "
              "WHERE e.work_key IS NULL")
    c.execute("COMMIT")
    c.execute("CREATE INDEX tkey_skey ON tkey (skey)")


# -- read side ----------------------------------------------------------------------

def stats(path: Path) -> dict[str, Any]:
    path = Path(path)
    out: dict[str, Any] = {"path": str(path), "exists": path.exists()}
    if path.exists():
        with _connect(path) as c:
            out |= {k: json.loads(v) for k, v in c.execute("SELECT k, v FROM meta")}
            out["size_gb"] = round(path.stat().st_size / 1e9, 2)
    tmp = path.with_name(path.name + ".building")
    if tmp.exists():
        b: dict[str, Any] = {"size_gb": round(tmp.stat().st_size / 1e9, 2)}
        try:
            with sqlite3.connect(f"file:{tmp}?mode=ro", uri=True, timeout=5) as c:
                b |= {"progress": _get(c, "progress"),
                      "started_at": _get(c, "build_started_at"),
                      "files_done": dict(c.execute("SELECT tbl, count(*) FROM done GROUP BY tbl")
                                         .fetchall())}
        except sqlite3.Error as exc:  # exclusive lock while a batch is written
            b["note"] = f"build in progress ({exc})"
        out["building"] = b
    return out


class OLIndex:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.c = _connect(self.path)
        self.c.row_factory = sqlite3.Row

    def close(self) -> None:
        self.c.close()

    def _names(self, keys_json: str | None) -> list[str]:
        keys = json.loads(keys_json or "[]")
        if not keys:
            return []
        q = ",".join("?" * len(keys))
        got = dict(self.c.execute(f"SELECT key, name FROM author WHERE key IN ({q})", keys)
                   .fetchall())
        return [got[k] for k in keys if k in got]

    def edition(self, key: str) -> dict[str, Any] | None:
        e = self.c.execute("SELECT * FROM edition WHERE key=?", (key,)).fetchone()
        if not e:
            return None
        names = self._names(e["author_keys"])
        if not names and e["work_key"]:
            w = self.c.execute("SELECT author_keys FROM work WHERE key=?",
                               (e["work_key"],)).fetchone()
            names = self._names(w["author_keys"]) if w else []
        return {"title": e["title"], "subtitle": e["subtitle"], "authors": names,
                "author": names[0] if names else None, "edition_key": e["key"],
                "work_key": e["work_key"], "isbns": json.loads(e["isbns"] or "[]")}

    def by_isbn(self, raw: str) -> dict[str, Any] | None:
        i = isbn13(raw)
        if not i:
            return None
        for (ek,) in self.c.execute("SELECT edition_key FROM isbn WHERE isbn13=? LIMIT 5", (i,)):
            e = self.edition(ek)
            if e:
                return e | {"isbn13": i, "isbn": i, "match_type": "isbn",
                            "source": "openlibrary-index", "confidence": 1.0}
        return None

    def by_title(self, title: str, author: str | None, limit: int = 2000) -> dict | None:
        sn, nt = surname(author), ntitle(title)
        if not sn or not nt:
            return None
        lo = f"{sn}|{nt[:3]}"
        rows = self.c.execute("SELECT * FROM tkey WHERE skey >= ? AND skey < ? LIMIT ?",
                              (lo, lo + "\uffff", limit)).fetchall()
        best, bs = None, 0.0
        for r in rows:
            s = fuzz.token_set_ratio(nt, r["skey"].split("|", 1)[1]) / 100
            s = s if fuzz.ratio(nt, r["skey"].split("|", 1)[1]) >= 60 else s * 0.9
            if s > bs:
                best, bs = r, s
        if not best or bs < 0.7:
            return None
        if best["work_key"]:
            w = self.c.execute("SELECT * FROM work WHERE key=?", (best["work_key"],)).fetchone()
            names = self._names(w["author_keys"])
            names = [n for n in names if surname(n) == sn] + [n for n in names if surname(n) != sn]
            ref = {"title": w["title"], "subtitle": w["subtitle"], "authors": names,
                   "work_key": w["key"], "edition_key": None}
        else:
            ref = self.edition(best["edition_key"]) or {}
            ref["authors"] = sorted(ref.get("authors") or [], key=lambda n: surname(n) != sn)
        ref["author"] = (ref.get("authors") or [None])[0]
        return ref | {"match_type": "title", "source": "openlibrary-index",
                      "confidence": round(bs, 3)}

    def lookup(self, title: str | None, author: str | None,
               isbns: Iterable[str] | str | None = None) -> dict[str, Any] | None:
        title, author = as_str(title), as_str(author)
        if isbns is None or isinstance(isbns, (str, bytes, bytearray)) or hasattr(isbns, "get_object"):
            isbns = [isbns]
        for i in isbns or []:
            i = as_str(i)
            if not i:
                continue
            hit = self.by_isbn(i)
            if hit:
                return hit
        return self.by_title(title, author) if title and author else None


def open_index(s) -> OLIndex | None:
    p = getattr(s, "ol_index_path", None)
    if not p or not Path(p).exists():
        return None
    try:
        return OLIndex(Path(p))
    except sqlite3.Error:
        return None


def make_ref_lookup(s):
    """``lookup(title, author, isbns)`` or ``None`` when no index has been built."""
    idx = open_index(s)
    if idx is None:
        return None

    def _lookup(title, author, isbns=None):
        try:
            return idx.lookup(title, author, isbns)
        except sqlite3.Error:
            return None
    return _lookup
