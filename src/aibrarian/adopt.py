"""Adopt loose files inside the Calibre library (``aibrarian books adopt-orphans``).

"Orphans" are files under ``calibre_dir`` that ``metadata.db`` does not reference (the
same list ``books validate`` reports). They are grouped by folder - the expected layout is
``<library>/<Author>/<Title>/<Title>.<ext>`` with one title in several formats - and per
group:

* Calibre already has a book with the same (normalized) title + author:
  formats it lacks -> ``add_format`` (``calibredb add_format --dont-replace``);
  formats it already has -> ``duplicate`` (the loose file is quarantined).
* no such book -> ``calibre_add`` of the preferred format (``calibredb add --title
  --authors`` from the folder names), then ``add_format`` for the rest.

Only after calibredb succeeded is the loose original moved (never deleted) into
``<library>/.aibrarian-quarantine/<date>/`` with a quarantine ``manifest.jsonl``/``.tsv``
(same mount as the library, so moves are instant renames; hidden, so Calibre, Calibre-Web
and ``validate`` ignore it). Leftover sidecars (cover.jpg, metadata.opf) follow the last
book file, and the emptied folder is removed.

Dry run by default. Every action goes to ``aib.library_action`` (run ``adopt-<UTC ts>``,
with calibredb stdout/stderr) and is undoable with ``aibrarian books undo --run``.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from aibrarian import changelog
from aibrarian import quarantine as qmod
from aibrarian.booklib import ActionLog, make_calibre
from aibrarian.calibre import Calibre
from aibrarian.config import BOOK_EXTENSIONS
from aibrarian.duplicates import full_hash

log = logging.getLogger(__name__)
QUARANTINE_DIRNAME = ".aibrarian-quarantine"
FORMAT_PREFERENCE = ("EPUB", "AZW3", "MOBI", "PDF", "CBZ", "CBR")


def norm(text: str | None) -> str:
    """Casefolded, accents and punctuation removed, whitespace collapsed
    ("George R.R. Martin" == "george r r martin")."""
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).casefold()
    t = t.replace("&", " and ")
    t = re.sub(r"[^\w]+", " ", t).replace("_", " ")
    return " ".join(t.split())


def norm_author(text: str | None) -> str:
    return norm(text).replace(" ", "")


def _fmt(p: Path) -> str:
    return p.suffix.lower().lstrip(".").upper()


def _fmt_rank(fmt: str) -> int:
    return FORMAT_PREFERENCE.index(fmt) if fmt in FORMAT_PREFERENCE else len(FORMAT_PREFERENCE)


def find_groups(cal: Calibre) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(groups, skipped)`` from the orphan list. A group is one ``Author/Title`` folder."""
    lib = cal.library
    by_dir: dict[Path, list[Path]] = {}
    skipped = []
    for f in cal.orphan_files():
        p = Path(f)
        rel = p.relative_to(lib)
        if p.suffix.lower() not in BOOK_EXTENSIONS:
            skipped.append({"path": f, "reason": "not an ebook format"})
        elif len(rel.parts) != 3:
            skipped.append({"path": f, "reason": "not in <Author>/<Title>/ layout"})
        else:
            by_dir.setdefault(p.parent, []).append(p)
    groups = []
    for d in sorted(by_dir):
        files = sorted(by_dir[d], key=lambda p: (_fmt_rank(_fmt(p)), p.name))
        groups.append({"folder": str(d), "author": d.parent.name, "title": d.name,
                       "files": [str(p) for p in files]})
    return groups, skipped


def match_book(books: list[dict[str, Any]], title: str, author: str) -> dict[str, Any] | None:
    nt, na = norm(title), norm_author(author)
    for b in books:
        if norm(b["title"]) == nt and any(norm_author(a) == na for a in b["authors"]):
            return b
    return None


def plan_group(g: dict[str, Any], books: list[dict[str, Any]]) -> dict[str, Any]:
    book = match_book(books, g["title"], g["author"])
    steps: list[dict[str, Any]] = []
    have = {f["format"] for f in book["formats"]} if book else set()
    seen: set[str] = set()
    for i, f in enumerate(g["files"]):
        fmt = _fmt(Path(f))
        if fmt in seen:
            steps.append({"op": "skip", "path": f, "format": fmt,
                          "reason": f"second {fmt} in the same folder; left for review"})
            continue
        seen.add(fmt)
        if book is None and i == 0:
            op = "calibre_add"
        elif fmt in have:
            op = "duplicate"
        else:
            op = "add_format"
        steps.append({"op": op, "path": f, "format": fmt})
    return g | {"calibre_id": book["id"] if book else None,
                "calibre_title": book["title"] if book else None, "steps": steps}


