"""Tiny Jellyfin API client (read-only)."""

from __future__ import annotations

from typing import Any

import httpx

from .config import Settings


class JellyfinClient:
    def __init__(self, base_url: str, api_key: str = "", client: httpx.Client | None = None):
        headers = {"X-Emby-Token": api_key} if api_key else {}
        self.http = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=20.0,
                                           headers=headers)

    @classmethod
    def from_settings(cls, s: Settings) -> JellyfinClient:
        return cls(s.jellyfin_url, s.jellyfin_api_key)

    def search(self, term: str, types: str = "Movie,Series,Episode,Audio",
               limit: int = 20) -> list[dict[str, Any]]:
        if not term:
            return []
        try:
            r = self.http.get("/Items", params={"SearchTerm": term, "IncludeItemTypes": types,
                                                "Recursive": True, "Limit": limit,
                                                "Fields": "Path,ProductionYear"})
            r.raise_for_status()
            return r.json().get("Items", [])
        except httpx.HTTPError:
            return []

    def refresh_library(self) -> bool:
        try:
            return self.http.post("/Library/Refresh").status_code in (200, 204)
        except httpx.HTTPError:
            return False

    def ping(self) -> bool:
        try:
            return self.http.get("/System/Info/Public").status_code == 200
        except httpx.HTTPError:
            return False
