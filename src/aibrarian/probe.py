"""ffprobe / ffmpeg / mutagen helpers (subprocess runner injectable for tests)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


class ToolError(RuntimeError):
    """ffprobe/ffmpeg failed; message carries the tool's stderr."""


def run_checked(cmd: list[str], runner=subprocess.run, **kw):
    try:
        return runner(cmd, check=True, capture_output=True, **kw)
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or b"")
        err = err.decode("utf-8", errors="replace") if isinstance(err, bytes) else str(err)
        tail = " | ".join(line.strip() for line in err.strip().splitlines()[-5:])[-1500:]
        raise ToolError(f"{cmd[0]} exit {exc.returncode}: {tail or 'no stderr'}") from exc


def run_ffprobe(path: Path, runner=subprocess.run, timeout: int = 60) -> dict[str, Any]:
    cmd = ["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
           "-show_streams", "-show_chapters", str(path)]
    out = run_checked(cmd, runner, timeout=timeout)
    return json.loads(out.stdout.decode("utf-8", errors="ignore") or "{}")


ENGLISH = {"eng", "en", "english", "en-us", "en-gb"}

# ffmpeg channel_layout names -> human label
_LAYOUT_LABELS = {
    "mono": "mono", "stereo": "stereo", "2.1": "2.1", "3.0": "3.0", "3.0(back)": "3.0",
    "3.1": "3.1", "4.0": "4.0", "quad": "4.0", "quad(side)": "4.0", "4.1": "4.1",
    "5.0": "5.0", "5.0(side)": "5.0", "5.1": "5.1", "5.1(side)": "5.1", "6.0": "6.0",
    "6.0(front)": "6.0", "hexagonal": "6.0", "6.1": "6.1", "6.1(back)": "6.1",
    "6.1(front)": "6.1", "7.0": "7.0", "7.0(front)": "7.0", "7.1": "7.1", "7.1(wide)": "7.1",
    "7.1(wide-side)": "7.1", "7.1(top)": "7.1", "octagonal": "8.0", "5.1.2": "5.1.2",
    "5.1.4": "5.1.4", "7.1.2": "7.1.2", "7.1.4": "7.1.4", "downmix": "stereo",
    "22.2": "22.2",
}
_COUNT_LABELS = {1: "mono", 2: "stereo", 3: "2.1", 4: "4.0", 5: "5.0", 6: "5.1", 7: "6.1",
                 8: "7.1"}


def layout_label(channel_layout: str | None, channels: int | None) -> str | None:
    """Normalize an audio layout: ``5.1(side)`` -> ``5.1``; fall back to channel count."""
    if channel_layout:
        key = channel_layout.strip().lower()
        if key in _LAYOUT_LABELS:
            return _LAYOUT_LABELS[key]
        base = key.split("(")[0]
        if base in _LAYOUT_LABELS:
            return _LAYOUT_LABELS[base]
    if channels:
        return _COUNT_LABELS.get(int(channels), f"{int(channels)}ch")
    return None


def _rate(v: Any) -> float | None:
    """'24000/1001' -> 23.976; '0/0' -> None."""
    if not v or not isinstance(v, str):
        return _f(v)
    if "/" in v:
        a, b = v.split("/", 1)
        try:
            return round(float(a) / float(b), 3) if float(b) else None
        except ValueError:
            return None
    return _f(v)


def _disp(s: dict[str, Any], *keys: str) -> dict[str, bool]:
    d = s.get("disposition") or {}
    return {k: bool(d.get(k)) for k in keys}


def _hdr(s: dict[str, Any]) -> dict[str, Any]:
    side = [x for x in s.get("side_data_list") or [] if isinstance(x, dict)]
    types = [x.get("side_data_type") for x in side if x.get("side_data_type")]
    dovi = next((x for x in side if "DOVI" in str(x.get("side_data_type", "")).upper()
                 or "dolby vision" in str(x.get("side_data_type", "")).lower()), None)
    transfer = s.get("color_transfer")
    fmt = None
    if dovi:
        fmt = "dolby_vision"
    elif transfer == "smpte2084":
        fmt = "hdr10+" if any("HDR10+" in t or "SMPTE2094" in t.upper() for t in types) else "hdr10"
    elif transfer == "arib-std-b67":
        fmt = "hlg"
    out: dict[str, Any] = {"hdr_format": fmt, "side_data_types": types}
    if dovi:
        out["dolby_vision"] = {k: dovi.get(k) for k in (
            "dv_profile", "dv_level", "rpu_present_flag", "el_present_flag", "bl_present_flag",
            "dv_bl_signal_compatibility_id") if k in dovi}
    mastering = next((x for x in side if "Mastering" in str(x.get("side_data_type"))), None)
    if mastering:
        out["mastering_display"] = {k: v for k, v in mastering.items() if k != "side_data_type"}
    cll = next((x for x in side if "Content light" in str(x.get("side_data_type"))), None)
    if cll:
        out["content_light"] = {k: v for k, v in cll.items() if k != "side_data_type"}
    return out


