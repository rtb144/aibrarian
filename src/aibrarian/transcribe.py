"""faster-whisper wrapper and transcript chunking (used by the ``transcribe`` action)."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import Settings
from .models import Segment

log = logging.getLogger(__name__)


@dataclass
class RawSegment:
    start: float
    end: float
    text: str
    words: list[tuple[float, float, str]] | None = None


@dataclass
class TranscriptResult:
    segments: list[RawSegment]
    language: str | None
    duration: float | None


class Transcriber(Protocol):
    model_name: str

    def transcribe(self, path: Path, language: str | None = None) -> TranscriptResult: ...


class WhisperTranscriber:
    """faster-whisper wrapper; model is loaded lazily once per process."""

    def __init__(self, model_name: str, device: str = "cuda",
                 compute_type: str = "float16", language: str | None = None):
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self._model = None

    @classmethod
    def from_settings(cls, s: Settings) -> WhisperTranscriber:
        return cls(s.whisper_model, s.whisper_device, s.whisper_compute_type, s.whisper_language)

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel  # heavy, GPU-only image

            log.info("loading whisper %s on %s", self.model_name, self.device)
            self._model = WhisperModel(self.model_name, device=self.device,
                                       compute_type=self.compute_type)
        return self._model

    def transcribe(self, path: Path, language: str | None = None) -> TranscriptResult:
        """``language`` overrides the configured one (None = autodetect)."""
        segs, info = self._load().transcribe(
            str(path), language=language or self.language, vad_filter=True, beam_size=5,
            word_timestamps=True)
        raw = [
            RawSegment(s.start, s.end, s.text.strip(),
                       [(w.start, w.end, w.word.strip()) for w in (s.words or [])])
            for s in segs if s.text.strip()
        ]
        return TranscriptResult(raw, info.language, info.duration)


def chunk_segments(raw: Iterable[RawSegment], target_seconds: float) -> list[Segment]:
    """Merge consecutive whisper segments into chunks of ~``target_seconds``.

    Chunks never split a whisper segment, so timestamps stay exact.
    """
    chunks: list[Segment] = []
    buf: list[RawSegment] = []

    def flush() -> None:
        if buf:
            words = [w for s in buf for w in (s.words or [])]
            chunks.append(Segment(seq=len(chunks), start=buf[0].start, end=buf[-1].end,
                                  text=" ".join(s.text for s in buf), words=words or None))
            buf.clear()

    for seg in raw:
        if buf and seg.end - buf[0].start > target_seconds:
            flush()
        buf.append(seg)
    flush()
    return chunks


def result_to_dict(r: TranscriptResult, fingerprint: str, model: str) -> dict:
    """Sidecar JSON shape (raw whisper segments, word timings included)."""
    return {"fingerprint": fingerprint, "model": model, "language": r.language,
            "duration": r.duration,
            "segments": [{"start": s.start, "end": s.end, "text": s.text,
                          "words": [list(w) for w in (s.words or [])]} for s in r.segments]}


def _word(w) -> tuple[float, float, str]:
    if isinstance(w, dict):  # legacy sidecars: {"start", "end", "word"}
        return (float(w["start"]), float(w["end"]), str(w.get("word", w.get("text", ""))).strip())
    return (float(w[0]), float(w[1]), str(w[2]))


def result_from_dict(d: dict) -> TranscriptResult:
    return TranscriptResult(
        [RawSegment(float(s["start"]), float(s["end"]), str(s["text"]).strip(),
                    [_word(w) for w in s.get("words") or []] or None) for s in d["segments"]],
        d.get("language"), d.get("duration"))
