"""Higher-level operations used by the API, CLI and Prefect flows:
library/book scans, applying suggestions, transcript search and clipping."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import books, changelog, clips, fileops, titles
from .actions import Context
from .config import AUDIO_EXTENSIONS, VIDEO_EXTENSIONS
from .eta import estimate
from .media import iter_files
from .openlibrary import make_ol_lookup
from .probe import compact_probe, read_audio_tags, run_ffprobe
from .search import SearchService
from .status import backlog_stages
from .video_librarian import identify, jellyfin_lookup, make_suggestion
from .windows import merge_hits

log = logging.getLogger("aibrarian.services")


def library_scan(ctx: Context, max_items: int | None = None, resume: bool = True,
                 flow_run_id: str | None = None, reporter=None) -> dict[str, Any]:
    """Identify every video/audio file in ``library_roots`` (incl. downloads and
    music) and record placement suggestions. Dry-run: nothing is moved."""
    s = ctx.settings
    run_id = ctx.db.start_run("video", {"max_items": max_items, "resume": resume}, flow_run_id)
    done = ctx.db.suggestion_sources("video") if resume else set()
    counts = {"processed": 0, "skipped_resume": 0, "errors": 0}
    if reporter:
        reporter.start()
    for root in s.library_roots:
        base = s.media_root / root
        if not base.is_dir():
            continue
        for p in iter_files(base, ["video", "audio"], [s.quarantine_root()]):
            if max_items and counts["processed"] >= max_items:
                break
            if str(p) in done:
                counts["skipped_resume"] += 1
                continue
            try:
                probe = compact_probe(run_ffprobe(p, runner=ctx.runner))
            except Exception as exc:
                probe = {"error": str(exc)}
                counts["errors"] += 1
            tags = read_audio_tags(p) if p.suffix.lower() in AUDIO_EXTENSIONS else {}
            parsed = identify(ctx.ollama, str(p.relative_to(s.media_root)), probe, tags)
            match, score = jellyfin_lookup(ctx.jellyfin, parsed)
            sug = make_suggestion(p, parsed, match, score, s.media_root, s.canonical_roots,
                                  s.video_min_confidence, s.video_review_confidence, run_id)
            ctx.db.save_suggestion(sug)
            counts["processed"] += 1
            if reporter:
                reporter.tick(current=p.name, **counts)
    if reporter:
        reporter.finish(**counts)
    ctx.db.finish_run(run_id, counts)
    return {"run_id": run_id, **counts}


def book_scan(ctx: Context, paths: list[Path] | None = None, resume: bool = True,
              max_items: int | None = None, flow_run_id: str | None = None,
              reporter=None) -> dict[str, Any]:
    """``reporter``: optional ProgressReporter, ticked once per file."""
    s = ctx.settings
    dirs = paths or s.book_source_dirs
    run_id = ctx.db.start_run("book", {"dirs": [str(d) for d in dirs]}, flow_run_id)
    done = ctx.db.suggestion_sources("book") if resume else set()
    from .olindex import make_ref_lookup

    ref = make_ref_lookup(s)
    ol = (lambda t, a, i: ref(t, a, [i] if i else [])) if ref else make_ol_lookup(ctx.db)
    counts = {"processed": 0, "auto_applied": 0, "skipped_resume": 0}
    files = list(books.iter_book_files(dirs))
    if reporter:
        todo = sum(1 for p in files
                   if p.suffix.lower() not in VIDEO_EXTENSIONS and str(p) not in done)
        reporter.total = min(todo, max_items) if max_items else todo
        reporter.start(f"files_found={len(files)}")
    for p in files:
        if max_items and counts["processed"] >= max_items:
            break
        if p.suffix.lower() in VIDEO_EXTENSIONS or str(p) in done:
            counts["skipped_resume"] += 1
            continue
        if reporter:
            reporter.tick(current=p.name, **counts)
        sug = books.catalog_file(p, s, ctx.ollama, ol_lookup=ol, run_id=run_id,
                                  ref_lookup=ref)
        sid = ctx.db.save_suggestion(sug)
        counts["processed"] += 1
        if sug.decision == "auto":
            res = apply_suggestion(ctx, sid, apply=True)
            counts["auto_applied"] += int(res["status"] == "applied")
    if reporter:
        reporter.finish(**counts)
    ctx.db.finish_run(run_id, counts)
    return {"run_id": run_id, **counts}


def _allowed_roots(ctx: Context) -> list[Path]:
    s = ctx.settings
    return [s.media_root, s.calibre_dir, s.audiobooks_dir, s.podcasts_dir]


def apply_suggestion(ctx: Context, sid: int, apply: bool = False) -> dict[str, Any]:
    """Plan (default) or execute one suggestion: move/rename the file plus its
    sidecars, refuse if the destination exists, repoint the catalog row, record
    an event and ask Jellyfin to rescan."""
    from datetime import UTC, datetime

    from .db import is_noop

    sug = ctx.db.get_suggestion(sid)
    if sug is None:
        raise KeyError(sid)
    base = {"id": sid, "source_path": sug["source_path"], "target_path": sug["target_path"]}
    if is_noop(sug["source_path"], sug["target_path"], sug["op"]):
        return base | {"status": "nothing_to_do", "ops": []}
    if sug["status"] != "open":
        return base | {"status": f"not_open:{sug['status']}", "ops": []}
    src, dst = Path(sug["source_path"]), Path(sug["target_path"])
    audit_dir = ctx.settings.report_dir

    def audit(r: fileops.OpResult) -> None:
        ctx.db.record_file_op(r.op, r.src, r.dst, r.result, r.error, sid)

    started = datetime.now(UTC)

    def run_move() -> list[fileops.OpResult]:
        return fileops.move_with_sidecars(
            src, dst, apply=apply, allowed_roots=_allowed_roots(ctx),
            audit=fileops.chain_audit(audit, fileops.jsonl_audit(audit_dir / "file_ops.jsonl")),
            cleanup_stop=next((r for r in _allowed_roots(ctx) if src.is_relative_to(r)), None))

    applied_media_id: int | None = None
    if not apply:
        ops = run_move()
    else:
        # change_log + advisory locks: pending row before the move, media row and its
        # dependent paths repointed in the same transaction that marks it committed.
        media = ctx.db.get_media_by_path(str(src)) if hasattr(ctx.db, "get_media_by_path") \
            else None
        op_name = "rename" if src.parent == dst.parent else "move"
        try:
            with changelog.change(ctx.db, op_name, "media_file" if media else (
                    "book" if sug["domain"] == "book" else "file"),
                                  old_path=src, new_path=dst,
                                  entity_id=media["id"] if media else None,
                                  details={"suggestion_id": sid}) as ch:
                ops = run_move()
                if ops and ops[0].result == "ok":
                    moved = [o for o in ops if o.result == "ok"]
                    ch.add_undo(lambda: [Path(o.dst).rename(o.src) for o in reversed(moved)
                                         if Path(o.dst).exists() and not Path(o.src).exists()])
                    ch.repoint(src, dst)
                    ch.sidecars([(o.src, o.dst) for o in moved[1:]])
                else:
                    ch.fail(ops[0].result if ops else "error")
            applied_media_id = ch.entity_id
        except changelog.Busy as exc:
            return base | {"status": "busy", "error": str(exc), "ops": []}
    main = ops[0] if ops else None
    result = main.result if main else "error"
    status = "planned" if not apply else {"ok": "applied", "skip_dst_exists": "refused_dst_exists",
                                          "skip_missing": "refused_missing",
                                          "skip_outside_root": "refused_outside_root"
                                          }.get(result, "failed")
    if not apply and result not in ("dry_run", "ok"):
        status = f"would_refuse:{result}"
    out = base | {"status": status, "ops": [o.__dict__ for o in ops]}
    if result == "skip_dst_exists" and main is not None and main.src == str(src):
        # Tom's rule: destination exists -> quarantine the SOURCE (+ sidecars), never
        # touch the destination, never delete; undo via `duplicates restore`.
        from . import quarantine

        q = quarantine.quarantine_dst_exists(
            src, dst, ctx.settings, apply=apply, db=ctx.db, suggestion_id=sid,
            roots=_allowed_roots(ctx))
        ctx.db.record_file_op("quarantine", str(src), q["quarantine_path"],
                              "would_quarantine" if not apply else q["result"],
                              "dst_exists" if q["result"] != "error" else q.get("error"), sid)
        out["quarantine"] = q
        out["ops"].append({"op": "quarantine", "src": str(src), "dst": q["quarantine_path"],
                           "result": q["result"], "error": q.get("error"),
                           "comparison": q["comparison"], "larger": q["larger"]})
        if not apply:
            out["status"] = "would_quarantine:dst_exists"
            return out
        if q["result"] == "quarantined_dst_exists":
            ctx.db.set_suggestion_status(sid, "quarantined_dst_exists")
            out["status"] = "quarantined_dst_exists"
            return out
        out["status"] = "failed_quarantine"  # left open; nothing moved
        return out
    if not apply:
        return out
    if status == "applied":
        ctx.db.set_suggestion_status(sid, "applied")
        media_id = applied_media_id  # repointed (with cascade) inside the change above
        out["media_id"] = media_id
        if media_id is not None:
            ctx.db.record_event(media_id, "apply_suggestion", ctx.db.get_media(media_id)["stage"],
                                "info", started, {"suggestion_id": sid, "from": str(src),
                                                  "to": str(dst), "sidecars": len(ops) - 1})
        out["jellyfin_refresh"] = ctx.jellyfin.refresh_library()
    elif status == "failed":
        ctx.db.set_suggestion_status(sid, "failed")
    # refused_*: left open so it can be fixed and retried
    return out


def reject_suggestion(ctx: Context, sid: int) -> dict[str, Any]:
    if ctx.db.get_suggestion(sid) is None:
        raise KeyError(sid)
    ctx.db.set_suggestion_status(sid, "dismissed")
    return {"id": sid, "status": "dismissed"}


def suggestion_rows(ctx: Context, domain: str | None = None, status: str = "open",
                    include_noop: bool = False) -> list[dict[str, Any]]:
    """Compact view used by CLI and dashboard."""
    keys = ("id", "domain", "media_kind", "op", "source_path", "target_path", "confidence",
            "decision", "status")
    return [{k: r.get(k) for k in keys}
            for r in ctx.db.list_suggestions(domain, status, include_noop)]


def search(ctx: Context, q: str, limit: int = 10, mode: str = "hybrid",
           media_id: int | None = None) -> list[dict[str, Any]]:
    svc = SearchService(ctx.db, ctx.vectors, ctx.ollama)
    return [h.model_dump() for h in svc.search(q, limit, mode, media_id)]


def search_windows(ctx: Context, q: str, limit: int = 10) -> list[dict[str, Any]]:
    hits = ctx.windows.search(ctx.ollama.embed_query(q), limit * 3)
    return merge_hits(hits)[:limit]


def resolve_title(ctx: Context, title: str) -> dict[str, Any]:
    """Fuzzy-resolve ``title`` to one searchable media item or raise TitleNotResolved."""
    res = titles.resolve(title, ctx.db.searchable_titles())
    if res.match is None:
        raise titles.TitleNotResolved(title, res)
    return res.match | {"resolved_title": titles.label(res.match)}


def autocomplete_titles(ctx: Context, q: str, limit: int = 10) -> list[dict[str, Any]]:
    avail = ctx.db.searchable_titles()
    if not q.strip():
        return [{**t, "score": None} for t in avail[:limit]]
    return [r | {"label": titles.label(r)} for r in titles.rank(q, avail, limit)]


def search_titled(ctx: Context, q: str, limit: int = 10, mode: str = "hybrid",
                  media_id: int | None = None, title: str | None = None
                  ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    resolved = None
    if title and media_id is None:
        resolved = resolve_title(ctx, title)
        media_id = resolved["media_id"]
    return search(ctx, q, limit, mode, media_id), resolved


def resolve_media(ctx: Context, media_id: int | None, title: str | None,
                  rel_path: str | None) -> dict[str, Any]:
    if media_id is not None:
        m = ctx.db.get_media(media_id)
    elif rel_path:
        m = ctx.db.get_media_by_path(str(ctx.settings.media_root / rel_path))
    elif title:
        m = ctx.db.get_media(resolve_title(ctx, title)["media_id"])
    else:
        m = None
    if m is None:
        raise LookupError("media not found")
    return m


def make_clip(ctx: Context, phrase: str, media_id: int | None = None, title: str | None = None,
              rel_path: str | None = None, match_index: int = 0,
              padding: float | None = None) -> dict[str, Any]:
    """Find ``phrase`` in a video and cut a padded clip. Without an identifier,
    search all transcripts and take the ``match_index``-th hit."""
    s = ctx.settings
    pad = s.clip_padding_s if padding is None else padding
    if media_id is None and not title and not rel_path:
        hits = search(ctx, phrase, limit=match_index + 1, mode="keyword") or \
            search(ctx, phrase, limit=match_index + 1)
        if len(hits) <= match_index:
            raise LookupError("phrase not found in any transcript")
        media_id = hits[match_index]["media_id"]
    m = resolve_media(ctx, media_id, title, rel_path)
    segs = ctx.db.segments_for_media(m["id"])
    match = clips.best_word_match(phrase, segs, s.clip_fuzzy_threshold, match_index) \
        or clips.best_quote_match(phrase, segs, s.clip_fuzzy_threshold)
    if match is None:
        raise LookupError(f"phrase not found in media {m['id']}")
    out, start, end = clips.extract_clip(Path(m["path"]), s.clips_dir, m.get("title") or "clip",
                                         match.start, match.end, pad, m.get("duration_s"),
                                         runner=ctx.runner)
    ctx.db.record_clip(m["id"], phrase, start, end, str(out))
    return {"media_id": m["id"], "title": m.get("title"), "match": match.text,
            "score": match.score, "start": start, "end": end, "output": str(out)}


def pipeline_status(ctx: Context) -> dict[str, Any]:
    counts = ctx.db.stage_counts()
    backlog = sum(counts.get(st.value, 0) for st in backlog_stages())
    return {"stages": counts, "backlog": backlog,
            "audio_layouts": ctx.db.audio_layout_counts(),
            "eta": estimate(ctx.db.completion_times("transcribe"), backlog)}


def quarantine_plan(ctx: Context, groups: list[int] | None = None, limit: int | None = None,
                    cached: bool = False, progress=None) -> dict[str, Any]:
    """Dry-run plan for identical groups (read-only)."""
    import json

    from . import duplicates, quarantine

    f = ctx.settings.report_dir / "duplicates.json"
    if cached and f.exists():
        rep = json.loads(f.read_text())
    else:  # fresh exact-only report; does not overwrite the cached full report
        if progress:
            progress("building exact-duplicates report (hash cache makes re-runs fast)...")
        rep = duplicates.report(ctx.db.duplicate_candidates(), duplicates.HashCache(ctx.db),
                                exact_only=True)
        rep["identical_books"] = book_duplicate_groups(ctx)
    return quarantine.plan(rep, ctx.settings, groups, limit)


def quarantine_execute(ctx: Context, groups: list[int] | None = None,
                       limit: int | None = None, progress=None,
                       source: str = "cli") -> dict[str, Any]:
    """Exact (identical) groups only. Fresh report, then per-file sha256 re-check."""
    from . import quarantine

    p = quarantine_plan(ctx, groups, limit, progress=progress)
    if progress:
        progress(f"plan: {p['files']} extra copies, {p['total_gb']} GB")
    return quarantine.execute(p, ctx.db, progress=progress, source=source,
                             media_root=ctx.settings.media_root)


def quarantine_purge(ctx: Context, older_than_days: int | None = None, execute: bool = False,
                     progress=None, source: str = "cli") -> dict[str, Any]:
    from . import quarantine

    days = ctx.settings.quarantine_retention_days if older_than_days is None else older_than_days
    return quarantine.purge(ctx.settings.quarantine_root(), days, execute, ctx.db,
                            progress=progress, source=source)


def cleanup_empty_folders(ctx: Context, execute: bool = False, progress=None,
                          source: str = "cli") -> dict[str, Any]:
    from . import quarantine

    return quarantine.cleanup_empty(ctx.settings.quarantine_root(), ctx.settings.media_root,
                                    execute, ctx.db, source, progress)


def dedup_backfill(ctx: Context) -> int:
    """Idempotently load existing manifests into aib.dedup_action."""
    from . import quarantine

    return quarantine.backfill_log(ctx.settings.quarantine_root(), ctx.db)


def dedup_actions(ctx: Context, action: str | None = None, since: str | None = None,
                  until: str | None = None, q: str | None = None, limit: int = 50,
                  offset: int = 0) -> dict[str, Any]:
    """Read-only: action log page + summary cards."""
    rows, total = ctx.db.dedup_actions(action or None, since or None, until or None,
                                       q or None, limit, offset)
    st = quarantine_status(ctx)
    tot = ctx.db.dedup_totals()
    purged = tot.get("purge", {"files": 0, "bytes": 0})
    return {"summary": {"quarantined_files": st["files"], "quarantined_gb": st["total_gb"],
                        "purged_files": purged["files"],
                        "purged_gb": round(purged["bytes"] / 1e9, 2),
                        "next_purge_run": st["next_purge_run"],
                        "retention_days": st["retention_days"],
                        "by_action": tot},
            "total": total, "limit": limit, "offset": offset, "actions": rows}


def quarantine_status(ctx: Context) -> dict[str, Any]:
    from . import quarantine

    return quarantine.status(ctx.settings.quarantine_root(),
                             ctx.settings.quarantine_retention_days)


def book_duplicate_groups(ctx: Context) -> list[dict[str, Any]]:
    """Identical books across Calibre, Audiobookshelf and the book drop folders."""
    from . import booklib, duplicates

    s = ctx.settings
    rows = duplicates.book_rows(booklib.library_roots(s) + list(s.book_source_dirs),
                                [s.quarantine_root()])
    return duplicates.book_groups(rows, duplicates.HashCache(ctx.db), s.media_root)


def book_suggestions_oldest_first(ctx: Context) -> list[dict[str, Any]]:
    return sorted(ctx.db.list_suggestions("book", "open"), key=lambda r: r["id"])


def book_autoapply(ctx: Context, execute: bool = False, limit: int | None = None,
                   progress=None, source: str = "cli", reporter=None) -> dict[str, Any]:
    from . import booklib

    return booklib.autoapply(book_suggestions_oldest_first(ctx), ctx.settings, ctx.db, execute,
                             limit, source, progress=progress, reporter=reporter)


def books_undo(ctx: Context, ids: list[int] | None = None, run_id: str | None = None,
               execute: bool = False, progress=None, source: str = "cli") -> dict[str, Any]:
    from . import booklib

    acts = ctx.db.get_library_actions(ids, run_id)
    return booklib.undo(acts, ctx.settings, ctx.db, execute, source, progress)


def books_validate(ctx: Context, progress=None, source: str = "cli") -> dict[str, Any]:
    from . import booklib

    return booklib.validate(ctx.settings, ctx.db, source, progress=progress)


def books_adopt_orphans(ctx: Context, execute: bool = False, limit: int | None = None,
                        progress=None, source: str = "cli") -> dict[str, Any]:
    """Ingest loose files in the Calibre library via calibredb, then quarantine them."""
    from aibrarian.adopt import adopt_orphans

    return adopt_orphans(ctx.settings, ctx.db, execute, limit, source, progress)


def latest_validation(ctx: Context) -> dict[str, Any] | None:
    rows, _ = ctx.db.library_actions(action="validate", limit=1)
    return rows[0] if rows else None


def library_log(ctx: Context, **filters: Any) -> dict[str, Any]:
    """Read-only library action log page + summary (today / totals)."""
    from datetime import UTC, datetime

    clean = {k: (v or None) for k, v in filters.items() if k not in ("limit", "offset")}
    rows, total = ctx.db.library_actions(limit=filters.get("limit", 50),
                                         offset=filters.get("offset", 0), **clean)
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return {"summary": {"today": ctx.db.library_action_counts(today),
                        "total": ctx.db.library_action_counts(None),
                        "threshold": ctx.settings.book_auto_min_confidence,
                        "auto_enabled": ctx.settings.book_auto_apply_enabled,
                        "validation": latest_validation(ctx)},
            "total": total, "limit": filters.get("limit", 50),
            "offset": filters.get("offset", 0), "actions": rows}


def duplicates_report(ctx: Context, exact_only: bool = False,
                      duration_tol: float = 2.0) -> dict[str, Any]:
    """Read-only; see duplicates.py. Result also cached to report_dir/duplicates.json."""
    import json

    from . import duplicates

    rep = duplicates.report(ctx.db.duplicate_candidates(), duplicates.HashCache(ctx.db),
                            exact_only, duration_tol)
    try:
        rep["identical_books"] = book_duplicate_groups(ctx)
    except OSError as exc:
        log.warning("book duplicates failed", extra={"error": str(exc)})
        rep["identical_books"] = []
    rep["summary"]["identical_book_groups"] = len(rep["identical_books"])
    rep["summary"]["identical_book_reclaimable_bytes"] = sum(
        g["reclaimable_bytes"] for g in rep["identical_books"])
    try:
        ctx.settings.report_dir.mkdir(parents=True, exist_ok=True)
        (ctx.settings.report_dir / "duplicates.json").write_text(json.dumps(rep, default=str))
    except OSError:
        pass
    return rep


def openlibrary_stats(ctx: Context) -> dict[str, Any]:
    """Index row counts / last build (``aibrarian openlibrary stats``, GET /openlibrary/stats)."""
    from .olindex import stats

    s = ctx.settings
    out = {"index": stats(s.ol_index_path), "delta_dir": str(s.ol_delta_dir),
           "delta_present": Path(s.ol_delta_dir).is_dir()}
    try:
        with ctx.db.conn() as c:
            out["pg_ol_edition_rows"] = c.execute(
                "SELECT count(*) AS n FROM aib.ol_edition").fetchone()["n"]
    except Exception:
        out["pg_ol_edition_rows"] = None
    return out


def openlibrary_build_index(ctx: Context, rebuild: bool = False, log=print) -> dict[str, Any]:
    from .olindex import build_index

    s = ctx.settings
    return build_index(Path(s.ol_delta_dir), Path(s.ol_index_path), rebuild=rebuild, log=log)
