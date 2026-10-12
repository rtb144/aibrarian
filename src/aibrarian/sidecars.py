"""Transcript sidecars next to the video (``<stem>.transcript.json`` + ``<stem>.srt``),
written atomically with the video's ownership, plus SRT cataloguing helpers.

Kept from the old transcriber: Jellyfin picks up the .srt automatically, and an
existing valid sidecar is reused instead of re-running whisper.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .config import VIDEO_EXTENSIONS

SIDECAR_VERSION = 1


def transcript_path(video: Path) -> Path:
    return video.with_suffix(".transcript.json")


def srt_path(video: Path) -> Path:
    return video.with_suffix(".srt")


def srt_time(t: float) -> str:
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(segments: list[dict[str, Any]]) -> str:
    blocks = [f"{i}\n{srt_time(s['start'])} --> {srt_time(s['end'])}\n{s['text'].strip()}\n"
              for i, s in enumerate(segments, 1)]
    return "\n".join(blocks)


def write_atomic(path: Path, text: str, like: Path | None = None) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        if like is not None and like.exists():
            st = like.stat()
            with contextlib.suppress(PermissionError):
                os.chown(tmp, st.st_uid, st.st_gid)
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_sidecars(video: Path, transcript: dict[str, Any]) -> tuple[Path, Path]:
    tp, sp = transcript_path(video), srt_path(video)
    write_atomic(tp, json.dumps({"version": SIDECAR_VERSION, **transcript}, ensure_ascii=False),
                 like=video)
    write_atomic(sp, to_srt(transcript["segments"]), like=video)
    return tp, sp


def load_existing_sidecar(video: Path, fingerprint: str, model: str,
                          accept_legacy: bool = True) -> dict[str, Any] | None:
    """Return the sidecar transcript if it matches this file version + model.

    Legacy sidecars written by the old ``transcriber.py`` carry no fingerprint;
    with ``accept_legacy`` they are reused when they are newer than the video
    and contain a segment list - this avoids re-transcribing the whole library
    after migration.
    """
    tp = transcript_path(video)
    if not tp.exists():
        return None
    try:
        data = json.loads(tp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("segments"), list):
        return None
    if "fingerprint" not in data:
        try:
            newer = tp.stat().st_mtime >= video.stat().st_mtime
        except OSError:
            newer = False
        if accept_legacy and newer and all(
                isinstance(x, dict) and {"start", "end", "text"} <= x.keys()
                for x in data["segments"]):
            return data | {"legacy": True}
        return None
    if data.get("fingerprint") != fingerprint or data.get("model") != model:
        return None
    return data


# --- SRT catalogue (successor of catalog_srt_files.py) ---------------------


def split_stem_parts(stem: str) -> tuple[str, str | None]:
    """``Movie (2000).en.forced`` -> (``Movie (2000)``, ``en.forced``)."""
    parts = stem.split(".")
    hints: list[str] = []
    while len(parts) > 1 and re.fullmatch(r"[a-z]{2,3}(-[A-Za-z]{2})?|forced|sdh|cc|default",
                                           parts[-1], re.IGNORECASE):
        hints.insert(0, parts.pop())
    return ".".join(parts), (".".join(hints) or None)


def find_sidecar_video(srt: Path) -> Path | None:
    base, _ = split_stem_parts(srt.stem)
    for ext in VIDEO_EXTENSIONS:
        cand = srt.with_name(base + ext)
        if cand.exists():
            return cand
    return None


def catalog_srt_files(media_root: Path) -> Iterator[dict[str, Any]]:
    for dirpath, dirnames, filenames in os.walk(media_root):
        dirnames[:] = [d for d in dirnames if d != "_quarantine"]
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if not fn.lower().endswith(".srt"):
                continue
            p = Path(dirpath) / fn
            rel = p.relative_to(media_root)
            video = find_sidecar_video(p)
            yield {"srt": str(p), "root": rel.parts[0] if len(rel.parts) > 1 else "",
                   "lang_hint": split_stem_parts(p.stem)[1],
                   "video": str(video) if video else None, "paired": video is not None,
                   "size": p.stat().st_size}


def related_sidecars(video: Path) -> list[Path]:
    """All sidecars that must travel with ``video`` on move/rename."""
    out = []
    stem = video.stem
    if not video.parent.is_dir():
        return out
    for p in video.parent.iterdir():
        if p == video or not p.is_file():
            continue
        if p.name.startswith(stem + ".") and (
            p.suffix.lower() in {".srt", ".ass", ".sub", ".vtt", ".nfo"}
            or p.name.endswith(".transcript.json")
        ):
            out.append(p)
    return sorted(out)
