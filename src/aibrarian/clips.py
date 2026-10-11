"""Quote -> clip extraction with ffmpeg."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz


@dataclass
class QuoteMatch:
    media_id: int
    seq: int
    start: float
    end: float
    text: str
    score: float


def best_quote_match(quote: str, segments: Sequence[dict[str, Any]],
                     threshold: int) -> QuoteMatch | None:
    """Find the segment (or adjacent pair, for quotes spanning a boundary) best
    matching ``quote`` using rapidfuzz partial ratio."""
    q = quote.lower().strip()
    best: QuoteMatch | None = None
    for i, seg in enumerate(segments):
        windows = [(seg, seg)]
        if i + 1 < len(segments):
            windows.append((seg, segments[i + 1]))
        for a, b in windows:
            text = a["text"] if a is b else f'{a["text"]} {b["text"]}'
            t = text.lower()
            # partial_ratio only when the window can contain the whole quote;
            # otherwise a short segment that is a prefix of the quote scores 100.
            score = fuzz.partial_ratio(q, t) if len(t) >= len(q) else fuzz.ratio(q, t)
            if best is None or score > best.score:
                best = QuoteMatch(a["media_id"], a["seq"], a["start_s"], b["end_s"], text, score)
    return best if best and best.score >= threshold else None


def _norm_word(w: str) -> str:
    return re.sub(r"[^\w']+", "", w.lower())


def best_word_match(phrase: str, segments: Sequence[dict[str, Any]], threshold: int,
                    match_index: int = 0) -> QuoteMatch | None:
    """Word-level phrase matching (precise timestamps), from movie_clipper.py.

    Slides a window of len(phrase) words over all word timings and returns the
    ``match_index``-th non-overlapping match scoring >= threshold (best first).
    Returns None when no word timings exist (caller falls back to segments).
    """
    words: list[tuple[float, float, str, int, int]] = []
    for seg in segments:
        for w in seg.get("words") or []:
            words.append((float(w[0]), float(w[1]), str(w[2]), seg["media_id"], seg["seq"]))
    target = [_norm_word(w) for w in phrase.split() if _norm_word(w)]
    if not words or not target:
        return None
    n = len(target)
    tgt = " ".join(target)
    cands: list[tuple[float, int]] = []
    for i in range(0, max(1, len(words) - n + 1)):
        window = " ".join(_norm_word(w[2]) for w in words[i:i + n])
        sc = fuzz.ratio(tgt, window)
        if sc >= threshold:
            cands.append((sc, i))
    picked: list[tuple[float, int]] = []
    for sc, i in sorted(cands, key=lambda x: (-x[0], x[1])):
        if all(abs(i - j) >= n for _, j in picked):
            picked.append((sc, i))
    if len(picked) <= match_index:
        return None
    sc, i = picked[match_index]
    span = words[i:i + n]
    return QuoteMatch(span[0][3], span[0][4], span[0][0], span[-1][1],
                      " ".join(w[2] for w in span), sc)


def safe_filename(s: str, max_len: int = 60) -> str:
    s = re.sub(r"[^\w\-]+", "_", s).strip("_")
    return s[:max_len] or "clip"


def build_ffmpeg_cmd(src: Path, dst: Path, start: float, end: float,
                     reencode: bool = True) -> list[str]:
    duration = max(0.1, end - start)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{duration:.3f}"]
    if reencode:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"]
    else:
        cmd += ["-c", "copy"]
    return cmd + [str(dst)]


def extract_clip(src: Path, out_dir: Path, name: str, start: float, end: float,
                 padding: float, duration_limit: float | None = None,
                 runner=subprocess.run) -> tuple[Path, float, float]:
    s = max(0.0, start - padding)
    e = end + padding
    if duration_limit:
        e = min(e, duration_limit)
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / f"{safe_filename(name)}_{int(s)}-{int(e)}.mp4"
    runner(build_ffmpeg_cmd(src, dst, s, e), check=True, capture_output=True)
    return dst, s, e
