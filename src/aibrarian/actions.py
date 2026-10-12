"""Single-purpose, idempotent pipeline actions.

Each action takes one media item that is in the action's ``requires`` stage,
does exactly one thing, and moves the item to ``produces`` (or a side state).
All bookkeeping - stage transition, ``aib.media_event`` row, attempts, Prefect
run ids, Prometheus metrics, JSON logs - lives in :func:`run_action`, so the
action bodies stay small and testable. Re-running an action is safe: outputs
are overwritten / upserted, never duplicated.

Prefect wraps these in tasks (``aibrarian.flows``); the CLI can run them
directly too (``aibrarian run-action probe --id 42``).
"""

from __future__ import annotations

import logging
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import sidecars
from .config import Settings
from .db import Database
from .jellyfin import JellyfinClient
from .media import scan_root
from .metrics import ACTION_SECONDS, ACTION_TOTAL
from .ollama import OllamaClient
from .probe import (
    duration_of,
    extract_audio_cmd,
    has_audio,
    is_english,
    llm_view,
    media_info,
    run_checked,
    run_ffprobe,
    select_audio_stream,
)
from .status import ACTIONS, MAX_ATTEMPTS, Stage, should_give_up
from .transcribe import (
    Transcriber,
    WhisperTranscriber,
    chunk_segments,
    result_from_dict,
    result_to_dict,
)
from .vectors import VectorStore
from .video_librarian import identify, jellyfin_lookup, make_suggestion
from .windows import make_windows, window_point_id

log = logging.getLogger("aibrarian.actions")


@dataclass
class Context:
    """Dependencies for actions; build once per worker process."""
    settings: Settings
    db: Database
    ollama: OllamaClient
    vectors: VectorStore
    windows: VectorStore
    jellyfin: JellyfinClient
    transcriber_factory: Callable[[], Transcriber]
    runner: Callable[..., Any] = subprocess.run
    run_ids: Callable[[], tuple[str | None, str | None]] = lambda: (None, None)
    _transcriber: Transcriber | None = field(default=None, init=False)

    @property
    def transcriber(self) -> Transcriber:
        if self._transcriber is None:
            self._transcriber = self.transcriber_factory()
        return self._transcriber

    @classmethod
    def from_settings(cls, s: Settings) -> Context:
        return cls(
            settings=s, db=Database.from_settings(s), ollama=OllamaClient.from_settings(s),
            vectors=VectorStore(s.qdrant_url, s.qdrant_collection, s.embed_dim),
            windows=VectorStore(s.qdrant_url, s.window_collection, s.embed_dim),
            jellyfin=JellyfinClient.from_settings(s),
            transcriber_factory=lambda: WhisperTranscriber.from_settings(s),
        )


@dataclass
class Outcome:
    to_stage: Stage
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def label(self) -> str:
        return {Stage.SKIPPED: "skipped", Stage.NEEDS_REVIEW: "needs_review",
                Stage.FAILED: "failed"}.get(self.to_stage, "ok")


class RetryableError(RuntimeError):
    """Raised after a failed attempt that should be retried by the orchestrator."""


