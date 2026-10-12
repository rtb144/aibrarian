from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from aibrarian.actions import Context
from aibrarian.config import Settings


class FakeDB:
    """In-memory stand-in for aibrarian.db.Database (only what tests need)."""

    def __init__(self) -> None:
        self.media: dict[int, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.segments: dict[int, list[dict[str, Any]]] = {}
        self.suggestions: list[Any] = []
        self.file_ops_log: list[tuple] = []

    def add(self, **row: Any) -> dict[str, Any]:
        mid = row.setdefault("id", len(self.media) + 1)
        row.setdefault("stage", "discovered")
        row.setdefault("stage_attempts", 0)
        row.setdefault("fingerprint", "fp")
        row.setdefault("title", "Movie")
        self.media[mid] = row
        return row

    def known_files(self, root):
        return {}

    @contextmanager
    def try_lock(self, key):
        yield True

    def get_media(self, mid: int):
        return dict(self.media[mid]) if mid in self.media else None

    def update_media_fields(self, mid: int, **f: Any) -> None:
        self.media[mid].update(f)

    def transition(self, mid, action, frm, to, outcome, started, **kw) -> bool:
        if self.media[mid]["stage"] != frm:
            return False
        self.media[mid]["stage"] = str(to)
        self.media[mid]["stage_attempts"] = 0 if outcome == "ok" else self.media[mid]["stage_attempts"]
        if outcome == "failed":
            self.media[mid]["failed_stage"] = frm
        self.events.append({"media_id": mid, "action": action, "from": frm, "to": str(to),
                            "outcome": outcome, **kw})
        return True

    def record_attempt_failure(self, mid, action, stage, started, error, attempt, *a) -> None:
        self.media[mid]["stage_attempts"] += 1
        self.events.append({"media_id": mid, "action": action, "outcome": "failed",
                            "attempt": attempt, "error": error})

    def save_segments(self, mid, chunks, fp, lang, model, dur):
        self.segments[mid] = [{"id": mid * 100 + c.seq, "media_id": mid, "seq": c.seq,
                               "start_s": c.start, "end_s": c.end, "text": c.text,
                               "words": c.words} for c in chunks]
        return [s["id"] for s in self.segments[mid]]

    def segments_for_media(self, mid):
        return self.segments.get(mid, [])

    def save_suggestion(self, s) -> int:
        self.suggestions.append(s)
        return len(self.suggestions)

    def record_file_op(self, *a) -> None:
        self.file_ops_log.append(a)


class FakeOllama:
    def __init__(self, reply: dict | None = None) -> None:
        self.reply = reply or {}
        self.embedded: list[list[str]] = []

    def embed(self, texts, prefix="search_document: "):
        self.embedded.append(list(texts))
        return [[float(len(t)), 0.0] for t in texts]

    def embed_query(self, text):
        return [1.0, 0.0]

    def generate_json(self, prompt, schema=None, num_ctx=4096):
        return dict(self.reply)

    def ping(self):
        return True


class FakeVectors:
    def __init__(self) -> None:
        self.points: dict[int, tuple] = {}
        self.deleted: list[int] = []

    def ensure_collection(self):
        pass

    def delete_media(self, mid):
        self.deleted.append(mid)

    def upsert(self, ids, vecs, payloads):
        for i, v, p in zip(ids, vecs, payloads, strict=True):
            self.points[i] = (v, p)

    def search(self, vec, limit=10, media_id=None, min_score=None):
        return [{"id": i, "score": 0.9, "payload": p} for i, (_, p) in list(self.points.items())[:limit]]

    def ping(self):
        return True


class FakeJellyfin:
    def __init__(self, items=None) -> None:
        self.items = items or []

    def search(self, term, types="", limit=20):
        return self.items

    def ping(self):
        return True


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, media_root=tmp_path / "media", work_dir=tmp_path / "work",
                    report_dir=tmp_path / "reports", clips_dir=tmp_path / "clips",
                    calibre_dir=tmp_path / "books", audiobooks_dir=tmp_path / "abs" / "audiobooks",
                    podcasts_dir=tmp_path / "abs" / "podcasts")


@pytest.fixture
def ctx(settings: Settings) -> Context:
    return Context(settings=settings, db=FakeDB(), ollama=FakeOllama(), vectors=FakeVectors(),
                   windows=FakeVectors(), jellyfin=FakeJellyfin(),
                   transcriber_factory=lambda: None)
