"""Read-only duplicates report. Never deletes or moves anything.

* **identical**: same size -> same head/tail content sample -> same full sha256.
  (``media_file.fingerprint`` mixes in mtime, so it cannot detect copies; content
  hashes are cached in ``aib.file_hash`` keyed by path+size+mtime.)
* **review** (likely duplicates): same normalized title+year, or durations within
  ``duration_tol`` seconds when the files share a folder or have similar titles.

Each group gets a non-binding ``suggested_keep`` plus the sidecars that exist only
next to the other copies (so they can be preserved before anything is removed).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from .media import parse_name
from .sidecars import related_sidecars
from .titles import norm

log = logging.getLogger(__name__)
SAMPLE = 4 * 1024 * 1024
CHUNK = 8 * 1024 * 1024


def sample_hash(path: Path, size: int) -> str:
    h = hashlib.blake2b(digest_size=16)
    with path.open("rb") as fh:
        h.update(fh.read(SAMPLE))
        if size > 2 * SAMPLE:
            fh.seek(-SAMPLE, os.SEEK_END)
            h.update(fh.read(SAMPLE))
    return h.hexdigest()


def full_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


class HashCache:
    """Cache wrapper; ``store`` is any object with ``get_file_hash``/``put_file_hash``
    (the Database) or None for an in-memory cache."""

    def __init__(self, store=None):
        self.store, self.mem = store, {}
        self.computed = {"sample": 0, "sha256": 0}

    def _get(self, path: str, size: int, mtime: float) -> dict:
        if path in self.mem:
            return self.mem[path]
        row = self.store.get_file_hash(path) if self.store else None
        if not row or row["size_bytes"] != size or abs(row["mtime"] - mtime) > 1e-6:
            row = {"path": path, "size_bytes": size, "mtime": mtime, "sample": None, "sha256": None}
        self.mem[path] = row
        return row

    def _put(self, row: dict) -> None:
        if self.store:
            self.store.put_file_hash(row["path"], row["size_bytes"], row["mtime"],
                                     row["sample"], row["sha256"])

    def sample(self, path: str, size: int, mtime: float) -> str:
        row = self._get(path, size, mtime)
        if not row["sample"]:
            row["sample"] = sample_hash(Path(path), size)
            self.computed["sample"] += 1
            self._put(row)
        return row["sample"]

    def sha256(self, path: str, size: int, mtime: float) -> str:
        row = self._get(path, size, mtime)
        if not row["sha256"]:
            row["sha256"] = full_hash(Path(path))
            self.computed["sha256"] += 1
            self._put(row)
        return row["sha256"]


# -- describing a copy ---------------------------------------------------------

def _sidecar_suffix(video: Path, side: Path) -> str:
    return side.name[len(video.stem):]  # e.g. ".eng.srt"


def describe(row: dict[str, Any]) -> dict[str, Any]:
    p = Path(row["path"])
    info = row.get("media_info") or {}
    v = (info.get("video") or [{}])[0]
    try:
        sides = related_sidecars(p)
    except OSError:
        sides = []
    return {
        "media_id": row["id"], "path": str(p), "size_bytes": row["size_bytes"],
        "container": p.suffix.lower().lstrip("."), "duration_s": row.get("duration_s"),
        "title": row.get("title"), "year": row.get("year"),
        "video_codec": v.get("codec_name"), "width": v.get("width"), "height": v.get("height"),
        "hdr": v.get("hdr_format"),
        "bit_rate": (info.get("container") or {}).get("bit_rate"),
        "audio_layout": row.get("audio_layout") or info.get("audio_layout"),
        "audio_streams": len(info.get("audio") or []),
        "subtitle_streams": len(info.get("subtitles") or []),
        "sidecars": [_sidecar_suffix(p, s) for s in sides],
        "stage": row.get("stage"), "canonical_name": is_canonical(p),
    }


def is_canonical(p: Path) -> bool:
    """Jellyfin style ``Title (Year)/Title (Year).ext``."""
    return bool(re.fullmatch(r".+ \((19|20)\d{2}\)", p.stem)) and p.parent.name == p.stem


def keep_score(c: dict[str, Any]) -> tuple:
    return (c["canonical_name"], (c["width"] or 0) * (c["height"] or 0), c["bit_rate"] or 0,
            c["audio_streams"] + c["subtitle_streams"], len(c["sidecars"]), c["size_bytes"],
            -len(c["path"]))


def build_group(kind: str, reason: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    copies = [describe(r) for r in rows]
    copies.sort(key=keep_score, reverse=True)
    keep = copies[0]
    why = []
    if keep["canonical_name"]:
        why.append("canonical Jellyfin name")
    if all(keep_score(keep)[1] > keep_score(o)[1] for o in copies[1:]):
        why.append("highest resolution")
    have = set(keep["sidecars"])
    orphans = [{"from": o["path"], "sidecar": str(Path(o["path"]).with_name(Path(o["path"]).stem + s)),
                "would_be": str(Path(keep["path"]).with_name(Path(keep["path"]).stem + s))}
               for o in copies[1:] for s in o["sidecars"] if s not in have]
    others = sum(o["size_bytes"] for o in copies[1:])
    return {"kind": kind, "reason": reason, "count": len(copies),
            "suggested_keep": keep["path"], "keep_reason": ", ".join(why) or "best quality/metadata",
            "reclaimable_bytes": others, "sidecars_only_on_others": orphans, "copies": copies}


# -- grouping ------------------------------------------------------------------

def _title_key(r: dict[str, Any]) -> tuple[str, int | None] | None:
    title, year = r.get("title"), r.get("year")
    if not title:
        meta = parse_name(Path(r["path"]).stem)
        title, year = meta.get("title"), year or meta.get("year")
    t = norm(str(title or ""))
    return (t, year) if t else None


def find_exact(rows: list[dict[str, Any]], cache: HashCache,
               progress: Callable[[str], None] | None = None) -> list[list[dict[str, Any]]]:
    by_size: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        if r.get("size_bytes"):
            by_size[r["size_bytes"]].append(r)
    groups = []
    for size, same in by_size.items():
        if len(same) < 2:
            continue
        by_sample: dict[str, list[dict]] = defaultdict(list)
        for r in same:
            try:
                by_sample[cache.sample(r["path"], size, r["mtime"])].append(r)
            except OSError as exc:
                log.warning("hash failed", extra={"path": r["path"], "error": str(exc)})
        for cand in by_sample.values():
            if len(cand) < 2:
                continue
            by_full: dict[str, list[dict]] = defaultdict(list)
            for r in cand:
                if progress:
                    progress(r["path"])
                try:
                    by_full[cache.sha256(r["path"], size, r["mtime"])].append(r)
                except OSError as exc:
                    log.warning("hash failed", extra={"path": r["path"], "error": str(exc)})
            groups += [g for g in by_full.values() if len(g) > 1]
    return groups


def find_likely(rows: list[dict[str, Any]], exclude: Iterable[frozenset[int]] = (),
                duration_tol: float = 2.0) -> list[tuple[str, list[dict[str, Any]]]]:
    """Union-find over title+year matches and close durations (same folder or
    similar title). Groups identical to an exact group are dropped."""
    parent = {r["id"]: r["id"] for r in rows}
    reasons: dict[int, set[str]] = defaultdict(set)

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b, why):
        ra, rb = find(a["id"]), find(b["id"])
        if ra != rb:
            parent[ra] = rb
        reasons[a["id"]].add(why)
        reasons[b["id"]].add(why)

    by_title: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        k = _title_key(r)
        if k:
            by_title[k].append(r)
    for same in by_title.values():
        for o in same[1:]:
            union(same[0], o, "same title+year")
    timed = sorted((r for r in rows if r.get("duration_s")), key=lambda r: r["duration_s"])
    for i, a in enumerate(timed):
        for b in timed[i + 1:]:
            if b["duration_s"] - a["duration_s"] > duration_tol:
                break
            ka, kb = _title_key(a), _title_key(b)
            similar = bool(ka and kb and fuzz.token_set_ratio(ka[0], kb[0]) >= 80)
            if Path(a["path"]).parent == Path(b["path"]).parent or similar:
                union(a, b, f"duration within {duration_tol:g}s")
    clusters: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        clusters[find(r["id"])].append(r)
    skip = set(exclude)
    out = []
    for members in clusters.values():
        ids = frozenset(m["id"] for m in members)
        if len(members) < 2 or ids in skip:
            continue
        why = sorted(set().union(*(reasons[m["id"]] for m in members)))
        out.append((", ".join(why), members))
    return out


def report(rows: list[dict[str, Any]], cache: HashCache, exact_only: bool = False,
           duration_tol: float = 2.0) -> dict[str, Any]:
    rows = [r for r in rows if Path(r["path"]).exists()]
    exact = find_exact(rows, cache)
    exact_groups = [build_group("identical", "byte-identical (sha256)", g) for g in exact]
    likely_groups = []
    if not exact_only:
        likely_groups = [build_group("review", why, g) for why, g in
                         find_likely(rows, [frozenset(r["id"] for r in g) for g in exact],
                                     duration_tol)]
    for groups in (exact_groups, likely_groups):
        groups.sort(key=lambda g: -g["reclaimable_bytes"])
    return {
        "read_only": True,
        "note": "Nothing is deleted or moved. suggested_keep is advisory.",
        "summary": {
            "files_scanned": len(rows),
            "identical_groups": len(exact_groups),
            "identical_reclaimable_bytes": sum(g["reclaimable_bytes"] for g in exact_groups),
            "review_groups": len(likely_groups),
            "review_reclaimable_bytes": sum(g["reclaimable_bytes"] for g in likely_groups),
            "hashes_computed": dict(cache.computed),
        },
        "identical": exact_groups,
        "review": likely_groups,
    }


def to_tsv(rep: dict[str, Any]) -> str:
    cols = ["section", "group", "keep", "path", "size_bytes", "container", "video_codec",
            "resolution", "audio_layout", "audio_streams", "subtitle_streams", "sidecars", "reason"]
    lines = ["\t".join(cols)]
    for section in ("identical", "review"):
        for gi, g in enumerate(rep.get(section, []), 1):
            for c in g["copies"]:
                res = f"{c['width']}x{c['height']}" if c["width"] else ""
                lines.append("\t".join(str(x) for x in (
                    section, gi, "KEEP" if c["path"] == g["suggested_keep"] else "",
                    c["path"], c["size_bytes"], c["container"], c["video_codec"] or "", res,
                    c["audio_layout"] or "", c["audio_streams"], c["subtitle_streams"],
                    ",".join(c["sidecars"]), g["reason"])))
    return "\n".join(lines) + "\n"


# -- books ---------------------------------------------------------------------

def book_rows(dirs: Iterable[Path], exclude: Iterable[Path] = ()) -> list[dict[str, Any]]:
    """Book/audiobook files under ``dirs`` (Calibre, Audiobookshelf, book_drop, downloads)."""
    from .config import AUDIO_EXTENSIONS, BOOK_EXTENSIONS
    from .media import iter_files

    exts = BOOK_EXTENSIONS | AUDIO_EXTENSIONS
    seen, rows = set(), []
    for d in dirs:
        d = Path(d)
        if not d.is_dir():
            continue
        for p in iter_files(d, None, exclude):
            if p.suffix.lower() not in exts or str(p) in seen or p.is_symlink():
                continue
            seen.add(str(p))
            try:
                st = p.stat()
            except OSError:
                continue
            rows.append({"id": None, "path": str(p), "size_bytes": st.st_size,
                         "mtime": st.st_mtime, "title": None, "year": None,
                         "duration_s": None, "media_info": None, "stage": None})
    return rows


def book_groups(rows: list[dict[str, Any]], cache: HashCache,
                media_root: Path) -> list[dict[str, Any]]:
    """Identical book groups. Keeper: a copy inside the libraries (not under media_root),
    else the shortest path; only copies under media_root are movable to quarantine."""
    mr = Path(media_root).resolve()
    out = []
    for g in find_exact(rows, cache):
        def lib_first(r: dict) -> tuple:
            return (Path(r["path"]).resolve().is_relative_to(mr), len(r["path"]), r["path"])
        g = sorted(g, key=lib_first)
        keep = g[0]["path"]
        copies = []
        for r in g:
            try:
                sides = [s.name[len(Path(r["path"]).stem):] for s in related_sidecars(Path(r["path"]))]
            except OSError:
                sides = []
            copies.append({"media_id": None, "path": r["path"], "size_bytes": r["size_bytes"],
                           "container": Path(r["path"]).suffix.lower().lstrip("."),
                           "sidecars": sides, "stage": None, "width": None, "height": None,
                           "video_codec": None, "audio_layout": None, "audio_streams": 0,
                           "subtitle_streams": 0, "hdr": None,
                           "movable": Path(r["path"]).resolve().is_relative_to(mr)})
        movable = [c for c in copies[1:] if c["movable"]]
        out.append({"kind": "identical", "reason": "byte-identical book (sha256)",
                    "count": len(copies), "suggested_keep": keep,
                    "keep_reason": "copy inside the book library" if not copies[0]["movable"]
                    else "shortest path", "reclaimable_bytes": sum(c["size_bytes"] for c in movable),
                    "sidecars_only_on_others": [], "copies": copies})
    out.sort(key=lambda g: -g["reclaimable_bytes"])
    return out
