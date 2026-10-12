"""Fuzzy title resolution against *searchable* media (items with transcript segments).

Case/punctuation-insensitive, typo tolerant (rapidfuzz), optional year:
``"22 jump stret (2014)"`` -> media 42 "22 Jump Street" (2014).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from rapidfuzz import fuzz

_YEAR = re.compile(r"[\s(\[]*((?:19|20)\d{2})[)\]]?\s*$")
_ARTICLES = re.compile(r"^(the|a|an)\s+")

MIN_SCORE = 80.0      # below: not a match
MARGIN = 6.0          # top must beat the runner-up (different title) by this much


def norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return _ARTICLES.sub("", s)


def split_year(q: str) -> tuple[str, int | None]:
    m = _YEAR.search(q or "")
    if m and m.start() > 0:
        return q[: m.start()].strip(), int(m.group(1))
    return (q or "").strip(), None


def _score(nq: str, nt: str) -> float:
    if not nq or not nt:
        return 0.0
    if nq == nt:
        return 100.0
    s = fuzz.WRatio(nq, nt)
    if nt.startswith(nq):  # prefix typing ("22 jump") ranks high but below exact
        s = max(s, 90.0 + 9.0 * len(nq) / len(nt))
    return min(float(s), 99.0)


def rank(query: str, titles: list[dict[str, Any]], limit: int = 10,
         min_score: float = 50.0) -> list[dict[str, Any]]:
    """Score ``titles`` (dicts with media_id, title, year) for ``query``."""
    text, year = split_year(query)
    nq = norm(text)
    out = []
    for t in titles:
        s = _score(nq, norm(t.get("title")))
        if year is not None and t.get("year") is not None:
            s += 2.0 if int(t["year"]) == year else -15.0
        if s >= min_score:
            out.append({"media_id": t["media_id"], "title": t.get("title"),
                        "year": t.get("year"), "score": round(min(s, 100.0), 1)})
    out.sort(key=lambda r: (-r["score"], r["title"] or "", r["year"] or 0))
    return out[:limit]


def label(t: dict[str, Any]) -> str:
    return f"{t['title']} ({t['year']})" if t.get("year") else str(t.get("title"))


@dataclass
class Resolution:
    match: dict[str, Any] | None
    suggestions: list[dict[str, Any]] = field(default_factory=list)
    reason: str = "ok"   # ok | ambiguous | not_found


def resolve(query: str, titles: list[dict[str, Any]], min_score: float = MIN_SCORE,
            margin: float = MARGIN) -> Resolution:
    ranked = rank(query, titles, limit=10, min_score=0.0)
    sugg = [r for r in ranked if r["score"] >= 40.0][:5]
    if not ranked or ranked[0]["score"] < min_score:
        return Resolution(None, sugg, "not_found")
    top = ranked[0]
    rivals = [r for r in ranked[1:] if r["score"] > top["score"] - margin]
    if rivals:
        return Resolution(None, [top, *rivals][:5], "ambiguous")
    return Resolution(top, sugg)


class TitleNotResolved(LookupError):
    def __init__(self, query: str, res: Resolution):
        self.query, self.reason, self.suggestions = query, res.reason, res.suggestions
        super().__init__(f"title {query!r} {res.reason.replace('_', ' ')}")
