"""Hybrid transcript search: Qdrant semantic hits fused with Postgres FTS."""

from __future__ import annotations

from .db import Database
from .models import SearchHit
from .ollama import OllamaClient
from .vectors import VectorStore


def rrf_fuse(rankings: list[list[int]], k: int = 60) -> dict[int, float]:
    """Reciprocal rank fusion over lists of ids (best first)."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank + 1)
    return scores


class SearchService:
    def __init__(self, db: Database, vectors: VectorStore, ollama: OllamaClient):
        self.db, self.vectors, self.ollama = db, vectors, ollama

    def search(self, query: str, limit: int = 10, mode: str = "hybrid",
               media_id: int | None = None) -> list[SearchHit]:
        rankings: list[list[int]] = []
        if mode in ("hybrid", "semantic"):
            hits = self.vectors.search(self.ollama.embed_query(query), limit * 2, media_id)
            rankings.append([int(h["id"]) for h in hits])
        if mode in ("hybrid", "keyword"):
            kw = self.db.keyword_search(query, limit * 2, media_id)
            rankings.append([int(r["id"]) for r in kw])
        scores = rrf_fuse(rankings)
        top = sorted(scores, key=scores.__getitem__, reverse=True)[:limit]
        rows = {r["id"]: r for r in self.db.segments_by_ids(top)}
        return [
            SearchHit(segment_id=i, media_id=rows[i]["media_id"], path=rows[i]["path"],
                      title=rows[i]["title"], start=rows[i]["start_s"], end=rows[i]["end_s"],
                      text=rows[i]["text"], score=round(scores[i], 6))
            for i in top if i in rows
        ]
