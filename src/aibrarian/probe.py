"""ffprobe / ffmpeg / mutagen helpers (subprocess runner injectable for tests)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


def run_ffprobe(path: Path, runner=subprocess.run, timeout: int = 60) -> dict[str, Any]:
    cmd = ["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
           "-show_streams", str(path)]
    out = runner(cmd, check=True, capture_output=True, timeout=timeout)
    return json.loads(out.stdout.decode("utf-8", errors="ignore") or "{}")


def compact_probe(probe: dict[str, Any]) -> dict[str, Any]:
    """Small summary stored in Postgres and sent to the LLM (from librarian.py)."""
    fmt = probe.get("format", {}) or {}
    return {
        "duration": _f(fmt.get("duration")),
        "bit_rate": _i(fmt.get("bit_rate")),
        "format": fmt.get("format_name"),
        "streams": [
            {k: s.get(k) for k in ("codec_type", "codec_name", "width", "height", "channels")}
            | {"language": (s.get("tags") or {}).get("language")}
            for s in (probe.get("streams") or [])[:8]
        ],
    }


def has_audio(probe: dict[str, Any]) -> bool:
    return any(s.get("codec_type") == "audio" for s in probe.get("streams") or [])


def duration_of(probe: dict[str, Any]) -> float | None:
    return _f((probe.get("format") or {}).get("duration"))


def extract_audio_cmd(src: Path, dst: Path) -> list[str]:
    """16 kHz mono WAV - the exact input whisper wants; decoding once on CPU keeps
    the GPU busy only with inference."""
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst)]


def read_audio_tags(path: Path) -> dict[str, Any]:
    """Easy tags via mutagen (optional dependency); {} when unavailable."""
    try:
        from mutagen import File as MutagenFile
    except ImportError:
        return {}
    try:
        media = MutagenFile(str(path), easy=True)
    except Exception as exc:  # corrupt file etc.
        return {"error": str(exc)}
    tags = dict(media.tags or {}) if media else {}

    def first(name: str) -> str | None:
        v = tags.get(name)
        if isinstance(v, list):
            return str(v[0]) if v else None
        return str(v) if v else None

    return {"artist": first("artist"), "album": first("album"), "title": first("title"),
            "tracknumber": first("tracknumber"), "date": first("date") or first("year")}


def _f(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> int | None:
    f = _f(v)
    return int(f) if f is not None else None
