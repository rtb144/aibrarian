"""Qdrant vector store via its REST API (no heavy client dependency).

Compatible with the old ``vector_store.py`` contract: point id = Postgres
segment id, payload ``movie_id`` (= ``aib.media_file.id``) indexed as integer,
cosine distance, default collection ``movie_segments``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx

from .config import Settings


class VectorStore:
    def __init__(self, base_url: str, collection: str, dim: int,
                 client: httpx.Client | None = None):
        self.collection = collection
        self.dim = dim
        self.http = client or httpx.Client(base_url=base_url, timeout=60.0)

    @classmethod
    def from_settings(cls, s: Settings) -> VectorStore:
        return cls(s.qdrant_url, s.qdrant_collection, s.embed_dim)

    def ensure_collection(self) -> None:
        r = self.http.get(f"/collections/{self.collection}")
        if r.status_code == 200:
            return
        r = self.http.put(f"/collections/{self.collection}", json={
            "vectors": {"size": self.dim, "distance": "Cosine"}})
        r.raise_for_status()
        r = self.http.put(f"/collections/{self.collection}/index",
                          json={"field_name": "movie_id", "field_schema": "integer"})
        r.raise_for_status()

    def delete_media(self, media_id: int) -> None:
        r = self.http.post(f"/collections/{self.collection}/points/delete?wait=true", json={
            "filter": {"must": [{"key": "movie_id", "match": {"value": media_id}}]}})
        r.raise_for_status()

    def upsert(self, ids: Sequence[int], vectors: Sequence[Sequence[float]],
               payloads: Sequence[dict[str, Any]]) -> None:
        if not ids:
            return
        if not (len(ids) == len(vectors) == len(payloads)):
            raise ValueError("ids, vectors and payloads must be the same length")
        points = [{"id": i, "vector": list(v), "payload": p}
                  for i, v, p in zip(ids, vectors, payloads, strict=True)]
        for start in range(0, len(points), 256):
            r = self.http.put(f"/collections/{self.collection}/points?wait=true",
                              json={"points": points[start:start + 256]})
            r.raise_for_status()

    def search(self, vector: Sequence[float], limit: int = 10, media_id: int | None = None,
               min_score: float | None = None) -> list[dict[str, Any]]:
        body: dict[str, Any] = {"vector": list(vector), "limit": limit, "with_payload": True}
        if min_score is not None:
            body["score_threshold"] = min_score
        if media_id is not None:
            body["filter"] = {"must": [{"key": "movie_id", "match": {"value": media_id}}]}
        r = self.http.post(f"/collections/{self.collection}/points/search", json=body)
        r.raise_for_status()
        return r.json()["result"]

    def count(self, media_id: int | None = None) -> int:
        body: dict[str, Any] = {"exact": True}
        if media_id is not None:
            body["filter"] = {"must": [{"key": "movie_id", "match": {"value": media_id}}]}
        r = self.http.post(f"/collections/{self.collection}/points/count", json=body)
        r.raise_for_status()
        return int(r.json()["result"]["count"])

    def drop(self) -> bool:
        r = self.http.delete(f"/collections/{self.collection}")
        return r.status_code == 200

    def collections(self) -> list[str]:
        r = self.http.get("/collections")
        r.raise_for_status()
        return [c["name"] for c in r.json()["result"]["collections"]]

    def ping(self) -> bool:
        try:
            return self.http.get("/readyz").status_code == 200
        except httpx.HTTPError:
            return False