# --------------------------------------------------------------------------
def run_action(ctx: Context, name: str, media: dict[str, Any] | int,
               max_attempts: int = MAX_ATTEMPTS) -> str:
    """Run action ``name`` on one item with full status bookkeeping.

    Returns the outcome label. Raises :class:`RetryableError` when the attempt
    failed but more attempts remain (so Prefect's task retry kicks in); after
    ``max_attempts`` the item moves to ``failed`` instead.
    """
    spec = ACTIONS[name]
    row = ctx.db.get_media(media) if isinstance(media, int) else media
    if row is None:
        raise ValueError(f"media {media} not found")
    if row["stage"] != spec.requires:
        log.info("skip: not in required stage", extra={
            "action": name, "media_id": row["id"], "stage": row["stage"]})
        return "noop"
    flow_run_id, task_run_id = ctx.run_ids()
    started = datetime.now(UTC)
    t0 = time.perf_counter()
    attempt = int(row.get("stage_attempts") or 0) + 1
    try:
        outcome: Outcome = ACTION_FUNCS[name](ctx, row)
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        ACTION_TOTAL.labels(name, "failed").inc()
        ACTION_SECONDS.labels(name).observe(time.perf_counter() - t0)
        log.warning("action failed", extra={"action": name, "media_id": row["id"],
                                            "attempt": attempt, "error": err})
        if should_give_up(attempt, max_attempts):
            ctx.db.transition(row["id"], name, spec.requires, Stage.FAILED, "failed", started,
                              attempt=attempt, error=err, flow_run_id=flow_run_id,
                              task_run_id=task_run_id)
            return "failed"
        ctx.db.record_attempt_failure(row["id"], name, spec.requires, started, err, attempt,
                                      flow_run_id, task_run_id)
        raise RetryableError(err) from exc
    ACTION_TOTAL.labels(name, outcome.label).inc()
    ACTION_SECONDS.labels(name).observe(time.perf_counter() - t0)
    ok = ctx.db.transition(row["id"], name, spec.requires, outcome.to_stage, outcome.label,
                           started, attempt=attempt, error=outcome.error,
                           details=outcome.details, flow_run_id=flow_run_id,
                           task_run_id=task_run_id)
    log.info("action done", extra={"action": name, "media_id": row["id"],
                                   "outcome": outcome.label, "applied": ok,
                                   "duration_s": round(time.perf_counter() - t0, 3)})
    return outcome.label if ok else "raced"


# --------------------------------------------------------------------------
DISCOVER_LOCK = 724412


def discover(ctx: Context, roots: list[str] | None = None) -> dict[str, int]:
    """Walk roots, register new/changed videos as ``discovered``. Not per-item.

    Fast on re-scans: unchanged files (same size + mtime as in the catalog) are
    neither hashed nor written; only new/changed rows are upserted, in short
    batches. A Postgres advisory lock makes concurrent discovers a no-op
    instead of fighting over row locks.
    """
    s = ctx.settings
    stats = {"seen": 0, "unchanged": 0, "new": 0, "reset": 0, "missing": 0, "locked": 0}
    with ctx.db.try_lock(DISCOVER_LOCK) as got:
        if not got:
            log.info("discover already running elsewhere; skipping")
            stats["locked"] = 1
            return stats
        for root in roots or s.transcribe_roots:
            t0 = time.perf_counter()
            known = ctx.db.known_files(root)
            files = scan_root(s.media_root, root, kinds=["video"], known=known,
                              progress=lambda r, n: log.info(
                                  "discover progress", extra={"root": r, "files": n}))
            changed = [f for f in files
                       if (k := known.get(str(f.path))) is None or k[2] != f.fingerprint]
            res = ctx.db.upsert_media(changed)
            stats["seen"] += len(files)
            stats["unchanged"] += len(files) - len(changed)
            stats["new"] += sum(r["created"] for r in res)
            stats["reset"] += sum(r["reset"] for r in res)
            stats["missing"] += ctx.db.mark_missing(root, [str(f.path) for f in files])
            log.info("discover root done", extra={
                "root": root, "files": len(files), "changed": len(changed),
                "seconds": round(time.perf_counter() - t0, 2)})
    log.info("discover done", extra=stats)
    return stats


def store_probe(ctx: Context, row: dict) -> tuple[dict, dict]:
    """ffprobe the file and persist full metadata + LLM view (idempotent)."""
    data = run_ffprobe(Path(row["path"]), runner=ctx.runner)
    info = media_info(data)
    ctx.db.update_media_fields(row["id"], probe=llm_view(info), media_info=info,
                               duration_s=duration_of(data), audio_layout=info["audio_layout"],
                               probed_at=datetime.now(UTC))
    return data, info


def probe(ctx: Context, row: dict) -> Outcome:
    data, info = store_probe(ctx, row)
    if not has_audio(data):
        return Outcome(Stage.SKIPPED, {"reason": "no audio stream"})
    return Outcome(Stage.PROBED, {"duration_s": info["container"]["duration"],
                                  "audio_streams": len(info["audio"]),
                                  "subtitle_streams": len(info["subtitles"]),
                                  "chapters": len(info["chapters"]),
                                  "audio_layout": info["audio_layout"]})


