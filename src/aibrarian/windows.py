"""Sliding-window semantic index (successor of movie_windows.py).

Windows of ``size`` consecutive segments advancing by ``stride`` are embedded
into a second Qdrant collection (``movie_windows``) so searches for a scene
or topic (rather than a single line) land on a coherent span. Point ids are
deterministic (``media_id * 1_000_000 + window_index``) so re-indexing is
idempotent.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


@dataclass
class Window:
    index: int
    first_seq: int
    last_seq: int
    start: float
    end: float
    text: str


def make_windows(segments: Sequence[dict[str, Any]], size: int, stride: int) -> list[Window]:
    if size < 1 or stride < 1:
        raise ValueError("size and stride must be >= 1")
    out: list[Window] = []
    n = len(segments)
    if n == 0:
        return out
    starts = list(range(0, max(n - size, 0) + 1, stride))
    if starts[-1] + size < n:  # make sure the tail is covered
        starts.append(n - size)
    for i, st in enumerate(starts):
        chunk = segments[st:st + size]
        out.append(Window(i, chunk[0]["seq"], chunk[-1]["seq"], chunk[0]["start_s"],
                          chunk[-1]["end_s"], " ".join(c["text"] for c in chunk)))
    return out


def window_point_id(media_id: int, index: int) -> int:
    return media_id * 1_000_000 + index


def merge_hits(hits: Sequence[dict[str, Any]], gap_s: float = 5.0) -> list[dict[str, Any]]:
    """Merge overlapping/adjacent window hits from the same media into spans,
    keeping the best score. ``hits`` items need payload movie_id/start/end."""
    by_media: dict[int, list[dict[str, Any]]] = {}
    for h in hits:
        p = h["payload"]
        by_media.setdefault(p["movie_id"], []).append(
            {"movie_id": p["movie_id"], "start": p["start"], "end": p["end"],
             "score": h["score"], "title": p.get("title"), "path": p.get("path")})
    merged: list[dict[str, Any]] = []
    for items in by_media.values():
        items.sort(key=lambda x: x["start"])
        cur = dict(items[0])
        for it in items[1:]:
            if it["start"] <= cur["end"] + gap_s:
                cur["end"] = max(cur["end"], it["end"])
                cur["score"] = max(cur["score"], it["score"])
            else:
                merged.append(cur)
                cur = dict(it)
        merged.append(cur)
    return sorted(merged, key=lambda x: x["score"], reverse=True)