def _quarantine(src: Path, keeper: str | None, lib: Path, batch: Path,
                sidecars: list[Path] | None = None) -> dict[str, Any]:
    """Rename ``src`` (plus ``sidecars``) into ``batch`` keeping the library-relative path;
    manifest record in the format ``quarantine.restore`` reads."""
    dst = batch / src.relative_to(lib)
    if dst.exists():
        return {"result": "skipped", "reason": "quarantine target already exists"}
    sha = full_hash(src)
    size = src.stat().st_size
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.rename(src, dst)
    moved = []
    for sp in sidecars or []:
        sd = batch / sp.relative_to(lib)
        if sp.is_file() and not sd.exists():
            os.rename(sp, sd)
            moved.append({"from": str(sp), "to": str(sd)})
    rec = {"moved_at": datetime.now(UTC).isoformat(), "group": None, "media_id": None,
           "size_bytes": size, "keeper": keeper, "path": str(src), "quarantine_path": str(dst),
           "sha256": sha, "prev_stage": None, "sidecars_copied_to_keeper": [],
           "sidecars_moved": moved}
    qmod._write_manifest(batch, rec)
    return {"result": "moved", "quarantine_path": str(dst), "sha256": sha,
            "manifest": str(batch / "manifest.jsonl"), "sidecars_moved": moved}


def _calibre_file(cal: Calibre, bid: int, fmt: str) -> str | None:
    book = next((b for b in cal.books() if b["id"] == bid), None)
    return next((f["path"] for f in (book or {}).get("formats", []) if f["format"] == fmt), None)


def _logged_quarantine(logger: ActionLog, src: Path, keeper: str | None, lib: Path,
                       batch: Path, sides: list[Path]) -> dict[str, Any]:
    """``_quarantine`` wrapped in an aib.change_log entry + per-path advisory lock."""
    try:
        with changelog.change(logger.db, "quarantine", "book", old_path=src,
                              new_path=batch / src.relative_to(lib), source=logger.source,
                              check_lease=False, details={"keeper": keeper,
                                                          "reason": "calibre_adopt"}) as ch:
            q = _quarantine(src, keeper, lib, batch, sides)
            if q["result"] != "moved":
                ch.fail(q.get("reason") or q["result"])
            else:
                ch.sidecars([(m["from"], m["to"]) for m in q.get("sidecars_moved", [])])
            return q
    except changelog.Busy as exc:
        return {"result": "skipped", "reason": f"busy: {exc}"}


def _run_group(p: dict[str, Any], cal: Calibre, batch: Path, logger: ActionLog,
               say: Callable[[str], None]) -> dict[str, int]:
    lib = cal.library
    bid = p["calibre_id"]
    counts: dict[str, int] = {}
    base = {"title": p["title"], "author": p["author"]}
    done: list[dict[str, Any]] = []

    def bump(k: str) -> None:
        counts[k] = counts.get(k, 0) + 1

    for st in p["steps"]:
        src, fmt = Path(st["path"]), st["format"]
        item = base | {"old_path": str(src)}
        if st["op"] == "skip":
            logger("skip", item | {"reason": st["reason"]})
            bump("skip")
            continue
        if not src.is_file():
            logger("skip", item | {"reason": "source vanished"})
            bump("skip")
            continue
        if st["op"] == "duplicate" or (st["op"] == "add_format" and bid is None):
            if bid is None:  # calibre_add failed earlier in this group
                logger("skip", item | {"reason": "calibre_add of this title failed; left"})
                bump("skip")
                continue
            keeper = _calibre_file(cal, bid, fmt)
            done.append({"src": src, "action": "quarantine", "keeper": keeper,
                         "reason": f"duplicate: Calibre book {bid} already has {fmt}",
                         "details": {"calibre_id": bid, "format": fmt}})
            continue
        sha = full_hash(src)
        if st["op"] == "calibre_add":
            res = cal.add(src, {"title": p["title"], "author": p["author"]})
            new_id = res.book_ids[0] if res.book_ids else None
        else:
            res = cal.add_format(int(bid), src)
            new_id = int(bid) if res.ok else None
        details = {"calibre": res.as_log(), "calibre_id": new_id, "sha256": sha, "format": fmt,
                   "library": str(lib), "adopted_orphan": True}
        if not res.ok or new_id is None:
            logger("error", item | {"reason": f"calibredb {st['op']} failed (exit "
                                              f"{res.exit_code}); file left in place"},
                   details=details)
            say(f"    ERROR calibredb {st['op']} exit {res.exit_code}: {src.name} "
                f"{(res.stderr or res.stdout).strip()[-300:]}")
            bump("error")
            continue
        bid = new_id
        done.append({"src": src, "action": st["op"], "keeper": _calibre_file(cal, bid, fmt),
                     "details": details})
    # quarantine the loose originals; the last one carries the folder's leftovers
    folder = Path(p["folder"])
    for k, d in enumerate(done):
        src = d["src"]
        last = k == len(done) - 1
        sides = []
        if last and folder.is_dir():
            sides = [x for x in sorted(folder.iterdir()) if x.is_file() and x != src
                     and x.suffix.lower() not in BOOK_EXTENSIONS]
        q = _logged_quarantine(logger, src, d["keeper"], lib, batch, sides) \
            if src.is_file() else {"result": "skipped", "reason": "source vanished"}
        sq = {x: q.get(x) for x in ("result", "reason", "manifest", "quarantine_path")}
        if d["action"] == "quarantine":
            if q["result"] == "moved":
                logger("quarantine", base | {"old_path": str(src),
                                             "new_path": q["quarantine_path"],
                                             "reason": d["reason"]},
                       details=d["details"] | {"manifest": q["manifest"], "keeper": d["keeper"],
                                               "sha256": q["sha256"]})
                bump("duplicate_quarantined")
            else:
                logger("skip", base | {"old_path": str(src),
                                       "reason": f"quarantine refused: {q.get('reason')}"})
                bump("skip")
            continue
        logger(d["action"], base | {"old_path": str(src), "new_path": d["keeper"],
                                    "reason": f"adopted orphan into Calibre book "
                                              f"{d['details']['calibre_id']}"},
               details=d["details"] | {"calibre_file": d["keeper"], "source_quarantine": sq})
        bump(d["action"])
        if q["result"] != "moved":
            logger("needs_review", base | {"old_path": str(src),
                                           "reason": f"in Calibre but loose file not "
                                                     f"quarantined: {q.get('reason')}"})
            bump("needs_review")
    try:
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    except OSError:
        pass
    return counts