def reprobe(ctx: Context, row: dict | int) -> dict[str, Any]:
    """Refresh metadata of an item in any stage without changing its stage."""
    row = ctx.db.get_media(row) if isinstance(row, int) else row
    started = datetime.now(UTC)
    flow_run_id, task_run_id = ctx.run_ids()
    try:
        _, info = store_probe(ctx, row)
    except Exception as exc:
        ctx.db.record_event(row["id"], "reprobe", row["stage"], "failed", started,
                            error=f"{type(exc).__name__}: {exc}", flow_run_id=flow_run_id,
                            task_run_id=task_run_id)
        return {"id": row["id"], "outcome": "failed", "error": str(exc)}
    details = {"audio_layout": info["audio_layout"], "audio_streams": len(info["audio"]),
               "subtitle_streams": len(info["subtitles"]), "chapters": len(info["chapters"])}
    ctx.db.record_event(row["id"], "reprobe", row["stage"], "info", started, details,
                        flow_run_id=flow_run_id, task_run_id=task_run_id)
    return {"id": row["id"], "outcome": "ok", **details}


def _wav_path(ctx: Context, media_id: int) -> Path:
    return ctx.settings.work_dir / "audio" / f"{media_id}.wav"


def extract_audio(ctx: Context, row: dict) -> Outcome:
    video = Path(row["path"])
    s = ctx.settings
    if sidecars.load_existing_sidecar(video, row["fingerprint"], s.whisper_model,
                                      s.reuse_legacy_sidecars) is not None:
        return Outcome(Stage.AUDIO_EXTRACTED, {"reused_sidecar": True})
    info = row.get("media_info")
    if not info:  # probed before media_info existed
        _, info = store_probe(ctx, row)
    sel = select_audio_stream(info)
    if sel is None:
        return Outcome(Stage.SKIPPED, {"reason": "no audio stream"})
    wav = _wav_path(ctx, row["id"])
    wav.parent.mkdir(parents=True, exist_ok=True)
    marker = wav.with_suffix(".stream")  # which track the cached WAV came from
    cached = (wav.exists() and wav.stat().st_size > 0 and marker.exists()
              and marker.read_text() == str(sel["index"]))
    if not cached:
        tmp = wav.with_suffix(".part.wav")
        run_checked(extract_audio_cmd(video, tmp, sel["index"]), ctx.runner)
        tmp.replace(wav)
        marker.write_text(str(sel["index"]))
    lang = "en" if sel["english"] else sel.get("language")
    ctx.db.update_media_fields(row["id"], audio_path=str(wav), audio_stream_index=sel["index"],
                               audio_language=lang, audio_layout=sel.get("layout"))
    return Outcome(Stage.AUDIO_EXTRACTED, {
        "wav": str(wav), "bytes": wav.stat().st_size, "stream_index": sel["index"],
        "stream_language": sel.get("language"), "english": sel["english"],
        "selection": sel["reason"], "layout": sel.get("layout"),
        "audio_streams": len(info.get("audio") or [])})


