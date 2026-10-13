"""Re-queue transcripts that came out non-English because whisper got a dubbed track.

Candidates come from ``aib.transcript_lang`` (``non_english`` or ``mismatch``,
``review`` not ``'correct'``). For each, the media file's audio tracks
(stored ``media_info``, or a fresh read-only ffprobe) are run through
:func:`aibrarian.probe.select_audio_stream`:

* ``requeue``      - an English track exists and differs from the one used
                     (or the used track is unknown, e.g. a reused legacy sidecar);
* ``untagged``     - several audio tracks, none tagged English
                     (requeued only with ``include_untagged``);
* ``keep``         - no English track and only one audio track: a real foreign
                     film, keep as-is;
* ``same_track``   - the English-tagged track was already used (mis-tagged file
                     or a real foreign-language film): nothing to gain.

Dry run unless ``execute``. Executing moves the old ``<stem>.transcript.json``
and ``<stem>.srt`` into ``<QUARANTINE_DIR>/<YYYY-MM-DD>/`` (``manifest.jsonl`` +
``manifest.tsv``, ``reason=transcript_requeue_lang``, logged to the dedup action
log; undo with ``duplicates restore --manifest``) - nothing is deleted - and
resets the item to stage ``probed`` with a ``reset`` event, so the normal
pipeline re-runs extract_audio -> transcribe -> embed -> catalog.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from aibrarian.probe import media_info, run_ffprobe, select_audio_stream
from aibrarian.quarantine import _qpath, _write_manifest, log_action
from aibrarian.sidecars import srt_path, transcript_path
from aibrarian.status import PIPELINE, Stage

log = logging.getLogger("aibrarian.requeue_lang")

REASON = "transcript_requeue_lang"
RESET_TO = Stage.PROBED
LEASE_SECONDS = 360 * 60  # Database.claim default lease
# stages at or past 'transcribed' (or parked after it) are safe to reset
RESETTABLE = {Stage.TRANSCRIBED, Stage.EMBEDDED, Stage.CATALOGED, Stage.NEEDS_REVIEW,
              Stage.SKIPPED, Stage.FAILED}


def used_stream(row: dict[str, Any], video: Path) -> int | None:
    """Track the existing transcript was made from, if known.

    The sidecar records it (newer pipeline); otherwise the DB column, but only
    for non-legacy sidecars (a reused legacy sidecar never ran extract_audio
    for real, so the DB value says nothing about it)."""
    tp = transcript_path(video)
    try:
        data = json.loads(tp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict):
        if data.get("audio_stream_index") is not None:
            return int(data["audio_stream_index"])
        if "fingerprint" not in data:
            return None  # legacy sidecar: unknown
    v = row.get("audio_stream_index")
    return int(v) if v is not None else None


def classify(row: dict[str, Any], info: dict[str, Any] | None,
             used: int | None) -> tuple[str, dict[str, Any] | None]:
    audio = (info or {}).get("audio") or []
    sel = select_audio_stream(info or {})
    if sel is None:
        return "no_audio", None
    if sel["english"]:
        if used is not None and used == sel["index"]:
            return "same_track", sel
        return "requeue", sel
    if len(audio) > 1:
        return "untagged", sel
    return "keep", sel


def _fmt_stream(a: dict[str, Any] | None) -> str:
    if not a:
        return "-"
    bits = [f"#{a.get('index')}", a.get("language") or "und"]
    if a.get("channels"):
        bits.append(f"{a['channels']}ch")
    if a.get("title"):
        bits.append(repr(a["title"])[:30])
    return " ".join(str(b) for b in bits)


def plan(ctx, include_untagged: bool = False, runner=None) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for r in ctx.db.langcheck_requeue_candidates():
        base = {"lang_id": r["lang_id"], "media_id": r.get("media_id"),
                "title": r.get("title"), "detected_lang": r.get("detected_lang"),
                "stored_lang": r.get("stored_lang"), "path": r.get("path"),
                "transcript": r.get("transcript_path"), "stage": r.get("stage")}
        if not r.get("path"):
            items.append(base | {"decision": "no_media", "used": None, "english": None})
            continue
        video = Path(r["path"])
        info = r.get("media_info")
        if isinstance(info, str):
            info = json.loads(info)
        if not info:
            try:
                info = media_info(run_ffprobe(video, runner=runner or ctx.runner))
            except Exception as exc:
                items.append(base | {"decision": "probe_error", "error": str(exc)[:200],
                                     "used": None, "english": None})
                continue
        used = used_stream(r, video)
        decision, sel = classify(r, info, used)
        if decision == "untagged" and include_untagged:
            decision = "requeue_untagged"
        audio = {a.get("index"): a for a in info.get("audio") or []}
        items.append(base | {
            "decision": decision, "used": used, "used_stream": _fmt_stream(audio.get(used)),
            "english": sel["index"] if sel and sel["english"] else None,
            "chosen": sel["index"] if sel else None, "chosen_stream": _fmt_stream(sel),
            "audio_streams": len(audio)})
    counts: dict[str, int] = {}
    for i in items:
        counts[i["decision"]] = counts.get(i["decision"], 0) + 1
    return {"items": items, "counts": counts, "include_untagged": include_untagged}


def _move(src: Path, batch: Path, roots: list[Path]) -> dict[str, str] | None:
    if not src.exists():
        return None
    dst = _qpath(batch, src, roots)
    if dst.exists():  # same file requeued twice in one day
        dst = dst.with_name(f"{dst.name}.{datetime.now(UTC).strftime('%H%M%S%f')}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.rename(src, dst)
    return {"from": str(src), "to": str(dst)}


def execute(ctx, p: dict[str, Any], source: str | None = "cli") -> dict[str, Any]:
    s = ctx.settings
    root = Path(s.quarantine_root())
    batch = root / date.today().isoformat()
    roots = [*s.transcribe_paths(), Path(s.media_root)]
    todo = [i for i in p["items"] if i["decision"] in ("requeue", "requeue_untagged")]
    results = []
    for it in todo:
        mid, video = it["media_id"], Path(it["path"])
        row = ctx.db.get_media(mid)
        stage = row and row.get("stage")
        if not row or row.get("missing") or Stage(stage) not in RESETTABLE:
            results.append(it | {"result": "skipped", "reason": f"stage {stage}"})
            continue
        locked_at = row.get("locked_at")
        if row.get("locked_by") and locked_at and (
                datetime.now(UTC) - locked_at).total_seconds() < LEASE_SECONDS:
            results.append(it | {"result": "skipped", "reason": f"locked by {row['locked_by']}"})
            continue
        started = datetime.now(UTC)
        try:
            moved = [m for m in (_move(transcript_path(video), batch, roots),
                                 _move(srt_path(video), batch, roots)) if m]
        except OSError as exc:
            results.append(it | {"result": "error", "error": str(exc)})
            continue
        ok = ctx.db.transition(mid, "reset", stage, str(RESET_TO), "ok", started, details={
            "reason": REASON, "detected_lang": it.get("detected_lang"),
            "used_stream": it.get("used"), "new_stream": it.get("chosen"),
            "moved": moved})
        if not ok:  # stage changed under us: put the transcript back, touch nothing
            for m in moved:
                if not Path(m["from"]).exists():
                    os.rename(m["to"], m["from"])
            results.append(it | {"result": "skipped", "reason": "stage changed concurrently"})
            continue
        ctx.db.update_media_fields(mid, audio_path=None)
        for m in moved:
            # media_id=None: 'duplicates restore' must not repoint/restage the media row
            rec = {"moved_at": started.isoformat(), "group": None, "media_id": None,
                   "transcript_media_id": mid, "size_bytes": Path(m["to"]).stat().st_size,
                   "keeper": None, "path": m["from"], "quarantine_path": m["to"],
                   "prev_stage": stage, "reason": REASON,
                   "detected_lang": it.get("detected_lang"), "used_stream": it.get("used"),
                   "new_stream": it.get("chosen")}
            _write_manifest(batch, rec)
            log_action(root, ctx.db, source, action="quarantine", media_id=mid,
                       file_path=m["from"], quarantine_path=m["to"],
                       size_bytes=rec["size_bytes"], reason=REASON)
        log.info("transcript requeued", extra={"media_id": mid, "title": it.get("title"),
                                               "detected_lang": it.get("detected_lang"),
                                               "used_stream": it.get("used"),
                                               "new_stream": it.get("chosen"),
                                               "moved": len(moved)})
        results.append(it | {"result": "requeued", "moved": moved})
    return {"requeued": sum(r["result"] == "requeued" for r in results),
            "manifest": str(batch / "manifest.jsonl") if results else None,
            "results": results}


_HDR = ("id", "title", "lang", "stage", "used stream", "english/chosen stream")


def _table(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["  (none)"]
    cells = [(str(r.get("media_id") or "-"), (r.get("title") or "?")[:40],
              r.get("detected_lang") or "?", r.get("stage") or "-",
              r.get("used_stream") or ("unknown" if r.get("used") is None else "-"),
              r.get("chosen_stream") or "-") for r in rows]
    w = [max(len(h), *(len(c[i]) for c in cells)) for i, h in enumerate(_HDR)]
    out = ["  " + "  ".join(h.ljust(w[i]) for i, h in enumerate(_HDR))]
    out += ["  " + "  ".join(c[i].ljust(w[i]) for i in range(len(_HDR))) for c in cells]
    return out


def format_plan(p: dict[str, Any], res: dict[str, Any] | None = None) -> str:
    by: dict[str, list[dict[str, Any]]] = {}
    for i in p["items"]:
        by.setdefault(i["decision"], []).append(i)
    rq = by.get("requeue", []) + by.get("requeue_untagged", [])
    lines = [f"{'RE-QUEUED' if res else 'WOULD RE-QUEUE (dry run, use --execute)'}: {len(rq)}",
             *_table(rq), "",
             f"KEEP AS-IS (no English track, single audio track - true foreign films): "
             f"{len(by.get('keep', []))}", *_table(by.get("keep", []))]
    if by.get("untagged"):
        lines += ["", f"MULTIPLE UNTAGGED TRACKS (not re-queued; --include-untagged): "
                  f"{len(by['untagged'])}", *_table(by["untagged"])]
    if by.get("same_track"):
        lines += ["", f"ENGLISH-TAGGED TRACK ALREADY USED (mis-tagged or real foreign; "
                  f"not re-queued): {len(by['same_track'])}", *_table(by["same_track"])]
    for k in ("no_audio", "no_media", "probe_error"):
        if by.get(k):
            lines += ["", f"{k.upper()}: {len(by[k])}",
                      *[f"  {i.get('title')}  {i.get('path') or i.get('transcript')}"
                        f"  {i.get('error', '')}" for i in by[k]]]
    lines += ["", "counts: " + ", ".join(f"{k}={v}" for k, v in sorted(p["counts"].items()))]
    if res:
        bad = [r for r in res["results"] if r["result"] != "requeued"]
        lines.append(f"requeued {res['requeued']} (reset to '{RESET_TO}'); "
                     f"old transcripts moved, manifest: {res['manifest']}")
        lines += [f"  NOT requeued: {r.get('title')}: {r['result']} {r.get('reason') or r.get('error') or ''}"
                  for r in bad]
    return "\n".join(lines) + "\n"


assert RESET_TO in PIPELINE
