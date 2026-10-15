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
    # quality signals (None when unknown, e.g. reused legacy sidecars)
    language_probability: float | None = None
    avg_logprob_mean: float | None = None
    max_compression_ratio: float | None = None

    def quality(self) -> dict:
        return {"language_probability": self.language_probability,
                "avg_logprob_mean": self.avg_logprob_mean,
                "max_compression_ratio": self.max_compression_ratio}


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
            load_wav(path), language=language or self.language, vad_filter=True, beam_size=5,
            word_timestamps=True)
        raw: list[RawSegment] = []
        logprobs: list[float] = []
        ratios: list[float] = []
        for s in segs:
            if getattr(s, "avg_logprob", None) is not None:
                logprobs.append(float(s.avg_logprob))
            if getattr(s, "compression_ratio", None) is not None:
                ratios.append(float(s.compression_ratio))
            if s.text.strip():
                raw.append(RawSegment(s.start, s.end, s.text.strip(),
                                      [(w.start, w.end, w.word.strip()) for w in (s.words or [])]))
        lp = getattr(info, "language_probability", None)
        return TranscriptResult(raw, info.language, info.duration,
                                language_probability=None if lp is None else float(lp),
                                avg_logprob_mean=sum(logprobs) / len(logprobs) if logprobs else None,
                                max_compression_ratio=max(ratios) if ratios else None)


def load_wav(path: Path):
    """Decode our 16 kHz mono PCM WAV to float32 without PyAV.

    faster-whisper's own ``decode_audio`` calls ``av.open(..., metadata_errors=...)``,
    which newer PyAV releases reject (TypeError). Handing it a numpy array skips
    that code path entirely. Anything that is not 16 kHz mono s16 is returned as a
    path string (faster-whisper decodes it itself)."""
    import wave

    import numpy as np

    try:
        with wave.open(str(path), "rb") as w:
            if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
                return str(path)
            frames = w.readframes(w.getnframes())
    except (wave.Error, EOFError):
        return str(path)
    return np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0


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


def result_to_dict(r: TranscriptResult, fingerprint: str, model: str,
                   compute_type: str | None = None,
                   audio_stream_index: int | None = None,
                   audio_language: str | None = None) -> dict:
    """Sidecar JSON shape (raw whisper segments, word timings included).
    ``audio_stream_index``/``audio_language``: the ffprobe track transcribed."""
    extra = {} if audio_stream_index is None else {
        "audio_stream_index": audio_stream_index, "audio_language": audio_language}
    if compute_type:
        extra["compute_type"] = compute_type
    return {"fingerprint": fingerprint, "model": model, "language": r.language,
            "duration": r.duration, **extra,
            **({"quality": r.quality()} if any(v is not None for v in r.quality().values()) else {}),
            "segments": [{"start": s.start, "end": s.end, "text": s.text,
                          "words": [list(w) for w in (s.words or [])]} for s in r.segments]}


def _word(w) -> tuple[float, float, str]:
    if isinstance(w, dict):  # legacy sidecars: {"start", "end", "word"}
        return (float(w["start"]), float(w["end"]), str(w.get("word", w.get("text", ""))).strip())
    return (float(w[0]), float(w[1]), str(w[2]))


def result_from_dict(d: dict) -> TranscriptResult:
    q = d.get("quality") if isinstance(d.get("quality"), dict) else {}
    return TranscriptResult(
        [RawSegment(float(s["start"]), float(s["end"]), str(s["text"]).strip(),
                    [_word(w) for w in s.get("words") or []] or None) for s in d["segments"]],
        d.get("language"), d.get("duration"),
        language_probability=q.get("language_probability"),
        avg_logprob_mean=q.get("avg_logprob_mean"),
        max_compression_ratio=q.get("max_compression_ratio"))
