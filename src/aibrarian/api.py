"""Unified FastAPI service (replaces librarian :8787, book-librarian :8788,
catalog-api :8790 and movie-clipper :8791).

Old catalog-api routes (/movies, /movies/lookup, /movies/by-path,
/movies/{id}/location, /search/text, /search/semantic) are kept with the same
query parameters so existing callers keep working.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from aibrarian import __version__

from . import services
from .actions import Context, RetryableError, run_action
from .config import get_settings
from .dashboard import (
    CHANGES_UI_HTML,
    DASHBOARD_HTML,
    DEDUP_UI_HTML,
    DUPLICATES_UI_HTML,
    EMBED_EVAL_UI_HTML,
    LIBRARY_LOG_UI_HTML,
    SEARCH_UI_HTML,
    TRANSCRIPT_LANG_UI_HTML,
)
from .metrics import (
    SUGGESTIONS_OPEN,
    refresh_consistency_gauges,
    refresh_pending_gauge,
    refresh_stage_gauges,
)
from .status import ACTIONS, Stage
from .titles import TitleNotResolved

log = logging.getLogger("aibrarian.api")


class ClipRequest(BaseModel):
    phrase: str
    movie_id: int | None = None
    title: str | None = None
    rel_path: str | None = None
    match_index: int = 0
    padding: float | None = None


class LangReview(BaseModel):
    review: str | None = None  # 'correct' | 'wrong' | null (clear)
    note: str | None = None


class BookScanRequest(BaseModel):
    path: str | None = None
    resume: bool = True
    max_items: int | None = None


def create_app(ctx: Context | None = None, migrate: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if migrate:
            applied = c().db.migrate()
            if applied:
                log.info("migrations applied", extra={"versions": applied})
            try:  # load quarantine manifests written before aib.dedup_action existed
                services.dedup_backfill(c())
            except Exception as exc:
                log.warning("dedup backfill failed", extra={"error": str(exc)})
        yield

    app = FastAPI(title="aibrarian", version=__version__, lifespan=lifespan)

    def c() -> Context:
        if app.state.ctx is None:
            app.state.ctx = Context.from_settings(get_settings())
        return app.state.ctx

    app.state.ctx = ctx

    def with_locations(m: dict[str, Any]) -> dict[str, Any]:
        root = c().settings.media_root
        out = dict(m)
        p = Path(m["path"])
        out["rel_path"] = str(p.relative_to(root)) if p.is_relative_to(root) else str(p)
        out["transcript_path"] = str(p.with_suffix(".transcript.json"))
        out["srt_path"] = str(p.with_suffix(".srt"))
        return out

    def need(media_id: int) -> dict[str, Any]:
        m = c().db.get_media(media_id)
        if not m:
            raise HTTPException(404, f"No media with id {media_id}")
        return m

    # -- health / metrics ---------------------------------------------------
    @app.get("/health")
    def health() -> dict[str, Any]:
        ctx_ = c()
        try:
            with ctx_.db.conn() as conn:
                conn.execute("SELECT 1")
        except Exception as exc:
            raise HTTPException(503, f"database unreachable: {exc}") from exc
        jf = ctx_.jellyfin.check()
        return {"status": "ok", "version": __version__, "qdrant": ctx_.vectors.ping(),
                "ollama": ctx_.ollama.ping(), "jellyfin": jf == "ok", "jellyfin_status": jf}

    @app.get("/metrics")
    def metrics() -> Response:
        try:
            refresh_stage_gauges(c().db.stage_counts())
            for d in ("video", "book"):
                SUGGESTIONS_OPEN.labels(d).set(len(c().db.list_suggestions(d)))
        except Exception:
            log.exception("metrics refresh failed")
        try:
            from .consistency import load_report

            refresh_consistency_gauges(load_report(c().settings.report_dir))
            refresh_pending_gauge(c().db)
        except Exception:
            log.exception("consistency metrics refresh failed")
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # -- status / observability --------------------------------------------
    @app.get("/status")
    def status() -> dict[str, Any]:
        return services.pipeline_status(c())

    @app.get("/status/stages/{stage}")
    def by_stage(stage: Stage, limit: int = Query(100, le=1000), offset: int = 0):
        return c().db.list_by_stage(stage.value, limit, offset)

    @app.get("/status/events")
    def events(limit: int = Query(50, le=500), outcome: str | None = None):
        return c().db.recent_events(limit, outcome)

    @app.get("/status/durations")
    def durations(hours: int = Query(24, ge=1, le=24 * 30)):
        return c().db.action_durations(hours)

    @app.get("/media/{media_id}/timeline")
    def timeline(media_id: int):
        return {"media": with_locations(need(media_id)), "events": c().db.timeline(media_id)}

    @app.post("/media/{media_id}/retry")
    def retry(media_id: int):
        need(media_id)
        return {"reset": c().db.retry_failed(media_id)}

    @app.post("/media/{media_id}/actions/{action}")
    def run_one(media_id: int, action: str):
        if action not in ACTIONS:
            raise HTTPException(404, f"unknown action {action}")
        try:
            return {"outcome": run_action(c(), action, need(media_id))}
        except RetryableError as exc:
            raise HTTPException(500, f"attempt failed, will retry: {exc}") from exc

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard() -> str:
        return DASHBOARD_HTML

    # -- catalog (old catalog-api compatible) ------------------------------
    @app.get("/media")
    @app.get("/movies")
    def list_media(status: str | None = None, kind: str | None = "video",
                   audio_layout: str | None = None,
                   limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)):
        return [with_locations(m)
                for m in c().db.list_media(status, kind, limit, offset, audio_layout)]

    @app.get("/media/{media_id}/info")
    def media_info(media_id: int):
        """Full stored ffprobe metadata (streams, HDR, subtitles, chapters)."""
        m = need(media_id)
        return {"id": media_id, "audio_layout": m.get("audio_layout"),
                "audio_stream_index": m.get("audio_stream_index"),
                "audio_language": m.get("audio_language"), "probed_at": m.get("probed_at"),
                "media_info": m.get("media_info")}

    @app.post("/media/{media_id}/reprobe")
    def reprobe_one(media_id: int):
        from .actions import reprobe

        return reprobe(c(), need(media_id))

    @app.get("/status/audio-layouts")
    def audio_layouts():
        return c().db.audio_layout_counts()

    @app.get("/movies/lookup")
    def lookup(title: str = Query(..., min_length=1), limit: int = Query(10, ge=1, le=50)):
        return [with_locations(m) for m in c().db.find_media_by_title(title, limit)]

    @app.get("/movies/by-path")
    def by_path(rel_path: str = Query(..., min_length=1)):
        m = c().db.get_media_by_path(str(c().settings.media_root / rel_path))
        if not m:
            raise HTTPException(404, f"No media registered at {rel_path}")
        return with_locations(m)

    @app.get("/media/{media_id}")
    @app.get("/movies/{media_id}")
    def get_media(media_id: int):
        return with_locations(need(media_id))

    @app.get("/movies/{media_id}/location")
    def location(media_id: int):
        m = with_locations(need(media_id))
        return {k: m.get(k) for k in ("id", "title", "year", "stage", "rel_path", "path",
                                      "transcript_path", "srt_path")}

    @app.get("/media/{media_id}/segments")
    @app.get("/movies/{media_id}/segments")
    def segments(media_id: int):
        need(media_id)
        return [{k: s[k] for k in ("seq", "start_s", "end_s", "text")}
                for s in c().db.segments_for_media(media_id)]

    # -- search ------------------------------------------------------------
    def _unresolved(exc: TitleNotResolved) -> HTTPException:
        code = 404 if exc.reason == "not_found" else 422
        return HTTPException(code, {"error": str(exc), "reason": exc.reason,
                                    "title": exc.query, "suggestions": exc.suggestions})

    @app.get("/search")
    def search(response: Response, q: str = Query(..., min_length=2), mode: str = "hybrid",
               movie_id: int | None = None, limit: int = Query(10, ge=1, le=100),
               title: str | None = None):
        """List of hits (unchanged shape). With ``title`` the film is fuzzy-resolved
        and reported in ``X-Resolved-Title`` / ``X-Media-Id`` headers; 404 (no match)
        or 422 (ambiguous) carry ``suggestions``."""
        if mode not in ("hybrid", "semantic", "keyword"):
            raise HTTPException(422, "mode must be hybrid|semantic|keyword")
        try:
            hits, resolved = services.search_titled(c(), q, limit, mode, movie_id, title)
        except TitleNotResolved as exc:
            raise _unresolved(exc) from exc
        except Exception as exc:
            raise HTTPException(503, f"search backend unavailable: {exc}") from exc
        if resolved:
            # headers must be latin-1; quote non-ASCII
            from urllib.parse import quote

            response.headers["X-Resolved-Title"] = quote(resolved["resolved_title"], safe=" ()'.,:-&!?")
            response.headers["X-Media-Id"] = str(resolved["media_id"])
            response.headers["X-Title-Score"] = str(resolved["score"])
        return hits

    @app.get("/search/text")
    def search_text(response: Response, q: str = Query(..., min_length=2),
                    movie_id: int | None = None, limit: int = Query(10, ge=1, le=100),
                    title: str | None = None):
        return search(response, q, "keyword", movie_id, limit, title)

    @app.get("/search/semantic")
    def search_semantic(response: Response, q: str = Query(..., min_length=2),
                        movie_id: int | None = None, limit: int = Query(10, ge=1, le=100),
                        title: str | None = None):
        return search(response, q, "semantic", movie_id, limit, title)

    @app.get("/titles/resolve")
    def resolve_title(title: str = Query(..., min_length=1)):
        try:
            return services.resolve_title(c(), title)
        except TitleNotResolved as exc:
            raise _unresolved(exc) from exc

    @app.get("/titles/autocomplete")
    def autocomplete(q: str = "", limit: int = Query(10, ge=1, le=50)):
        """Only searchable (transcribed) titles; prefix + fuzzy."""
        return services.autocomplete_titles(c(), q, limit)

    @app.get("/duplicates")
    def duplicates(exact_only: bool = False, cached: bool = False,
                   duration_tol: float = Query(2.0, ge=0, le=60)):
        """Read-only report. ``cached=true`` returns the last saved report instantly."""
        import json

        f = c().settings.report_dir / "duplicates.json"
        if cached and f.exists():
            return json.loads(f.read_text())
        return services.duplicates_report(c(), exact_only, duration_tol)

    @app.get("/duplicates/quarantine-plan")
    def quarantine_plan(cached: bool = True, limit: int | None = Query(None, ge=1),
                        group: Annotated[list[int] | None, Query()] = None):
        """Read-only dry-run preview of `aibrarian duplicates quarantine`. Moves nothing."""
        return services.quarantine_plan(c(), group, limit, cached)

    @app.get("/duplicates/quarantine")
    def quarantine_status():
        """Read-only: files in quarantine, their age, purge dates, next purge run."""
        return services.quarantine_status(c())

    @app.get("/dedup/actions")
    def dedup_actions(action: str | None = None, since: str | None = None,
                      until: str | None = None, q: str | None = None,
                      limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
        """Read-only dedup action log (newest first) + summary."""
        return services.dedup_actions(c(), action, since, until, q, limit, offset)

    @app.get("/library-log")
    def library_log(action: str | None = None, domain: str | None = None,
                    since: str | None = None, until: str | None = None,
                    title: str | None = None, author: str | None = None,
                    series: str | None = None, run_id: str | None = None,
                    limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
        """Read-only automatic library action log (newest first) + today/total counts."""
        return services.library_log(c(), action=action, domain=domain, since=since,
                                    until=until, title=title, author=author, series=series,
                                    run_id=run_id, limit=limit, offset=offset)

    @app.get("/library-log/validation")
    def library_validation():
        """Latest calibre-validate result (read-only)."""
        return services.latest_validation(c()) or {}

    @app.get("/library-log/facets")
    def library_facets(field: str = Query(..., pattern="^(title|author|series)$"), q: str = "",
                       limit: int = Query(20, ge=1, le=100)):
        return c().db.library_facet(field, q, limit)

    @app.get("/library-log/ui", response_class=HTMLResponse)
    def library_log_ui() -> str:
        return LIBRARY_LOG_UI_HTML

    # -- embedding A/B (read-only) -----------------------------------------
    def _eval():
        from .embedeval import Harness

        return Harness(c().settings, c().db, c().ollama, say=lambda m: log.info(m))

    @app.get("/embed-eval/ui", response_class=HTMLResponse)
    def embed_eval_ui() -> str:
        return EMBED_EVAL_UI_HTML

    @app.get("/embed-eval/latest")
    def embed_eval_latest():
        h = _eval()
        md = h.out / "latest.md"
        return {"report": h.latest(), "markdown": md.read_text() if md.exists() else None,
                "models": list(h.state()["models"]), "movies": h.state()["movies"]}

    @app.get("/embed-eval/query")
    def embed_eval_query(q: str = Query(..., min_length=2), k: int = Query(5, le=20)):
        h = _eval()
        out = {}
        for m in h.state()["models"]:
            try:
                r = h.search(m, q, k)
            except Exception as exc:
                out[m] = {"error": str(exc)}
                continue
            out[m] = {ix: [{"title": x["payload"].get("title"),
                            "movie_id": x["payload"].get("movie_id"),
                            "start": x["payload"].get("start"), "end": x["payload"].get("end"),
                            "text": x["payload"].get("text"), "score": round(x["score"], 4)}
                           for x in r[ix][:k]] for ix in ("segments", "windows")}
            out[m]["latency_ms"] = round(r["embed_ms"] + r["search_ms"], 1)
        return out

    @app.get("/dedup/ui", response_class=HTMLResponse)
    def dedup_ui() -> str:
        return DEDUP_UI_HTML

    @app.get("/duplicates/ui", response_class=HTMLResponse)
    def duplicates_ui() -> str:
        return DUPLICATES_UI_HTML

    @app.get("/search/ui", response_class=HTMLResponse)
    def search_ui() -> str:
        return SEARCH_UI_HTML

    @app.get("/search/windows")
    def search_windows(q: str = Query(..., min_length=2), limit: int = Query(10, le=50)):
        return services.search_windows(c(), q, limit)

    # -- clips -------------------------------------------------------------
    @app.post("/clip")
    def clip(req: ClipRequest):
        try:
            return services.make_clip(c(), req.phrase, req.movie_id, req.title, req.rel_path,
                                      req.match_index, req.padding)
        except TitleNotResolved as exc:
            raise _unresolved(exc) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    # -- librarians --------------------------------------------------------
    @app.post("/scan")
    def scan(bg: BackgroundTasks, max_items: int | None = Query(None, ge=1),
             resume: bool = True):
        bg.add_task(services.library_scan, c(), max_items, resume)
        return {"ok": True, "queued": "library-scan"}

    @app.get("/report/latest")
    def report_latest(domain: str = "video"):
        run = c().db.latest_run(domain)
        if not run:
            return {"ok": False, "error": "no reports yet"}
        return run

    @app.post("/book-scan")
    def book_scan(req: BookScanRequest, bg: BackgroundTasks):
        paths = [Path(req.path)] if req.path else None
        bg.add_task(services.book_scan, c(), paths, req.resume, req.max_items)
        return {"ok": True, "queued": "book-scan"}

    @app.get("/suggestions")
    def suggestions(domain: str | None = None, status: str = "open", include_noop: bool = False):
        """Full rows (incl. ``id``); no-ops (target == source) hidden by default."""
        return c().db.list_suggestions(domain, status, include_noop)

    @app.get("/book-suggestions")
    def book_suggestions():
        return c().db.list_suggestions("book", "open", False)

    @app.post("/suggestions/{sid}/apply")
    def apply(sid: int, dry_run: bool = True):
        try:
            return services.apply_suggestion(c(), sid, apply=not dry_run)
        except KeyError as exc:
            raise HTTPException(404, "suggestion not found") from exc

    @app.post("/suggestions/{sid}/dismiss")
    @app.post("/suggestions/{sid}/reject")
    def dismiss(sid: int):
        try:
            return services.reject_suggestion(c(), sid)
        except KeyError as exc:
            raise HTTPException(404, "suggestion not found") from exc

    # -- transcript language check (read-only on media) -------------------
    @app.get("/transcripts/lang")
    def transcript_lang(flagged: bool = True, non_english: bool = False,
                        review: str | None = Query(None, pattern="^(none|correct|wrong)$"),
                        q: str | None = None, limit: int = Query(200, ge=1, le=2000),
                        offset: int = Query(0, ge=0)):
        """Results of the last `transcripts langcheck` run (aib.transcript_lang)."""
        return c().db.langcheck_list(flagged, non_english, review, q, limit, offset)

    @app.post("/transcripts/lang/{item_id}/review")
    def transcript_lang_review(item_id: int, body: LangReview):
        """Record Tom's verdict; only the DB row changes, never the transcript."""
        if body.review not in (None, "correct", "wrong"):
            raise HTTPException(422, "review must be 'correct', 'wrong' or null")
        row = c().db.langcheck_review(item_id, body.review, body.note)
        if not row:
            raise HTTPException(404, "no such langcheck item")
        return row

    @app.get("/transcripts/lang/ui", response_class=HTMLResponse)
    def transcript_lang_ui() -> str:
        return TRANSCRIPT_LANG_UI_HTML

    # -- change log + consistency (read-only) ------------------------------
    @app.get("/changes")
    def changes(day: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
                pipeline: str | None = None, op: str | None = None, path: str | None = None,
                status: str | None = Query(None,
                                           pattern="^(pending|committed|failed|rolled_back)$"),
                limit: int = Query(200, ge=1, le=2000), offset: int = Query(0, ge=0)):
        """aib.change_log page, newest first; filters: day, pipeline (actor substring),
        op, path (substring of old/new path), status."""
        rows, total = c().db.changes(day or None, pipeline or None, op or None, path or None,
                                     status or None, limit, offset)
        return {"total": total, "limit": limit, "offset": offset, "changes": rows}

    @app.get("/changes/ui", response_class=HTMLResponse)
    def changes_ui() -> str:
        return CHANGES_UI_HTML

    @app.get("/consistency")
    def consistency_report(full: bool = False):
        """Latest REPORT_DIR/consistency.json (counts + fix plan; ``full`` adds items)."""
        from .consistency import load_report

        rep = load_report(c().settings.report_dir)
        if rep is None:
            raise HTTPException(404, "no consistency report yet (run the consistency flow)")
        if not full:
            rep = {k: v for k, v in rep.items() if k != "issues"} | {
                "samples": {k: v[:5] for k, v in rep.get("issues", {}).items() if v}}
        return rep

    # -- open library ------------------------------------------------------
    @app.get("/openlibrary/lookup")
    def ol_lookup(title: str | None = None, author: str | None = None,
                  isbn: str | None = None, limit: int = Query(5, le=50)):
        from .openlibrary import lookup as ol

        if not (title or isbn):
            raise HTTPException(422, "title or isbn required")
        return ol(c().db, title, author, isbn, limit)

    @app.get("/openlibrary/stats")
    def ol_stats():
        return services.openlibrary_stats(c())

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        resp = await call_next(request)
        if request.url.path not in ("/health", "/metrics"):
            log.info("request", extra={"method": request.method, "path": request.url.path,
                                       "status": resp.status_code})
        return resp

    return app


def main() -> None:
    import os

    import uvicorn

    from .logging_setup import setup_logging

    setup_logging("aibrarian-api")
    from .profiling import init_profiling

    init_profiling("api")
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.getenv("AIB_API_PORT", "8790")),
                log_config=None)
