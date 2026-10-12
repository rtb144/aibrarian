"""Tiny Jellyfin API client (read-only).

Auth uses the ``Authorization: MediaBrowser ... Token="<key>"`` header. The
``api_key`` query parameter and ``X-Emby-Token`` header are rejected (401) by
the Jellyfin on z8g4, so they are never sent.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from aibrarian import __version__

from .config import Settings

log = logging.getLogger(__name__)


def auth_header(api_key: str, version: str = __version__) -> str:
    def q(v: str) -> str:
        return v.replace('"', "")

    return (f'MediaBrowser Client="aibrarian", Device="aibrarian", DeviceId="aibrarian", '
            f'Version="{q(version)}", Token="{q(api_key)}"')


class JellyfinClient:
    def __init__(self, base_url: str, api_key: str = "", client: httpx.Client | None = None):
        self.api_key = api_key
        self.http = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=20.0)
        if api_key:
            self.http.headers["Authorization"] = auth_header(api_key)

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
            if r.status_code == 401:
                log.warning("jellyfin rejected the API key (401)")
            r.raise_for_status()
            return r.json().get("Items", [])
        except httpx.HTTPError:
            return []

    def refresh_library(self) -> bool:
        try:
            return self.http.post("/Library/Refresh").status_code in (200, 204)
        except httpx.HTTPError:
            return False

    def check(self) -> str:
        """``ok`` | ``no_api_key`` | ``unauthorized`` | ``http_<code>`` | ``unreachable``.

        Uses the authenticated ``/System/Info`` so a bad key is reported, not
        just reachability."""
        if not self.api_key:
            return "no_api_key"
        try:
            code = self.http.get("/System/Info").status_code
        except httpx.HTTPError:
            return "unreachable"
        return "ok" if code == 200 else "unauthorized" if code in (401, 403) else f"http_{code}"

    def ping(self) -> bool:
        return self.check() == "ok"
