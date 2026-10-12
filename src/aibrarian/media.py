"""Filesystem scanning and filename parsing.

Fingerprints are cheap (size + mtime + head/tail sample hash) so a full rescan
of a multi-TB library is fast; content change re-triggers transcription.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable, Iterator
from pathlib import Path

from .config import AUDIO_EXTENSIONS, BOOK_EXTENSIONS, VIDEO_EXTENSIONS
from .models import MediaFile, MediaKind

SAMPLE_BYTES = 1 << 20  # 1 MiB from head and tail

_EPISODE_RE = re.compile(r"[Ss](\d{1,2})[ ._-]?[Ee](\d{1,3})")
_ALT_EPISODE_RE = re.compile(r"\b(\d{1,2})x(\d{2,3})\b")
_YEAR_RE = re.compile(r"[\(\[ ._-]((?:19|20)\d{2})(?:[\)\] ._-]|$)")
_JUNK_RE = re.compile(
    r"\b(2160p|1080p|720p|480p|4k|uhd|hdr|x264|x265|h264|h265|hevc|bluray|brrip|web-?dl|"
    r"webrip|dvdrip|remux|aac|ac3|dts|atmos|proper|repack|extended|10bit)\b.*$",
    re.IGNORECASE,
)


def classify(path: Path) -> MediaKind:
    ext = path.suffix.lower()
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    if ext in BOOK_EXTENSIONS:
        return "book"
    return "other"


def fingerprint(path: Path, size: int | None = None, mtime: float | None = None) -> str:
    st = path.stat()
    size = st.st_size if size is None else size
    mtime = st.st_mtime if mtime is None else mtime
    h = hashlib.blake2b(digest_size=16)
    h.update(f"{size}:{int(mtime)}".encode())
    with path.open("rb") as fh:
        h.update(fh.read(SAMPLE_BYTES))
        if size > 2 * SAMPLE_BYTES:
            fh.seek(-SAMPLE_BYTES, os.SEEK_END)
            h.update(fh.read(SAMPLE_BYTES))
    return h.hexdigest()


def parse_name(name: str) -> dict[str, object]:
    """Best-effort title/year/season/episode extraction from a file or dir name."""
    stem = Path(name).stem if Path(name).suffix.lower() in (
        VIDEO_EXTENSIONS | AUDIO_EXTENSIONS | BOOK_EXTENSIONS) else name
    out: dict[str, object] = {"title": None, "year": None, "season": None, "episode": None}
    cut = len(stem)
    m = _EPISODE_RE.search(stem) or _ALT_EPISODE_RE.search(stem)
    if m:
        out["season"], out["episode"] = int(m.group(1)), int(m.group(2))
        cut = min(cut, m.start())
    y = _YEAR_RE.search(stem)
    if y:
        out["year"] = int(y.group(1))
        cut = min(cut, y.start())
    title = _JUNK_RE.sub("", stem[:cut])
    title = re.sub(r"[._]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip(" -[]()")
    out["title"] = title or None
    return out


def iter_files(root: Path, kinds: Iterable[MediaKind] | None = None) -> Iterator[Path]:
    wanted = set(kinds) if kinds else None
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for fn in sorted(filenames):
            if fn.startswith("."):
                continue
            p = Path(dirpath) / fn
            if wanted is None or classify(p) in wanted:
                yield p


def scan_root(media_root: Path, root_name: str,
              kinds: Iterable[MediaKind] | None = None,
              known: dict[str, tuple[int, float, str]] | None = None,
              progress=None, progress_every: int = 500) -> list[MediaFile]:
    """Walk one root. ``known`` maps path -> (size, mtime, fingerprint) from the
    catalog; files whose size and mtime are unchanged reuse the stored
    fingerprint instead of reading 2 MiB from disk, so a re-scan of an
    already-catalogued library is just a directory walk + stat()."""
    root = media_root / root_name
    if not root.is_dir():
        return []
    known = known or {}
    results: list[MediaFile] = []
    for n, p in enumerate(iter_files(root, kinds), 1):
        try:
            st = p.stat()
            prev = known.get(str(p))
            if prev is not None and prev[0] == st.st_size and abs(prev[1] - st.st_mtime) < 1e-3:
                fp = prev[2]
            else:
                fp = fingerprint(p, st.st_size, st.st_mtime)
        except OSError:
            continue
        if progress is not None and n % progress_every == 0:
            progress(root_name, n)
        # TV shows: prefer the show directory name as title
        rel = p.relative_to(root)
        meta = parse_name(p.name)
        if meta["season"] is not None and len(rel.parts) > 1:
            show = parse_name(rel.parts[0])
            meta["title"] = show["title"] or meta["title"]
        elif len(rel.parts) > 1 and not meta["title"]:
            meta["title"] = parse_name(rel.parts[0])["title"]
        results.append(
            MediaFile(path=p, root=root_name, kind=classify(p), size_bytes=st.st_size,
                      mtime=st.st_mtime, fingerprint=fp, **meta)
        )
    return results
