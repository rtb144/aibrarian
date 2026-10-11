"""Backlog ETA (successor of movie_eta.py): throughput from gaps between
recent successful completions, robust to pauses via the median gap."""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from datetime import datetime


def estimate(completions: Sequence[datetime], remaining: int,
             max_gap_s: float = 6 * 3600) -> dict:
    gaps = [(b - a).total_seconds() for a, b in zip(completions, completions[1:], strict=False)]
    gaps = [g for g in gaps if 0 < g <= max_gap_s]  # ignore downtime
    if not gaps or remaining <= 0:
        return {"remaining": remaining, "samples": len(gaps), "eta_s": 0 if remaining <= 0
                else None, "median_gap_s": None}
    med = statistics.median(gaps)
    q = statistics.quantiles(gaps, n=4) if len(gaps) >= 4 else [med, med, med]
    return {"remaining": remaining, "samples": len(gaps), "median_gap_s": round(med, 1),
            "eta_s": round(med * remaining), "eta_low_s": round(q[0] * remaining),
            "eta_high_s": round(q[2] * remaining)}


def fmt(sec: float | None) -> str:
    if sec is None:
        return "unknown"
    sec = int(sec)
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m, _ = divmod(sec, 60)
    return (f"{d}d " if d else "") + f"{h}h {m}m"
