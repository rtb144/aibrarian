"""Video/music librarian: identify files with the LLM, match to Jellyfin and
suggest canonical placement (successor of librarian.py + apply_* planning).

Identification uses the same strict ``MEDIA_SCHEMA`` and prompt rules as the
old librarian so results are comparable. Nothing is moved here; placement is
recorded as an ``aib.suggestion`` and applied via :mod:`aibrarian.fileops`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from .jellyfin import JellyfinClient
from .media import parse_name
from .models import Decision, Suggestion
from .ollama import OllamaClient
from .probe import compact_probe

KINDS = ("movie", "tv", "music", "unknown")
MEDIA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "title": {"type": ["string", "null"]},
        "year": {"type": ["integer", "null"]},
        "series": {"type": ["string", "null"]},
        "season": {"type": ["integer", "null"]},
        "episode": {"type": ["integer", "null"]},
        "episode_title": {"type": ["string", "null"]},
        "artist": {"type": ["string", "null"]},
        "album": {"type": ["string", "null"]},
        "track": {"type": ["integer", "null"]},
        "track_title": {"type": ["string", "null"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "notes": {"type": "string"},
        "suggested_canonical_name": {"type": ["string", "null"]},
    },
    "required": ["kind", "title", "year", "series", "season", "episode", "episode_title",
                 "artist", "album", "track", "track_title", "confidence", "notes",
                 "suggested_canonical_name"],
    "additionalProperties": False,
}


def _to_int(v: Any) -> int | None:
    try:
        if v is None or isinstance(v, bool) or str(v).strip() == "":
            return None
        return int(float(v))
    except (TypeError, ValueError):
        return None


def safe_component(value: str) -> str:
    value = value.replace("/", "-").replace("\\", "-").replace(":", " -")
    value = re.sub(r'[<>"|?*\x00-\x1f]', "", value)
    return re.sub(r"\s+", " ", value).strip(" .")


def empty_result(note: str) -> dict[str, Any]:
    return {k: None for k in MEDIA_SCHEMA["required"]} | {
        "kind": "unknown", "confidence": 0.0, "notes": note}


def normalize_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return empty_result("LLM returned a non-object JSON value")
    out = empty_result("")
    out.update({k: value.get(k) for k in out})
    if out["kind"] not in KINDS:
        out["kind"] = "unknown"
    for k in ("year", "season", "episode", "track"):
        out[k] = _to_int(out[k])
    try:
        out["confidence"] = min(1.0, max(0.0, float(out["confidence"])))
    except (TypeError, ValueError):
        out["confidence"] = 0.0
    out["notes"] = str(out["notes"] or "")
    if out["suggested_canonical_name"]:
        out["suggested_canonical_name"] = safe_component(str(out["suggested_canonical_name"]))
    return out


def build_prompt(rel_path: str, basic: dict, tags: dict, probe: dict) -> str:
    return (
        "You are a strict media filename parser. Return exactly one JSON object and no markdown.\n"
        f"JSON Schema: {json.dumps(MEDIA_SCHEMA)}\n"
        "Rules: classify as movie, tv, music, or unknown; use null when evidence is absent; "
        "do not invent metadata. For movies use 'Title (Year).ext'; TV use "
        "'Series - SXXEYY - Episode Title.ext'; music use 'NN - Track Title.ext'. "
        "Use the existing extension and a confidence from 0 to 1.\n"
        f"PATH: {rel_path}\nBASIC: {json.dumps(basic)}\n"
        f"AUDIO_TAGS: {json.dumps(tags)}\nFFPROBE_SUMMARY: {json.dumps(compact_probe(probe))}"
    )


def identify(ollama: OllamaClient, rel_path: str, probe: dict, tags: dict) -> dict[str, Any]:
    basic = parse_name(Path(rel_path).name)
    try:
        return normalize_result(ollama.generate_json(build_prompt(rel_path, basic, tags, probe),
                                                     MEDIA_SCHEMA))
    except Exception as exc:  # LLM down / bad JSON -> unknown, goes to review
        return empty_result(f"LLM error: {exc}")


def best_match(parsed: dict[str, Any], candidates: list[dict[str, Any]]
               ) -> tuple[dict[str, Any] | None, float]:
    target = parsed.get("title") or parsed.get("series") or parsed.get("track_title") or ""
    best, score = None, 0.0
    for item in candidates:
        cur = fuzz.token_set_ratio(target, item.get("Name") or "") / 100.0
        if parsed.get("year") and item.get("ProductionYear") == parsed["year"]:
            cur = min(1.0, cur + 0.15)
        if cur > score:
            best, score = item, cur
    return best, round(score, 3)


def jellyfin_lookup(jf: JellyfinClient, parsed: dict[str, Any]
                    ) -> tuple[dict[str, Any] | None, float]:
    kind = parsed.get("kind")
    term = (parsed.get("series") if kind == "tv"
            else parsed.get("track_title") if kind == "music" else parsed.get("title"))
    return best_match(parsed, jf.search(term or ""))


def canonical_target(parsed: dict[str, Any], src: Path, media_root: Path,
                     roots: dict[str, str]) -> Path | None:
    """Canonical location (from apply_moves.py / apply_renames.py rules)."""
    ext = src.suffix
    kind = parsed.get("kind")
    if kind == "movie" and parsed.get("title"):
        name = safe_component(parsed["title"])
        if parsed.get("year"):
            name = f"{name} ({parsed['year']})"
        return media_root / roots["movie"] / name / f"{name}{ext}"
    if kind == "tv" and parsed.get("series") and parsed.get("season") is not None \
            and parsed.get("episode") is not None:
        series = safe_component(parsed["series"])
        fn = f"{series} - S{parsed['season']:02d}E{parsed['episode']:02d}"
        if parsed.get("episode_title"):
            fn += f" - {safe_component(parsed['episode_title'])}"
        return (media_root / roots["tv"] / series / f"Season {parsed['season']:02d}"
                / f"{fn}{ext}")
    if kind == "music" and parsed.get("artist") and parsed.get("track_title"):
        album = safe_component(parsed.get("album") or "Unknown Album")
        fn = safe_component(parsed["track_title"])
        if parsed.get("track"):
            fn = f"{parsed['track']:02d} - {fn}"
        return media_root / roots["music"] / safe_component(parsed["artist"]) / album / f"{fn}{ext}"
    return None


def decide(confidence: float, min_conf: float, review_conf: float) -> Decision:
    if confidence >= min_conf:
        return "suggest"
    if confidence >= review_conf:
        return "review"
    return "reject"


def make_suggestion(src: Path, parsed: dict[str, Any], match: dict[str, Any] | None,
                    match_score: float, media_root: Path, roots: dict[str, str],
                    min_conf: float, review_conf: float, run_id: int | None = None) -> Suggestion:
    target = canonical_target(parsed, src, media_root, roots)
    # combined confidence: LLM self-report, boosted when Jellyfin agrees
    conf = parsed["confidence"]
    if match is not None:
        conf = max(conf, (conf + match_score) / 2)
    op = "none" if target is None or target == src else (
        "rename" if target.parent == src.parent else "move")
    return Suggestion(
        domain="video", source_path=str(src), target_path=str(target) if target else None,
        metadata={"parsed": parsed, "jellyfin_id": (match or {}).get("Id"),
                  "jellyfin_name": (match or {}).get("Name"), "match_score": match_score},
        confidence=round(conf, 3), decision=decide(conf, min_conf, review_conf),
        rationale=parsed.get("notes"), media_kind=parsed.get("kind"), op=op, run_id=run_id)
