"""Plain data models shared across modules."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

MediaKind = Literal["video", "audio", "book", "other"]
Decision = Literal["auto", "suggest", "review", "reject"]


class MediaFile(BaseModel):
    path: Path
    root: str
    kind: MediaKind
    size_bytes: int
    mtime: float
    fingerprint: str
    title: str | None = None
    year: int | None = None
    season: int | None = None
    episode: int | None = None


class Segment(BaseModel):
    seq: int
    start: float
    end: float
    text: str
    words: list[tuple[float, float, str]] | None = None


class Suggestion(BaseModel):
    domain: Literal["video", "book"]
    source_path: str
    target_path: str | None
    metadata: dict[str, Any] = Field(default_factory=dict)
    confidence: float
    decision: Decision
    rationale: str | None = None
    media_kind: str | None = None
    op: Literal["move", "rename", "none"] = "move"
    run_id: int | None = None


class SearchHit(BaseModel):
    segment_id: int
    media_id: int
    path: str
    title: str | None
    start: float
    end: float
    text: str
    score: float
