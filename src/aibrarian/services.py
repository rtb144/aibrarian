"""Higher-level operations used by the API, CLI and Prefect flows:
library/book scans, applying suggestions, transcript search and clipping."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import books, clips, fileops
from .actions import Context
from .config import AUDIO_EXTENSIONS, VIDEO_EXTENSIONS
from .eta import estimate
from .media import iter_files
from .openlibrary import make_ol_lookup
from .probe import read_audio_tags, run_ffprobe
from .search import SearchService
from .status import backlog_stages
from .video_librarian import identify, jellyfin_lookup, make_suggestion
from .windows import merge_hits

log = logging.getLogger("aibrarian.services")


def library_scan(ctx: Context, max_items: int | None = None, resume: bool = True,
                 flow_run_id: str | None = None) -> dict[str, Any]:
    """Identify every video/audio file in ``library_roots`` (incl. downloads and
    music) and record placement suggestions. Dry-run: nothing is moved."""
    s = ctx.settings
    run_id = ctx.db.start_run("video", {"max_items": max_items, "resume": resume}, flow_run_id)
    done = ctx.db.suggestion_sources("video") if resume else set()
    counts = {"processed": 0, "skipped_resume": 0, "errors": 0}
    for root in s.library_roots:
        base = s.media_root / root
        if not base.is_dir():
            continue
        for p in iter_files(base, ["video", "audio"]):
            if max_items and counts["processed"] >= max_items:
                break
            if str(p) in done:
                counts["skipped_resume"] += 1
                continue
            try:
                probe = run_ffprobe(p, runner=ctx.runner)
            except Exception as exc:
                probe = {"error": str(exc)}
            tags = read_audio_tags(p) if p.suffix.lower() in AUDIO_EXTENSIONS else {}
            parsed = identify(ctx.ollama, str(p.relative_to(s.media_root)), probe, tags)
            match, score = jellyfin_lookup(ctx.jellyfin, parsed)
            sug = make_suggestion(p, parsed, match, score, s.media_root, s.canonical_roots,
                                  s.video_min_confidence, s.video_review_confidence, run_id)
            ctx.db.save_suggestion(sug)
            counts["processed"] += 1
    ctx.db.finish_run(run_id, counts)
    return {"run_id": run_id, **counts}


def book_scan(ctx: Context, paths: list[Path] | None = None, resume: bool = True,
              max_items: int | None = None, flow_run_id: str | None = None) -> dict[str, Any]:
    s = ctx.settings
    dirs = paths or s.book_source_dirs
    run_id = ctx.db.start_run("book", {"dirs": [str(d) for d in dirs]}, flow_run_id)
    done = ctx.db.suggestion_sources("book") if resume else set()
    ol = make_ol_lookup(ctx.db)
    counts = {"processed": 0, "auto_applied": 0, "skipped_resume": 0}
    for p in books.iter_book_files(dirs):
        if max_items and counts["processed"] >= max_items:
            break
        if p.suffix.lower() in VIDEO_EXTENSIONS or str(p) in done:
            counts["skipped_resume"] += 1
            continue
        sug = books.catalog_file(p, s, ctx.ollama, ol_lookup=ol, run_id=run_id)
        sid = ctx.db.save_suggestion(sug)
        counts["processed"] += 1
        if sug.decision == "auto":
            res = apply_suggestion(ctx, sid, apply=True)
            counts["auto_applied"] += int(res["status"] == "applied")
    ctx.db.finish_run(run_id, counts)
    return {"run_id": run_id, **counts}


def _allowed_roots(ctx: Context) -> list[Path]:
    s = ctx.settings
    return [s.media_root, s.calibre_dir, s.audiobooks_dir, s.podcasts_dir]


def apply_suggestion(ctx: Context, sid: int, apply: bool = False) -> dict[str, Any]:
    sug = ctx.db.get_suggestion(sid)
    if sug is None:
        raise KeyError(sid)
    if not sug["target_path"] or sug["op"] == "none":
        return {"id": sid, "status": "nothing_to_do", "ops": []}
    src, dst = Path(sug["source_path"]), Path(sug["target_path"])
    audit_dir = ctx.settings.report_dir

    def audit(r: fileops.OpResult) -> None:
        ctx.db.record_file_op(r.op, r.src, r.dst, r.result, r.error, sid)

    ops = fileops.move_with_sidecars(
        src, dst, apply=apply, allowed_roots=_allowed_roots(ctx),
        audit=fileops.chain_audit(audit, fileops.jsonl_audit(audit_dir / "file_ops.jsonl")),
        cleanup_stop=next((r for r in _allowed_roots(ctx) if src.is_relative_to(r)), None))
    main = ops[0] if ops else None
    status = "planned"
    if apply and main is not None:
        status = "applied" if main.result == "ok" else "failed"
        ctx.db.set_suggestion_status(sid, status)
        if main.result == "ok":
            ctx.db.update_path(str(src), str(dst))
    return {"id": sid, "status": status, "ops": [o.__dict__ for o in ops]}


def search(ctx: Context, q: str, limit: int = 10, mode: str = "hybrid",
           media_id: int | None = None) -> list[dict[str, Any]]:
    svc = SearchService(ctx.db, ctx.vectors, ctx.ollama)
    return [h.model_dump() for h in svc.search(q, limit, mode, media_id)]


def search_windows(ctx: Context, q: str, limit: int = 10) -> list[dict[str, Any]]:
    hits = ctx.windows.search(ctx.ollama.embed_query(q), limit * 3)
    return merge_hits(hits)[:limit]


def resolve_media(ctx: Context, media_id: int | None, title: str | None,
                  rel_path: str | None) -> dict[str, Any]:
    if media_id is not None:
        m = ctx.db.get_media(media_id)
    elif rel_path:
        m = ctx.db.get_media_by_path(str(ctx.settings.media_root / rel_path))
    elif title:
        found = ctx.db.find_media_by_title(title, 1)
        m = found[0] if found else None
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
            "eta": estimate(ctx.db.completion_times("transcribe"), backlog)}
