"""Read-only triage of the downloads folder (``aibrarian downloads triage``).

Every top-level entry of ``--root`` (and every child of numbered category folders such
as ``2-books``) is classified as one of :data:`CLASSES` with a suggested action. Nothing
is moved, renamed or written to the catalog: the catalog is read in one read-only
transaction (plain SELECTs, no advisory locks, so a running book-scan is unaffected),
Calibre's ``metadata.db`` is opened ``mode=ro``, archives are only listed. The only
writes are the JSON/CSV report files.

Matching (cheapest first; content is hashed only when a size matches, capped by
``max_hash``):

* video -> ``aib.media_file`` under 01-movies / 02-tvshows: exact size + head/tail sample
  hash (``aib.file_hash.sample`` when cached), else exact size, else normalized
  title+year (+season/episode) with size tolerance. Library copy is compared by
  resolution (``media_info``/``probe`` height vs. ``720p``/``1080p`` in the name), else size.
* book -> Calibre: applied suggestion whose target exists, a logged move
  (``file_op``/``change_log``) into Calibre, exact size + hash of a Calibre format,
  ISBN, or title+author.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import tarfile
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz, process

from .config import AUDIO_EXTENSIONS, BOOK_EXTENSIONS, VIDEO_EXTENSIONS
from .duplicates import sample_hash
from .media import parse_name
from .progress import ProgressReporter
from .titles import norm

CLASSES = ("in_library_movie", "in_library_book", "moved_already", "pending_book",
           "pending_video", "archive", "software", "audio", "junk", "unknown")
ARCHIVE_EXT = {".zip", ".rar", ".7z", ".gz", ".tgz", ".tar", ".bz2", ".xz"}
SOFTWARE_EXT = {".exe", ".msi", ".dmg", ".iso", ".pkg", ".apk", ".deb"}
JUNK_NAMES = {"desktop.ini", "thumbs.db", ".ds_store", "$recycle.bin", "@eadir",
              "system volume information"}
AD_EXT = {".txt", ".png", ".jpg", ".jpeg", ".gif", ".url", ".nfo", ".lnk", ".html", ".htm",
          ".website"}
AD_RE = re.compile(
    r"downloaded\s+from|torrent|tgx|galaxy|www\.|\.(com|to|org|net|cc|me)\b|upcoming\s+rel|"
    r"xclusive|yts|yify|rarbg|ettv|eztv|1337x|free\s+audiobook|visit|read\s*me|"
    r"rarbg|proof|^info$|^source$", re.I)
SAMPLE_MAX = 200 * 1024 ** 2
SIZE_TOL = 0.15          # title+year match: sizes within 15% count as "within tolerance"
CATEGORY_RE = re.compile(r"^\d{1,2}[-_][A-Za-z][A-Za-z0-9_-]*$")  # 2-books, 01-movies
VIDEO_ROOTS = ("01-movies", "02-tvshows")
_RES_RE = re.compile(r"\b(2160|1080|720|576|480)p\b|\b(4k|uhd)\b", re.I)


# -- helpers ------------------------------------------------------------------------
def is_junk_file(p: Path, size: int) -> bool:
    n = p.name.lower()
    if n in JUNK_NAMES or any(part.lower() == "$recycle.bin" for part in p.parts):
        return True
    ext = p.suffix.lower()
    if ext in {".url", ".nfo", ".lnk", ".website"}:
        return True
    if ext in AD_EXT and AD_RE.search(p.stem):
        return True
    return is_sample(p, size)


def is_sample(p: Path, size: int) -> bool:
    return (p.suffix.lower() in VIDEO_EXTENSIONS and size < SAMPLE_MAX
            and (re.search(r"\bsample\b", p.stem, re.I) is not None
                 or p.parent.name.lower() in {"sample", "samples"}))


def name_height(name: str) -> int | None:
    m = _RES_RE.search(name)
    if not m:
        return None
    return int(m.group(1)) if m.group(1) else 2160


def kind_of(p: Path) -> str:
    ext = p.suffix.lower()
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in BOOK_EXTENSIONS:
        return "book"
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    if ext in SOFTWARE_EXT:
        return "software"
    if ext in ARCHIVE_EXT or re.search(r"\.(r\d\d|part\d+\.rar)$", p.name, re.I):
        return "archive"
    return "other"


def walk(entry: Path) -> list[tuple[Path, int]]:
    """(path, size) of every regular file below ``entry`` (no symlink following)."""
    if entry.is_symlink():
        return []
    if entry.is_file():
        return [(entry, entry.stat().st_size)]
    out = []
    for dp, _dns, fns in os.walk(entry, followlinks=False):
        for f in fns:
            p = Path(dp) / f
            try:
                st = p.lstat()
            except OSError:
                continue
            if not p.is_symlink():
                out.append((p, st.st_size))
    return out


def entries(root: Path, depth: int = 1) -> list[Path]:
    """Top-level entries; numbered category dirs (``2-books``) are expanded ``depth``
    levels into their children."""
    def expand(d: Path, lvl: int) -> list[Path]:
        out = []
        for c in sorted(d.iterdir(), key=lambda x: x.name.lower()):
            if c.is_dir() and not c.is_symlink() and lvl < depth and CATEGORY_RE.match(c.name):
                kids = expand(c, lvl + 1)
                out += kids if kids else [c]
            else:
                out.append(c)
        return out
    return expand(root, 0)


def peek_archive(p: Path, limit: int = 15) -> dict[str, Any]:
    ext = p.suffix.lower()
    try:
        if ext == ".zip" or zipfile.is_zipfile(p):
            with zipfile.ZipFile(p) as z:
                names = z.namelist()
            return {"listed": True, "entries": len(names), "names": names[:limit]}
        if ext in {".tar", ".tgz"} or p.name.lower().endswith((".tar.gz", ".tar.bz2", ".tar.xz")):
            with tarfile.open(p) as t:
                names = t.getnames()
            return {"listed": True, "entries": len(names), "names": names[:limit]}
        if ext == ".rar":
            try:
                import rarfile  # type: ignore[import-not-found]
            except ImportError:
                return {"listed": False, "note": "rarfile not installed"}
            with rarfile.RarFile(p) as r:
                names = r.namelist()
            return {"listed": True, "entries": len(names), "names": names[:limit]}
    except Exception as exc:  # corrupt / partial download
        return {"listed": False, "note": f"unreadable: {type(exc).__name__}"}
    return {"listed": False, "note": "not listable"}


def _title_author(stem: str) -> tuple[str, str | None]:
    s = re.sub(r"[._]+", " ", stem)
    s = re.sub(r"[\(\[][^)\]]*[\)\]]", " ", s)
    parts = [x.strip() for x in re.split(r"\s+-\s+|\s+by\s+", s, flags=re.I) if x.strip()]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return (parts[0] if parts else stem), None


# -- catalog snapshot (read-only) -----------------------------------------------------
def load_catalog(db, calibre=None) -> dict[str, Any]:
    """One read-only transaction; no locks. Returns plain lists for :class:`Triage`."""
    cat: dict[str, Any] = {"media": [], "samples": {}, "suggestions": [], "moves": [],
                           "calibre": []}
    if db is not None:
        with db.conn() as c:
            c.execute("SET TRANSACTION READ ONLY")
            cat["media"] = c.execute(
                "SELECT id, path, root, size_bytes, title, year, season, episode, duration_s, "
                "COALESCE((media_info->'video'->0->>'height')::int, "
                "         (probe->'video'->>'height')::int) AS height "
                "FROM aib.media_file WHERE kind='video' AND NOT missing "
                "AND root = ANY(%s)", (list(VIDEO_ROOTS),)).fetchall()
            cat["samples"] = {r["path"]: r["sample"] for r in c.execute(
                "SELECT h.path, h.sample FROM aib.file_hash h JOIN aib.media_file m "
                "ON m.path=h.path AND m.size_bytes=h.size_bytes AND m.mtime=h.mtime "
                "WHERE h.sample IS NOT NULL AND m.root = ANY(%s)", (list(VIDEO_ROOTS),))}
            cat["suggestions"] = c.execute(
                "SELECT id, domain, source_path, target_path, status, confidence, decision, "
                "metadata FROM aib.suggestion WHERE source_path LIKE '/media/downloads/%%' "
                "OR source_path LIKE %s", ("%/downloads/%",)).fetchall()
            cat["moves"] = c.execute(
                "SELECT src AS old, dst AS new, 'file_op' AS via FROM aib.file_op "
                "WHERE result='ok' AND dst IS NOT NULL AND src LIKE %s "
                "UNION ALL SELECT old_path, new_path, 'change_log:' || op FROM aib.change_log "
                "WHERE status='committed' AND op IN ('move','rename','quarantine') "
                "AND new_path IS NOT NULL AND old_path LIKE %s",
                ("%/downloads/%", "%/downloads/%")).fetchall()
            c.rollback()
    if calibre is not None:
        try:
            cat["calibre"] = calibre.books()
        except Exception as exc:  # busy/corrupt: report, never guess
            cat["calibre_error"] = str(exc)
    return cat


# -- triage -------------------------------------------------------------------------
class Triage:
    def __init__(self, cat: dict[str, Any], calibre_dir: Path | None = None,
                 music_dir: Path | None = None, max_hash: int = 500, hasher=sample_hash):
        self.calibre_dir = str(calibre_dir) if calibre_dir else None
        self.music_dir, self.hasher = music_dir, hasher
        self.hash_budget, self.hashed = max_hash, 0
        self.samples = dict(cat.get("samples") or {})
        self.by_size: dict[int, list[dict]] = defaultdict(list)
        self.by_title: dict[tuple, list[dict]] = defaultdict(list)
        for m in cat.get("media") or []:
            self.by_size[int(m["size_bytes"])].append(m)
            t = norm(m.get("title"))
            if t:
                self.by_title[(t, m.get("year"), m.get("season"), m.get("episode"))].append(m)
                if m.get("year") is not None:
                    self.by_title[(t, None, m.get("season"), m.get("episode"))].append(m)
        self.sugs = {s["source_path"]: s for s in cat.get("suggestions") or []}
        self.moves: dict[str, list[dict]] = defaultdict(list)
        for mv in cat.get("moves") or []:
            self.moves[mv["old"]].append(mv)
        self.books = cat.get("calibre") or []
        self.calibre_error = cat.get("calibre_error")
        self.book_sizes: dict[int, list[tuple[dict, dict]]] = defaultdict(list)
        self.book_isbn: dict[str, dict] = {}
        self.book_titles: list[str] = []
        self.book_by_title: dict[str, list[dict]] = defaultdict(list)
        for b in self.books:
            for f in b.get("formats") or []:
                if f.get("size"):
                    self.book_sizes[int(f["size"])].append((b, f))
            if b.get("isbn"):
                self.book_isbn[re.sub(r"[^0-9Xx]", "", b["isbn"]).upper()] = b
            t = norm(b.get("title"))
            if t:
                if t not in self.book_by_title:
                    self.book_titles.append(t)
                self.book_by_title[t].append(b)
        self._music: set[str] | None = None

    # hashing with budget
    def _sample(self, path: str, size: int) -> str | None:
        if path in self.samples:
            return self.samples[path]
        if self.hashed >= self.hash_budget:
            return None
        try:
            self.hashed += 1
            self.samples[path] = self.hasher(Path(path), size)
        except OSError:
            self.samples[path] = None  # type: ignore[assignment]
        return self.samples[path]

    def moved(self, path: str) -> dict | None:
        for mv in self.moves.get(path, []):
            if Path(mv["new"]).exists():
                return mv
        return None

    # -- video --
    def match_video(self, p: Path, size: int, hint: str) -> dict | None:
        dl_h = name_height(p.name) or name_height(hint)
        def res(m: dict, how: str, conf: float) -> dict:
            lh = m.get("height")
            if how == "hash":
                cmp = "same"
            elif lh and dl_h:
                cmp = "same" if abs(lh - dl_h) <= 40 else ("bigger" if lh > dl_h else "smaller")
            else:
                ls = int(m["size_bytes"])
                cmp = "same" if ls == size else ("bigger" if ls > size else "smaller")
            return {"library_path": m["path"], "match": how, "confidence": conf,
                    "library_vs_download": cmp, "library_height": lh, "download_height": dl_h,
                    "library_size": int(m["size_bytes"])}
        cands = [m for m in self.by_size.get(size, []) if m["path"] != str(p)]
        if cands:
            mine = self._sample(str(p), size)
            for m in cands:
                theirs = self._sample(m["path"], size) if mine else None
                if mine and theirs and mine == theirs:
                    return res(m, "hash", 0.99)
            if mine is None:  # budget exhausted: exact size only
                return res(cands[0], "size", 0.8)
        meta = parse_name(p.name)
        if not meta.get("title") or (not meta.get("year") and meta.get("season") is None):
            hm = parse_name(hint)
            meta = {**hm, **{k: v for k, v in meta.items() if v is not None
                             and k in ("season", "episode")}} if hm.get("title") else meta
        t = norm(str(meta.get("title") or ""))
        if not t:
            return None
        key = (t, meta.get("year"), meta.get("season"), meta.get("episode"))
        hits = self.by_title.get(key) or (
            self.by_title.get((t, None, key[2], key[3])) if key[2] is not None else None) or []
        if not hits:
            return None
        best = min(hits, key=lambda m: abs(int(m["size_bytes"]) - size))
        within = abs(int(best["size_bytes"]) - size) <= SIZE_TOL * max(size, 1)
        conf = 0.9 if within else 0.75
        if meta.get("year") is None:
            conf -= 0.1
        return res(best, "title+year" + ("" if within else " (size differs)"), round(conf, 2))

    # -- book --
    def match_book(self, p: Path, size: int) -> dict:
        sp = str(p)
        sug = self.sugs.get(sp)
        sug_info = ({"suggestion_id": sug["id"], "suggestion_status": sug["status"],
                     "suggestion_confidence": sug["confidence"]} if sug else {})
        if sug and sug["status"] == "applied" and sug.get("target_path") \
                and Path(sug["target_path"]).exists():
            return {"library_path": sug["target_path"], "match": "suggestion applied",
                    "confidence": 0.95, "in_library": True, "library_vs_download": "same",
                    **sug_info}
        mv = self.moved(sp)
        if mv and self.calibre_dir and mv["new"].startswith(self.calibre_dir):
            return {"library_path": mv["new"], "match": f"moved ({mv['via']})",
                    "confidence": 0.95, "in_library": True, "library_vs_download": "same",
                    **sug_info}
        for _b, f in self.book_sizes.get(size, []):
            mine = self._sample(sp, size)
            theirs = self._sample(f["path"], size) if mine else None
            if mine and theirs and mine == theirs:
                return {"library_path": f["path"], "match": "hash", "confidence": 0.99,
                        "in_library": True, "library_vs_download": "same", **sug_info}
        meta = (sug or {}).get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except ValueError:
                meta = {}
        fmt = p.suffix.lower().lstrip(".").upper()
        isbn = re.sub(r"[^0-9Xx]", "", str(meta.get("isbn") or "")).upper()
        b = self.book_isbn.get(isbn) if isbn else None
        how = "isbn"
        title, author = meta.get("title"), meta.get("author")
        if isinstance(author, list):
            author = author[0] if author else None
        if not title:
            title, author = _title_author(p.stem)
        conf = 0.9
        if b is None and self.book_titles:
            hit = process.extractOne(norm(title), self.book_titles, scorer=fuzz.token_sort_ratio,
                                     score_cutoff=90)
            if hit:
                for cand in self.book_by_title[hit[0]]:
                    if author and any(fuzz.token_sort_ratio(author.lower(), a.lower()) >= 85
                                      for a in cand.get("authors") or []):
                        b, how, conf = cand, "title+author", 0.88 if hit[1] >= 95 else 0.8
                        break
                    if not author and hit[1] >= 97:
                        b, how, conf = cand, "title only", 0.65
        if b is None:
            return {"in_library": False, **sug_info}
        fmts = b.get("formats") or []
        same = [f for f in fmts if f["format"] == fmt]
        ref = (same or fmts or [{"path": str(Path(self.calibre_dir or "") / b.get("path", ""))}])[0]
        cmp = "same" if same else "other_format"
        return {"library_path": ref["path"], "match": how, "confidence": conf,
                "in_library": True, "library_vs_download": cmp,
                "calibre_id": b.get("id"), **sug_info}

    # -- audio --
    def music_names(self) -> set[str]:
        if self._music is None:
            self._music = set()
            if self.music_dir and self.music_dir.is_dir():
                for _dp, dns, fns in os.walk(self.music_dir):
                    for n in dns:
                        self._music.add(norm(n))
                    for n in fns:
                        if Path(n).suffix.lower() in AUDIO_EXTENSIONS:
                            self._music.add(norm(Path(n).stem))
        return self._music

    # -- one entry --
    def classify(self, entry: Path, files: list[tuple[Path, int]]) -> dict[str, Any]:
        size = sum(s for _, s in files)
        rec: dict[str, Any] = {"path": str(entry), "class": "unknown", "size_bytes": size,
                               "file_count": len(files), "library_paths": [],
                               "confidence": 0.5, "action": "review", "details": {}}
        def done(cls, action, conf, **det):
            rec.update({"class": cls, "action": action, "confidence": round(conf, 2)})
            rec["details"].update(det)
            return rec
        if entry.name.lower() in JUNK_NAMES:
            return done("junk", "quarantine_safe", 0.99, reason="system/recycle folder")
        if not files:
            return done("junk", "quarantine_safe", 0.95, reason="empty")
        real = [(p, s) for p, s in files if not is_junk_file(p, s)]
        if not real:
            return done("junk", "quarantine_safe", 0.95,
                        reason="only junk/ad/sample files")
        mv = self.moved(str(entry))
        if mv:
            rec["library_paths"] = [mv["new"]]
            return done("moved_already", "quarantine_safe", 0.95, via=mv["via"])
        by: dict[str, list[tuple[Path, int]]] = defaultdict(list)
        for p, s in real:
            by[kind_of(p)].append((p, s))
        # moved file-by-file?
        movable = [(p, s) for p, s in real if kind_of(p) in ("video", "book", "audio")]
        if movable:
            mvs = [self.moved(str(p)) for p, _ in movable]
            if all(mvs) and not (self.calibre_dir and all(
                    m["new"].startswith(self.calibre_dir) for m in mvs if m)):
                rec["library_paths"] = sorted({m["new"] for m in mvs if m})[:20]
                return done("moved_already", "quarantine_safe", 0.95, via=mvs[0]["via"])
        vids = sorted(by["video"], key=lambda x: -x[1])
        vbytes = sum(s for _, s in vids)
        if vids and vbytes >= 0.5 * size:
            ms = [self.match_video(p, s, entry.name) for p, s in vids]
            hit = [m for m in ms if m]
            rec["library_paths"] = sorted({m["library_path"] for m in hit})[:20]
            rec["details"]["videos"] = len(vids)
            rec["details"]["matched"] = len(hit)
            if hit and len(hit) == len(vids):
                cmps = {m["library_vs_download"] for m in hit}
                conf = min(m["confidence"] for m in hit)
                cmp = "smaller" if "smaller" in cmps else ("bigger" if "bigger" in cmps else "same")
                safe = cmp in ("same", "bigger") and conf >= 0.85
                return done("in_library_movie", "quarantine_safe" if safe else "review", conf,
                            library_vs_download=cmp, match=hit[0]["match"],
                            library_height=hit[0]["library_height"],
                            download_height=hit[0]["download_height"],
                            library_size=sum(m["library_size"] for m in hit))
            if hit:
                return done("pending_video", "review", 0.6, partial=True,
                            note=f"{len(hit)}/{len(vids)} videos already in library")
            return done("pending_video", "import", 0.7)
        books = by["book"]
        if books and sum(s for _, s in books) >= 0.3 * size:
            ms = [self.match_book(p, s) for p, s in books]
            inlib = [m for m in ms if m.get("in_library")]
            rec["library_paths"] = sorted({m["library_path"] for m in inlib})[:20]
            rec["details"]["books"] = len(books)
            rec["details"]["matched"] = len(inlib)
            sugs = [m for m in ms if m.get("suggestion_id")]
            if sugs:
                rec["details"]["suggestions"] = [
                    {"id": m["suggestion_id"], "status": m["suggestion_status"],
                     "score": m["suggestion_confidence"]} for m in sugs[:10]]
            if inlib and len(inlib) == len(books):
                conf = min(m["confidence"] for m in inlib)
                same = all(m["library_vs_download"] == "same" for m in inlib)
                safe = same and conf >= 0.85
                return done("in_library_book", "quarantine_safe" if safe else "review", conf,
                            match=inlib[0]["match"],
                            library_vs_download="same" if same else "other_format")
            note = f"{len(inlib)}/{len(books)} in Calibre" if inlib else None
            return done("pending_book", "import" if not inlib else "review",
                        0.7, has_suggestion=bool(sugs), note=note)
        if vids:  # small videos inside a mostly-other entry
            return done("pending_video", "review", 0.5)
        if by["audio"]:
            names = self.music_names()
            cand = [norm(entry.stem if entry.is_file() else entry.name)] + \
                   [norm(p.stem) for p, _ in by["audio"][:50]]
            found = any(c and c in names for c in cand)
            return done("audio", "review" if found else "import", 0.6,
                        in_music_library=found)
        if by["software"]:
            return done("software", "keep", 0.8,
                        files=[p.name for p, _ in by["software"][:10]])
        if by["archive"]:
            first = sorted(by["archive"], key=lambda x: -x[1])[0][0]
            return done("archive", "review", 0.7, peek=peek_archive(first),
                        archives=len(by["archive"]))
        return done("unknown", "review", 0.3,
                    exts=sorted({p.suffix.lower() for p, _ in real})[:15])


def triage(root: Path, cat: dict[str, Any], *, calibre_dir: Path | None = None,
           music_dir: Path | None = None, depth: int = 1, max_hash: int = 500,
           progress: ProgressReporter | None = None, hasher=sample_hash) -> dict[str, Any]:
    t = Triage(cat, calibre_dir, music_dir, max_hash, hasher)
    ents = entries(root, depth)
    pr = progress or ProgressReporter("downloads-triage", total=len(ents), done_key="entries")
    pr.total = len(ents)
    pr.start(str(root))
    items, nfiles = [], 0
    for i, e in enumerate(ents, 1):
        try:
            files = walk(e)
            rec = t.classify(e, files)
        except OSError as exc:
            rec = {"path": str(e), "class": "unknown", "size_bytes": 0, "file_count": 0,
                   "library_paths": [], "confidence": 0.0, "action": "review",
                   "details": {"error": str(exc)}}
            files = []
        nfiles += len(files)
        items.append(rec)
        pr.tick(current=e.name, entries=i, files=nfiles, hashed=t.hashed)
    pr.finish(entries=len(items), files=nfiles, hashed=t.hashed)
    return {"root": str(root), "entries": len(items), "files": nfiles, "hashed": t.hashed,
            "hash_budget_exhausted": t.hashed >= max_hash,
            "calibre_books": len(t.books), "calibre_error": t.calibre_error,
            "library_videos": sum(len(v) for v in t.by_size.values()),
            "summary": summarize(items), "items": items}


def summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
    per = {c: {"count": 0, "bytes": 0} for c in CLASSES}
    acts: dict[str, dict[str, int]] = defaultdict(lambda: {"count": 0, "bytes": 0})
    for r in items:
        per[r["class"]]["count"] += 1
        per[r["class"]]["bytes"] += r["size_bytes"]
        acts[r["action"]]["count"] += 1
        acts[r["action"]]["bytes"] += r["size_bytes"]
    review = sorted((r for r in items if r["action"] == "review"),
                    key=lambda r: -r["size_bytes"])[:20]
    return {"per_class": per, "per_action": dict(acts),
            "reclaimable_bytes": acts["quarantine_safe"]["bytes"] if "quarantine_safe" in acts else 0,
            "top_review": [{"path": r["path"], "class": r["class"], "size_bytes": r["size_bytes"]}
                           for r in review]}


def _gb(b: int) -> str:
    return f"{b / 1024 ** 3:,.2f} GB"


def format_summary(rep: dict[str, Any]) -> str:
    s = rep["summary"]
    out = io.StringIO()
    out.write(f"downloads triage of {rep['root']}: {rep['entries']} entries, {rep['files']} files,"
              f" {rep['hashed']} sample hashes"
              + (" (hash budget exhausted)" if rep["hash_budget_exhausted"] else "") + "\n")
    if rep.get("calibre_error"):
        out.write(f"WARNING calibre unreadable: {rep['calibre_error']}\n")
    out.write(f"\n{'class':<18}{'count':>8}{'size':>14}\n")
    for c, v in s["per_class"].items():
        if v["count"]:
            out.write(f"{c:<18}{v['count']:>8}{_gb(v['bytes']):>14}\n")
    out.write("\nby action:\n")
    for a, v in sorted(s["per_action"].items()):
        out.write(f"  {a:<16}{v['count']:>8}{_gb(v['bytes']):>14}\n")
    out.write(f"\nreclaimable (quarantine_safe): {_gb(s['reclaimable_bytes'])}\n")
    out.write("\ntop review items:\n")
    for r in s["top_review"]:
        out.write(f"  {_gb(r['size_bytes']):>12}  {r['class']:<16} {r['path']}\n")
    return out.getvalue()


CSV_FIELDS = ["path", "class", "action", "confidence", "size_bytes", "file_count",
              "library_paths", "details"]


def write_reports(rep: dict[str, Any], out: Path) -> tuple[Path, Path]:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(rep, indent=2, default=str))
    tmp.replace(out)
    cpath = out.with_suffix(".csv")
    with cpath.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rep["items"]:
            w.writerow({**{k: r[k] for k in CSV_FIELDS[:6]},
                        "library_paths": " | ".join(r["library_paths"]),
                        "details": json.dumps(r["details"], default=str)})
    return out, cpath
