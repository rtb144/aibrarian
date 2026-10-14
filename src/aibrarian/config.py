"""Central configuration, loaded from environment variables (prefix ``AIB_``).

Every service in the stack reads the same :class:`Settings` object so there is
exactly one place to look when changing paths, endpoints or thresholds.

Fields that existed in the old scripts also accept their legacy env var name
(``OLLAMA_URL``, ``QDRANT_COLLECTION``, ``BOOK_MIN_CONFIDENCE`` ...) so an old
``.env`` keeps working; ``AIB_*`` wins when both are set.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

VIDEO_EXTENSIONS = frozenset(
    {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".webm", ".ts", ".m2ts", ".wmv", ".mpg", ".mpeg"}
)
BOOK_EXTENSIONS = frozenset({".epub", ".pdf", ".mobi", ".azw3", ".cbz", ".cbr"})
AUDIO_EXTENSIONS = frozenset({".mp3", ".m4a", ".m4b", ".flac", ".ogg", ".opus", ".aac", ".wav", ".alac"})


def _env(name: str, *legacy: str) -> AliasChoices:
    return AliasChoices(f"AIB_{name.upper()}", *legacy)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIB_", env_file=".env", extra="ignore")

    # --- storage -----------------------------------------------------------
    database_url: str = Field(
        "postgresql://aibrarian:aibrarian@postgres:5432/aibrarian",
        validation_alias=_env("database_url", "CATALOG_DSN"))
    qdrant_url: str = Field("http://qdrant:6333", validation_alias=_env("qdrant_url", "QDRANT_URL"))
    qdrant_collection: str = Field(
        "movie_segments", validation_alias=_env("qdrant_collection", "QDRANT_COLLECTION"))
    window_collection: str = Field(
        "movie_windows", validation_alias=_env("window_collection", "WINDOW_COLLECTION"))
    window_size: int = 10  # segments per sliding window (movie_windows.py --size)
    window_stride: int = 5

    # --- models ------------------------------------------------------------
    ollama_url: str = Field("http://ollama:11434", validation_alias=_env("ollama_url", "OLLAMA_URL"))
    embed_model: str = Field(
        "nomic-embed-text", validation_alias=_env("embed_model", "EMBED_MODEL"))
    embed_dim: int = Field(768, validation_alias=_env("embed_dim", "EMBED_DIM"))
    llm_model: str = Field("qwen3:14b", validation_alias=_env("llm_model", "OLLAMA_MODEL"))
    llm_timeout_s: float = 300.0

    whisper_model: str = Field(
        "large-v3", validation_alias=_env("whisper_model", "WHISPER_MODEL"))
    whisper_device: str = "cuda"
    whisper_compute_type: str = "float16"
    whisper_language: str | None = None  # None = autodetect

    # --- media layout (container paths) ------------------------------------
    media_root: Path = Field(Path("/media"), validation_alias=_env("media_root", "MEDIA_ROOT"))
    ol_delta_dir: Path = Field(Path("/openlibrary/delta"),
                               validation_alias=_env("ol_delta_dir", "OL_DELTA_DIR"))
    ol_index_path: Path = Field(Path("/data/olindex/openlibrary.sqlite"),
                                validation_alias=_env("ol_index_path", "OL_INDEX_PATH"))
    report_dir: Path = Field(Path("/data/reports"), validation_alias=_env("report_dir", "REPORT_DIR"))
    transcribe_roots: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["01-movies", "02-tvshows", "05-instructional_videos"]
    )
    library_roots: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["01-movies", "02-tvshows", "03-music", "downloads"]
    )
    canonical_roots: dict[str, str] = Field(default_factory=lambda: {
        "movie": "01-movies", "tv": "02-tvshows", "music": "03-music",
        "instructional": "05-instructional_videos"})
    book_source_dirs: Annotated[list[Path], NoDecode] = Field(
        default_factory=lambda: [Path("/media/book_drop"), Path("/media/downloads")],
        validation_alias=_env("book_source_dirs", "BOOK_SOURCE_DIRS"))
    # duplicate quarantine root (dated subfolders); default <media_root>/_quarantine.
    # Must be on the same filesystem as media_root so moves are instant renames.
    quarantine_dir: Path | None = Field(
        None, validation_alias=_env("quarantine_dir", "QUARANTINE_DIR"))
    dedup_auto_enabled: bool = Field(
        True, validation_alias=_env("dedup_auto_enabled", "DEDUP_AUTO_ENABLED"))
    dedup_batch_limit: int = Field(  # extra copies per weekly run; 0 = unlimited
        50, validation_alias=_env("dedup_batch_limit", "DEDUP_BATCH_LIMIT"))
    quarantine_retention_days: int = Field(
        30, validation_alias=_env("quarantine_retention_days", "QUARANTINE_RETENTION_DAYS"))
    clips_dir: Path = Field(Path("/data/clips"), validation_alias=_env("clips_dir", "OUT_DIR"))
    work_dir: Path = Path("/media/ffmpeg_workspace/aibrarian")  # temp WAVs
    calibre_dir: Path = Path("/books")
    audiobooks_dir: Path = Path("/audiobookshelf/audiobooks")
    podcasts_dir: Path = Path("/audiobookshelf/podcasts")

    # --- jellyfin ----------------------------------------------------------
    jellyfin_url: str = Field(
        "http://host.docker.internal:8096", validation_alias=_env("jellyfin_url", "JELLYFIN_URL"))
    jellyfin_api_key: str = Field(
        "", validation_alias=_env("jellyfin_api_key", "JELLYFIN_API_KEY"))
    google_books_api_key: str = ""  # optional; unauthenticated requests are rate-limited
    # Google Books is off by default: the local Open Library index is the reference source
    google_books_enabled: bool = Field(
        False, validation_alias=_env("google_books_enabled", "GOOGLE_BOOKS_ENABLED"))

    # --- profiling (Pyroscope) ---------------------------------------------
    pyroscope_enabled: bool = Field(
        True, validation_alias=_env("pyroscope_enabled", "PYROSCOPE_ENABLED"))
    pyroscope_url: str = Field(
        "http://pyroscope:4040", validation_alias=_env("pyroscope_url", "PYROSCOPE_URL"))
    pyroscope_app_name: str = Field(
        "aibrarian", validation_alias=_env("pyroscope_app_name", "PYROSCOPE_APP_NAME"))

    # --- behaviour ---------------------------------------------------------
    transcribe_batch_size: int = Field(5, validation_alias=_env("transcribe_batch_size", "BATCH_SIZE"))
    transcribe_interval_s: int = Field(
        300, validation_alias=_env("transcribe_interval_s", "SCAN_INTERVAL_S"))
    discover_interval_s: int = 1800  # separate 'discover' deployment
    reuse_legacy_sidecars: bool = True  # reuse old transcriber.py sidecars (no fingerprint)
    write_sidecars: bool = True  # <video>.transcript.json + <video>.srt next to the file
    segment_chunk_seconds: float = 30.0
    clip_fuzzy_threshold: int = Field(
        85, validation_alias=_env("clip_fuzzy_threshold", "FUZZY_MIN_SCORE"))
    clip_padding_s: float = Field(30.0, validation_alias=_env("clip_padding_s", "CLIP_PAD_SECONDS"))
    book_mode: str = "suggest"  # suggest | auto
    book_min_confidence: float = Field(
        0.85, validation_alias=_env("book_min_confidence", "BOOK_MIN_CONFIDENCE"))
    book_review_confidence: float = Field(
        0.60, validation_alias=_env("book_review_confidence", "BOOK_REVIEW_THRESHOLD"))
    # book-autoapply (Prefect, hourly :50): applies open book suggestions >= this
    book_auto_apply_enabled: bool = Field(
        True, validation_alias=_env("book_auto_apply_enabled", "BOOK_AUTO_APPLY_ENABLED"))
    book_auto_batch_limit: int = Field(  # suggestions per run; 0 = unlimited
        50, validation_alias=_env("book_auto_batch_limit", "BOOK_AUTO_BATCH_LIMIT"))
    calibredb_bin: str = Field(
        "calibredb", validation_alias=_env("calibredb_bin", "AIB_CALIBREDB", "CALIBREDB"))
    book_auto_min_confidence: float = Field(
        0.90, validation_alias=_env("book_auto_min_confidence", "BOOK_AUTO_MIN_CONFIDENCE"))
    video_min_confidence: float = 0.85  # auto-catalog Jellyfin match threshold
    video_review_confidence: float = 0.60

    @field_validator("transcribe_roots", "library_roots", "book_source_dirs", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip().startswith("["):
            return [p.strip() for p in v.split(",") if p.strip()]
        return v

    @field_validator("book_mode")
    @classmethod
    def _mode(cls, v: str) -> str:
        if v not in {"suggest", "auto"}:
            raise ValueError("book_mode must be 'suggest' or 'auto'")
        return v

    def transcribe_paths(self) -> list[Path]:
        return [self.media_root / r for r in self.transcribe_roots]

    def library_paths(self) -> list[Path]:
        return [self.media_root / r for r in self.library_roots]

    def quarantine_root(self) -> Path:
        return self.quarantine_dir or self.media_root / "_quarantine"


@lru_cache
def get_settings() -> Settings:
    return Settings()
