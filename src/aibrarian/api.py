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
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from . import __version__, services
from .actions import Context, RetryableError, run_action
from .config import get_settings
from .dashboard import DASHBOARD_HTML, SEARCH_UI_HTML
from .metrics import SUGGESTIONS_OPEN, refresh_stage_gauges
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
    def suggestions(domain: str | None = None, status: str = "open"):
        return c().db.list_suggestions(domain, status)

    @app.get("/book-suggestions")
    def book_suggestions():
        return c().db.list_suggestions("book")

    @app.post("/suggestions/{sid}/apply")
    def apply(sid: int, dry_run: bool = True):
        try:
            return services.apply_suggestion(c(), sid, apply=not dry_run)
        except KeyError as exc:
            raise HTTPException(404, "suggestion not found") from exc

    @app.post("/suggestions/{sid}/dismiss")
    def dismiss(sid: int):
        c().db.set_suggestion_status(sid, "dismissed")
        return {"id": sid, "status": "dismissed"}

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
        with c().db.conn() as conn:
            return conn.execute("SELECT count(*) AS editions FROM aib.ol_edition").fetchone()

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
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.getenv("AIB_API_PORT", "8790")),
                log_config=None)