def stream_info(s: dict[str, Any]) -> dict[str, Any]:
    tags = s.get("tags") or {}
    base = {"index": s.get("index"), "codec_type": s.get("codec_type"),
            "codec_name": s.get("codec_name"), "language": tags.get("language"),
            "title": tags.get("title")}
    kind = s.get("codec_type")
    if kind == "video":
        return base | {
            "profile": s.get("profile"), "width": s.get("width"), "height": s.get("height"),
            "pix_fmt": s.get("pix_fmt"), "bit_rate": _i(s.get("bit_rate")),
            "r_frame_rate": _rate(s.get("r_frame_rate")),
            "avg_frame_rate": _rate(s.get("avg_frame_rate")),
            "color_space": s.get("color_space"), "color_transfer": s.get("color_transfer"),
            "color_primaries": s.get("color_primaries"), "color_range": s.get("color_range"),
            "attached_pic": bool((s.get("disposition") or {}).get("attached_pic")),
            **_disp(s, "default"), **_hdr(s)}
    if kind == "audio":
        ch = _i(s.get("channels"))
        return base | {
            "profile": s.get("profile"), "channels": ch, "channel_layout": s.get("channel_layout"),
            "layout": layout_label(s.get("channel_layout"), ch),
            "sample_rate": _i(s.get("sample_rate")), "bit_rate": _i(s.get("bit_rate")),
            **_disp(s, "default", "forced", "comment", "visual_impaired", "dub", "original")}
    if kind == "subtitle":
        return base | _disp(s, "default", "forced", "hearing_impaired")
    return base


def media_info(probe: dict[str, Any]) -> dict[str, Any]:
    """Full metadata stored in ``aib.media_file.media_info`` (all streams, chapters)."""
    fmt = probe.get("format", {}) or {}
    streams = [stream_info(s) for s in probe.get("streams") or []]
    info = {
        "container": {"format": fmt.get("format_name"), "duration": _f(fmt.get("duration")),
                      "bit_rate": _i(fmt.get("bit_rate")), "size": _i(fmt.get("size")),
                      "tags": fmt.get("tags") or {}},
        "video": [x for x in streams if x["codec_type"] == "video" and not x.get("attached_pic")],
        "audio": [x for x in streams if x["codec_type"] == "audio"],
        "subtitles": [x for x in streams if x["codec_type"] == "subtitle"],
        "other": [x for x in streams if x["codec_type"] not in ("video", "audio", "subtitle")],
        "chapters": [{"start": _f(c.get("start_time")), "end": _f(c.get("end_time")),
                      "title": (c.get("tags") or {}).get("title")}
                     for c in probe.get("chapters") or []],
    }
    sel = select_audio_stream(info)
    info["audio_layout"] = sel["layout"] if sel else None
    return info


def llm_view(info: dict[str, Any]) -> dict[str, Any]:
    """Small summary for prompts and the ``probe`` column (keeps tokens low)."""
    v = (info.get("video") or [{}])[0]
    return {
        "duration": info["container"]["duration"],
        "format": info["container"]["format"],
        "title_tag": (info["container"].get("tags") or {}).get("title"),
        "video": {k: v.get(k) for k in ("codec_name", "width", "height", "hdr_format")} if v else None,
        "audio": [f"{a.get('language') or 'und'}:{a.get('codec_name')}:{a.get('layout')}"
                  for a in info.get("audio", [])][:6],
        "subtitles": sorted({s.get("language") or "und" for s in info.get("subtitles", [])}),
        "chapters": len(info.get("chapters", [])),
    }


def compact_probe(probe: dict[str, Any]) -> dict[str, Any]:
    """Backwards-compatible: LLM view of raw ffprobe output."""
    return llm_view(media_info(probe))


def is_english(lang: str | None) -> bool:
    return bool(lang) and lang.strip().lower() in ENGLISH


def select_audio_stream(info: dict[str, Any]) -> dict[str, Any] | None:
    """English default > first English > default > first audio stream.

    Returns the stream dict plus ``english`` (bool) and ``reason``."""
    audio = [a for a in info.get("audio") or [] if a.get("index") is not None]
    if not audio:
        return None
    # commentary / audio-description tracks go last so the main mix wins
    audio.sort(key=lambda a: bool(a.get("comment") or a.get("visual_impaired")))
    eng = [a for a in audio if is_english(a.get("language"))]
    for cand, reason in (
        (next((a for a in eng if a.get("default")), None), "english_default"),
        (eng[0] if eng else None, "english_first"),
        (next((a for a in audio if a.get("default")), None), "default"),
        (audio[0], "first"),
    ):
        if cand is not None:
            return cand | {"english": reason.startswith("english"), "reason": reason}
    return None


def has_audio(probe: dict[str, Any]) -> bool:
    return any(s.get("codec_type") == "audio" for s in probe.get("streams") or [])


def duration_of(probe: dict[str, Any]) -> float | None:
    return _f((probe.get("format") or {}).get("duration"))


def extract_audio_cmd(src: Path, dst: Path, stream_index: int | None = None) -> list[str]:
    """16 kHz mono WAV - the exact input whisper wants; decoding once on CPU keeps
    the GPU busy only with inference. ``stream_index`` (absolute ffprobe index)
    selects the track via ``-map 0:<index>``; the source file is never modified."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src)]
    if stream_index is not None:
        cmd += ["-map", f"0:{int(stream_index)}"]
    return cmd + ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst)]


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
