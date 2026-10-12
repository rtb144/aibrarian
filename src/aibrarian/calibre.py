"""Calibre library access: ``calibredb`` for writes, read-only sqlite for lookups.

* Writes (add / add_format / remove) go through ``calibredb --library-path`` under an
  exclusive file lock (``<report_dir>/.calibre.lock``) shared by every aibrarian
  process, and are retried with backoff when SQLite says "database is locked"
  (Calibre-Web may hold a short lock).
* Lookups and validation open ``metadata.db`` with ``mode=ro`` and never write.
  (``calibredb check_library`` is deliberately NOT used: it VACUUMs metadata.db.)
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import shlex
import sqlite3
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from aibrarian.duplicates import full_hash

log = logging.getLogger(__name__)
LOCKED = "database is locked"
LIBRARY_FILES = {"metadata.db", "metadata_db_prefs_backup.json", "full-text-search.db",
                 "metadata.db-journal", "metadata.db-wal", "metadata.db-shm"}
BOOK_DIR_FILES = {"metadata.opf", "cover.jpg"}


@dataclass
class CalibreResult:
    cmd: list[str]
    exit_code: int
    stdout: str
    stderr: str
    attempts: int = 1
    book_ids: list[int] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def as_log(self) -> dict[str, Any]:
        return {"cmd": shlex.join(self.cmd), "exit_code": self.exit_code,
                "stdout": self.stdout[-20000:], "stderr": self.stderr[-20000:],
                "attempts": self.attempts, "book_ids": self.book_ids}


Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    env = os.environ | {"QT_QPA_PLATFORM": "offscreen"}
    return subprocess.run(cmd, capture_output=True, text=True, timeout=900, env=env)


class Calibre:
    def __init__(self, library: Path, lock_file: Path, calibredb: str = "calibredb",
                 runner: Runner | None = None, retries: int = 6, backoff_s: float = 2.0):
        self.library, self.lock_file = Path(library), Path(lock_file)
        self.bin, self.runner = calibredb, runner or _run
        self.retries, self.backoff_s = retries, backoff_s

    # -- writes ------------------------------------------------------------------
    @contextmanager
    def lock(self) -> Iterator[None]:
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_file.open("a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def command(self, *args: str) -> list[str]:
        return [self.bin, "--library-path", str(self.library), *args]

    def run(self, *args: str) -> CalibreResult:
        cmd = self.command(*args)
        with self.lock():
            for attempt in range(1, self.retries + 1):
                try:
                    p = self.runner(cmd)
                    res = CalibreResult(cmd, p.returncode, p.stdout or "", p.stderr or "", attempt)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    res = CalibreResult(cmd, 127, "", str(exc), attempt)
                if LOCKED not in (res.stdout + res.stderr).lower() or attempt == self.retries:
                    break
                log.warning("calibre db locked, retrying", extra={"attempt": attempt})
                time.sleep(self.backoff_s * attempt)
        m = re.search(r"Added book ids?:\s*([\d,\s]+)", res.stdout)
        if m:
            res.book_ids = [int(x) for x in re.findall(r"\d+", m.group(1))]
        if res.ok and LOCKED in (res.stdout + res.stderr).lower():
            res.exit_code = 75
        return res

    @staticmethod
    def add_args(src: Path, meta: dict[str, Any]) -> list[str]:
        a = ["add", "--automerge=ignore"]
        if meta.get("title"):
            a += ["--title", meta["title"]]
        if meta.get("author"):
            a += ["--authors", meta["author"]]
        if meta.get("series"):
            a += ["--series", meta["series"]]
            if meta.get("series_index") is not None:
                a += ["--series-index", str(meta["series_index"])]
        if meta.get("isbn"):
            a += ["--isbn", str(meta["isbn"])]
        ol = meta.get("openlibrary") or {}
        if ol.get("edition_key") or ol.get("work_key"):
            a += ["--identifier", f"openlibrary:{ol.get('edition_key') or ol.get('work_key')}"]
        a += ["--tags", "aibrarian"]
        return a + [str(src)]

    def add(self, src: Path, meta: dict[str, Any]) -> CalibreResult:
        res = self.run(*self.add_args(src, meta))
        if res.ok and not res.book_ids:
            res.exit_code = 65  # nothing added (calibre saw a duplicate) -> caller reviews
        return res

    def add_format(self, book_id: int, src: Path) -> CalibreResult:
        return self.run("add_format", "--dont-replace", str(book_id), str(src))

    def remove(self, book_id: int) -> CalibreResult:
        return self.run("remove", str(book_id))  # goes to Calibre's trash bin (recoverable)

    def remove_format(self, book_id: int, fmt: str) -> CalibreResult:
        return self.run("remove_format", str(book_id), fmt.upper())

    # -- read-only ---------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.library / "metadata.db"

    def connect_ro(self) -> sqlite3.Connection:
        c = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    def books(self) -> list[dict[str, Any]]:
        """All books with authors, isbn and format files (read-only)."""
        if not self.db_path.exists():
            return []
        with self.connect_ro() as c:
            rows = {r["id"]: dict(r) | {"authors": [], "isbn": None, "formats": []}
                    for r in c.execute("SELECT id, title, path FROM books")}
            for r in c.execute("SELECT l.book, a.name FROM books_authors_link l "
                               "JOIN authors a ON a.id=l.author ORDER BY l.id"):
                if r["book"] in rows:
                    rows[r["book"]]["authors"].append(r["name"])
            for r in c.execute("SELECT book, val FROM identifiers WHERE type='isbn'"):
                if r["book"] in rows:
                    rows[r["book"]]["isbn"] = r["val"]
            for r in c.execute("SELECT book, format, name, uncompressed_size FROM data"):
                b = rows.get(r["book"])
                if b:
                    b["formats"].append({
                        "format": r["format"].upper(), "size": r["uncompressed_size"],
                        "path": str(self.library / b["path"] / f"{r['name']}.{r['format'].lower()}")})
        return list(rows.values())

    def find_duplicate(self, src: Path, meta: dict[str, Any],
                       hasher=full_hash) -> dict[str, Any] | None:
        """``{"kind": "exact"|"same_book", "book": {...}, "has_format": bool}`` or None.
        exact: some Calibre format file is byte-identical (size + sha256) to ``src``."""
        size, sha = src.stat().st_size, None
        books = self.books()
        for b in books:
            for f in b["formats"]:
                if f["size"] == size and Path(f["path"]).is_file():
                    sha = sha or hasher(src)
                    if hasher(Path(f["path"])) == sha:
                        return {"kind": "exact", "book": b, "file": f["path"], "sha256": sha}
        fmt = src.suffix.lower().lstrip(".").upper()
        isbn = re.sub(r"[^0-9Xx]", "", str(meta.get("isbn") or ""))
        for b in books:
            same_isbn = isbn and re.sub(r"[^0-9Xx]", "", str(b["isbn"] or "")) == isbn
            same_ta = meta.get("title") and meta.get("author") and \
                fuzz.token_sort_ratio(meta["title"].lower(), (b["title"] or "").lower()) >= 92 and \
                any(fuzz.token_sort_ratio(meta["author"].lower(), a.lower()) >= 90
                    for a in b["authors"])
            if same_isbn or same_ta:
                return {"kind": "same_book", "book": b,
                        "has_format": any(f["format"] == fmt for f in b["formats"]),
                        "match": "isbn" if same_isbn else "title+author"}
        return None

    def integrity_check(self) -> str:
        with self.connect_ro() as c:
            return "; ".join(r[0] for r in c.execute("PRAGMA integrity_check"))

    def validate(self, ingests: list[dict[str, Any]], hasher=full_hash) -> dict[str, Any]:
        """Read-only consistency report. ``ingests``: logged calibre_add/add_format rows."""
        issues: dict[str, list] = {"integrity": [], "missing_logged_book": [],
                                   "missing_logged_file": [], "sha_mismatch": [],
                                   "missing_files": [], "orphan_files": []}
        if not self.db_path.exists():
            issues["integrity"].append(f"no metadata.db in {self.library}")
            return {"ok": False, "issues": issues, "books": 0}
        integ = self.integrity_check()
        if integ != "ok":
            issues["integrity"].append(integ)
        books = {b["id"]: b for b in self.books()}
        known = set()
        for b in books.values():
            for f in b["formats"]:
                known.add(os.path.normpath(f["path"]))
                if not Path(f["path"]).is_file():
                    issues["missing_files"].append({"book_id": b["id"], "title": b["title"],
                                                    "path": f["path"]})
        for a in ingests:
            d = a.get("details") or {}
            bid, sha, fmt = d.get("calibre_id"), d.get("sha256"), d.get("format")
            if not bid:
                continue
            b = books.get(int(bid))
            if not b:
                issues["missing_logged_book"].append({"action_id": a.get("id"), "book_id": bid,
                                                      "title": a.get("title")})
                continue
            fs = [f for f in b["formats"] if not fmt or f["format"] == fmt.upper()]
            if not fs or not Path(fs[0]["path"]).is_file():
                issues["missing_logged_file"].append({"action_id": a.get("id"), "book_id": bid,
                                                      "format": fmt})
            elif sha and hasher(Path(fs[0]["path"])) != sha:
                issues["sha_mismatch"].append({"action_id": a.get("id"), "book_id": bid,
                                               "path": fs[0]["path"],
                                               "note": "changed since ingest (e.g. metadata "
                                                       "embedded by Calibre)"})
        for dirpath, dirnames, filenames in os.walk(self.library):
            dirnames[:] = [x for x in dirnames if not x.startswith(".")]
            top = Path(dirpath) == self.library
            for fn in filenames:
                if fn.startswith(".") or (top and fn in LIBRARY_FILES) or \
                        (not top and fn in BOOK_DIR_FILES):
                    continue
                p = os.path.normpath(os.path.join(dirpath, fn))
                if p not in known:
                    issues["orphan_files"].append(p)
        n = sum(len(v) for v in issues.values())
        return {"ok": n == 0, "books": len(books), "issue_count": n, "integrity": integ,
                "issues": issues}