def transcribe(ctx: Context, row: dict) -> Outcome:
    video = Path(row["path"])
    s = ctx.settings
    existing = sidecars.load_existing_sidecar(video, row["fingerprint"], s.whisper_model,
                                              s.reuse_legacy_sidecars)
    if existing is not None:
        result, reused = result_from_dict(existing), True
    else:
        wav = Path(row["audio_path"]) if row.get("audio_path") else None
        if wav is None or not wav.exists():
            raise FileNotFoundError(f"extracted audio missing for media {row['id']}")
        # English track chosen -> force 'en'; otherwise configured language / autodetect
        lang = "en" if is_english(row.get("audio_language")) else s.whisper_language
        result, reused = ctx.transcriber.transcribe(wav, language=lang), False
    chunks = chunk_segments(result.segments, s.segment_chunk_seconds)
    ctx.db.save_segments(row["id"], chunks, row["fingerprint"], result.language,
                         s.whisper_model, result.duration)
    if s.write_sidecars and (not reused or existing.get("legacy")):  # upgrade legacy format
        try:
            sidecars.write_sidecars(video, result_to_dict(result, row["fingerprint"],
                                                          s.whisper_model))
        except OSError as exc:  # read-only media mount: keep going, note it
            log.warning("sidecar write failed", extra={"media_id": row["id"], "error": str(exc)})
    if row.get("audio_path"):
        Path(row["audio_path"]).unlink(missing_ok=True)
        Path(row["audio_path"]).with_suffix(".stream").unlink(missing_ok=True)
    if not chunks:
        return Outcome(Stage.SKIPPED, {"reason": "no speech detected",
                                       "language": result.language})
    return Outcome(Stage.TRANSCRIBED, {"segments": len(chunks), "language": result.language,
                                       "reused_sidecar": reused})


def embed(ctx: Context, row: dict) -> Outcome:
    s = ctx.settings
    segs = ctx.db.segments_for_media(row["id"])
    ctx.vectors.ensure_collection()
    ctx.windows.ensure_collection()
    ctx.vectors.delete_media(row["id"])
    ctx.windows.delete_media(row["id"])
    base = {"movie_id": row["id"], "title": row.get("title"), "path": row["path"]}
    vecs = ctx.ollama.embed([g["text"] for g in segs])
    ctx.vectors.upsert([g["id"] for g in segs], vecs,
                       [base | {"seq": g["seq"], "start": g["start_s"], "end": g["end_s"]}
                        for g in segs])
    wins = make_windows(segs, s.window_size, s.window_stride)
    wvecs = ctx.ollama.embed([w.text for w in wins])
    ctx.windows.upsert([window_point_id(row["id"], w.index) for w in wins], wvecs,
                       [base | {"first_seq": w.first_seq, "last_seq": w.last_seq,
                                "start": w.start, "end": w.end} for w in wins])
    return Outcome(Stage.EMBEDDED, {"segments": len(segs), "windows": len(wins)})


def catalog(ctx: Context, row: dict) -> Outcome:
    s = ctx.settings
    path = Path(row["path"])
    rel = str(path.relative_to(s.media_root)) if path.is_relative_to(s.media_root) else str(path)
    parsed = identify(ctx.ollama, rel, row.get("probe") or {"duration": row.get("duration_s")}, {})
    match, score = jellyfin_lookup(ctx.jellyfin, parsed)
    sug = make_suggestion(path, parsed, match, score, s.media_root, s.canonical_roots,
                          s.video_min_confidence, s.video_review_confidence)
    sid = ctx.db.save_suggestion(sug)
    if match is not None:
        ctx.db.update_media_fields(row["id"], jellyfin_id=match.get("Id"))
    details = {"suggestion_id": sid, "confidence": sug.confidence, "decision": sug.decision,
               "jellyfin_id": (match or {}).get("Id"), "op": sug.op}
    if sug.decision == "suggest":
        return Outcome(Stage.CATALOGED, details)
    return Outcome(Stage.NEEDS_REVIEW, details)


ACTION_FUNCS: dict[str, Callable[[Context, dict], Outcome]] = {
    "probe": probe, "extract_audio": extract_audio, "transcribe": transcribe,
    "embed": embed, "catalog": catalog,
}


def reprobe_all(ctx: Context, only_missing: bool = True, limit: int | None = None,
                batch: int = 200) -> dict[str, int]:
    """Re-probe items already past ``discovered`` (stages are left untouched)."""
    stats = {"ok": 0, "failed": 0}
    after = 0
    while True:
        rows = ctx.db.reprobe_candidates(batch, only_missing, after)
        if not rows:
            break
        for r in rows:
            after = r["id"]
            stats[reprobe(ctx, r)["outcome"]] += 1
            if limit and sum(stats.values()) >= limit:
                return stats
        log.info("reprobe progress", extra=stats)
    return stats