def adopt_orphans(s, db=None, execute: bool = False, limit: int | None = None,
                  source: str = "cli", progress: Callable[[str], None] | None = None,
                  calibre: Calibre | None = None) -> dict[str, Any]:
    say = progress or (lambda _m: None)
    cal = calibre or make_calibre(s)
    if not cal.db_path.exists():
        raise FileNotFoundError(f"no Calibre metadata.db in {cal.library}")
    groups, skipped = find_groups(cal)
    total = len(groups)
    groups = groups[:limit] if limit else groups
    books = cal.books()
    plans = [plan_group(g, books) for g in groups]
    run_id = "adopt-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    batch = cal.library / QUARANTINE_DIRNAME / date.today().isoformat()
    logger = ActionLog(db, Path(s.report_dir), run_id, source)
    counts: dict[str, int] = {}
    say("DRY RUN - nothing changed. Add --execute to apply." if not execute else
        f"run {run_id} (undo: aibrarian books undo --run {run_id}); quarantine {batch}")
    for k, p in enumerate(plans, 1):
        target = f"existing Calibre book {p['calibre_id']} ({p['calibre_title']!r})" \
            if p["calibre_id"] else "new Calibre book"
        say(f"[{k}/{len(plans)}] {p['author']} / {p['title']} -> {target}")
        for st in p["steps"]:
            extra = f"  ({st['reason']})" if st.get("reason") else ""
            say(f"    {st['op']:<12} {st['format']:<5} {Path(st['path']).name}{extra}")
        if not execute:
            for st in p["steps"]:
                counts[st["op"]] = counts.get(st["op"], 0) + 1
            continue
        try:
            for key, n in _run_group(p, cal, batch, logger, say).items():
                counts[key] = counts.get(key, 0) + n
            books = cal.books()  # refresh for later groups
        except Exception as exc:  # one bad group must not stop the batch
            log.exception("adopt-orphans group failed")
            logger("error", {"old_path": p["folder"], "title": p["title"],
                             "author": p["author"], "reason": f"{type(exc).__name__}: {exc}"})
            counts["error"] = counts.get("error", 0) + 1
    for sk in skipped:
        say(f"    skip (not adopted) {sk['path']}: {sk['reason']}")
    summary = (f"{'DRY RUN ' if not execute else ''}groups {len(plans)}/{total}, "
               f"files {sum(len(p['steps']) for p in plans)}; "
               + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
               + f"; not adoptable: {len(skipped)}")
    say(summary)
    return {"dry_run": not execute, "run_id": run_id if execute else None,
            "quarantine_batch": str(batch) if execute else None, "groups_total": total,
            "groups": plans, "skipped": skipped, "counts": counts, "summary": summary}
