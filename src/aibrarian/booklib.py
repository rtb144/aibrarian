"""Automatic book librarian: apply open book suggestions safely (``book-autoapply``).

Per open book suggestion (oldest first, up to ``limit``):

* confidence < ``book_auto_min_confidence`` -> status ``needs_review`` (file untouched)
* source must exist under ``media_root`` (book_drop/downloads); target must be inside a
  book library root (Calibre / Audiobookshelf audiobooks / podcasts), else needs_review
* target exists: byte-identical (size + sha256) -> the incoming copy is quarantined via
  :mod:`aibrarian.quarantine` (same manifest / purge / restore); different -> needs_review
* the move is a plain ``os.rename`` (never a copy): another filesystem/mount -> needs_review
* sidecars (same-stem files: cover, .opf, .nfo ...; plus folder-level cover/metadata.opf
  when the folder holds just this one book) travel along; nothing is ever overwritten
* the emptied source folder is removed (leftover junk quarantined; see quarantine.py)

Every outcome is logged in ``aib.library_action`` and ``<report_dir>/library-actions.log``
(JSONL, never purged). ``undo`` reverses move/rename/quarantine actions by id or run.
"""

from __future__ import annotations

import errno
import json
import logging
import os
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from aibrarian import quarantine as qmod
from aibrarian.config import AUDIO_EXTENSIONS, BOOK_EXTENSIONS
from aibrarian.duplicates import full_hash
from aibrarian.fileops import remove_empty_parents

log = logging.getLogger(__name__)
LOG_FILE = "library-actions.log"
FOLDER_SIDECARS = {"cover.jpg", "cover.jpeg", "cover.png", "folder.jpg", "metadata.opf",
                   "desc.txt", "reader.txt"}
APPLY = "apply"


def book_sidecars(src: Path) -> list[tuple[Path, str]]:
    """``(path, kind)``; kind ``stem`` (renamed with the book) or ``folder`` (name kept)."""
    out: list[tuple[Path, str]] = []
    if not src.parent.is_dir():
        return out
    books = [p for p in src.parent.iterdir() if p.is_file()
             and p.suffix.lower() in BOOK_EXTENSIONS | AUDIO_EXTENSIONS]
    for p in sorted(src.parent.iterdir()):
        if p == src or not p.is_file() or p.is_symlink():
            continue
        if p.suffix.lower() in BOOK_EXTENSIONS | AUDIO_EXTENSIONS:
            continue
        if p.stem == src.stem or p.name.startswith(src.stem + "."):
            out.append((p, "stem"))
        elif len(books) == 1 and p.name.lower() in FOLDER_SIDECARS:
            out.append((p, "folder"))
    return out


def _sidecar_dst(src: Path, dst: Path, side: Path, kind: str) -> Path:
    return dst.parent / side.name if kind == "folder" else \
        dst.with_name(dst.stem + side.name[len(src.stem):])


def _existing_ancestor(p: Path) -> Path:
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def _meta(sug: dict[str, Any]) -> dict[str, Any]:
    md = sug.get("metadata") or {}
    if isinstance(md, str):
        md = json.loads(md)
    cls, emb, ref = md.get("classification") or {}, md.get("embedded") or {}, \
        md.get("reference") or {}
    return {"title": cls.get("title") or emb.get("title") or ref.get("title"),
            "author": cls.get("author") or emb.get("author") or ref.get("author"),
            "series": cls.get("series") or emb.get("series"),
            "series_index": (str(emb["series_index"]) if emb.get("series_index") else None)}


def library_roots(s) -> list[Path]:
    return [Path(s.calibre_dir), Path(s.audiobooks_dir), Path(s.podcasts_dir)]


def _within(p: Path, roots: list[Path]) -> Path | None:
    rp = p.resolve()
    return next((r for r in roots if rp.is_relative_to(r.resolve())), None)


def assess(sug: dict[str, Any], s, hasher=full_hash) -> dict[str, Any]:
    """Pure check (no writes). ``decision`` is apply | quarantine | needs_review | skip."""
    src = Path(sug["source_path"])
    dst = Path(sug["target_path"]) if sug.get("target_path") else None
    conf = float(sug.get("confidence") or 0)
    thr = s.book_auto_min_confidence
    base = {"suggestion_id": sug["id"], "old_path": str(src),
            "new_path": str(dst) if dst else None, "confidence": conf, "threshold": thr,
            "passes": conf >= thr, **_meta(sug)}

    def out(decision: str, reason: str = "", **kw: Any) -> dict[str, Any]:
        return base | {"decision": decision, "reason": reason} | kw

    if dst is None or sug.get("op") == "none" or str(dst) == str(src):
        return out("skip", "no target (no-op)")
    if conf < thr:
        return out("needs_review", f"confidence {conf:.2f} < {thr:.2f}")
    if not src.is_file():
        return out("skip", "source missing")
    if src.is_symlink() or not _within(src, [Path(s.media_root)]):
        return out("needs_review", "source not under media_root")
    lib = _within(_existing_ancestor(dst.parent), library_roots(s)) and \
        _within(dst.parent, library_roots(s))
    if not lib or ".." in dst.parts:
        return out("needs_review", "target outside the book library roots")
    if dst.exists():
        try:
            same = dst.is_file() and dst.stat().st_size == src.stat().st_size and \
                hasher(dst) == hasher(src)
        except OSError as exc:
            return out("needs_review", f"could not compare with existing target: {exc}")
        if same:
            return out("quarantine", "identical copy (sha256) already at target")
        return out("needs_review", "target exists with different content")
    if src.stat().st_dev != _existing_ancestor(dst.parent).stat().st_dev:
        return out("needs_review", "target is on another filesystem (would need a copy)")
    sides = [(p, k, _sidecar_dst(src, dst, p, k)) for p, k in book_sidecars(src)]
    clash = [str(d) for _, _, d in sides if d.exists()]
    if clash:
        return out("needs_review", f"sidecar target exists: {clash[0]}")
    op = "rename" if src.parent == dst.parent else "move"
    return out(APPLY, op, op=op,
               sidecars=[{"from": str(p), "to": str(d)} for p, _, d in sides])


class ActionLog:
    def __init__(self, db, log_dir: Path, run_id: str, source: str):
        self.db, self.file, self.run_id, self.source = db, log_dir / LOG_FILE, run_id, source

    def __call__(self, action: str, item: dict[str, Any], **kw: Any) -> int | None:
        row = {"ts": datetime.now(UTC).isoformat(), "run_id": self.run_id, "action": action,
               "domain": "book", "source": self.source} | \
              {k: item.get(k) for k in ("suggestion_id", "old_path", "new_path", "title",
                                         "author", "series", "series_index", "confidence")} | \
              {"reason": item.get("reason")} | kw
        rid = None
        if self.db is not None and hasattr(self.db, "record_library_action"):
            try:
                rid = self.db.record_library_action(row)
            except Exception as exc:  # the file op already happened; keep going
                log.error("library_action insert failed", extra={"error": str(exc)})
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            with self.file.open("a") as fh:
                fh.write(json.dumps(row | {"id": rid}, default=str) + "\n")
        except OSError as exc:
            log.error("library log write failed", extra={"error": str(exc)})
        return rid


def _status(db, sid: int, status: str) -> None:
    if db is not None:
        db.set_suggestion_status(sid, status)


def _apply(it: dict[str, Any], s, db, logger: ActionLog, say) -> str:
    src, dst = Path(it["old_path"]), Path(it["new_path"])
    batch = s.quarantine_root() / date.today().isoformat()
    done: list[dict[str, str]] = []
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            raise FileExistsError(str(dst))
        os.rename(src, dst)
        done.append({"from": str(src), "to": str(dst)})
        for m in it["sidecars"]:
            if Path(m["to"]).exists():
                continue
            os.rename(m["from"], m["to"])
            done.append(m)
    except OSError as exc:
        for m in reversed(done):  # roll back a partial move
            if not Path(m["from"]).exists():
                os.rename(m["to"], m["from"])
        reason = "target is on another mount (EXDEV)" if exc.errno == errno.EXDEV else str(exc)
        action = "needs_review" if exc.errno in (errno.EXDEV, errno.EEXIST) or \
            isinstance(exc, FileExistsError) else "error"
        _status(db, it["suggestion_id"], "needs_review" if action == "needs_review" else "failed")
        logger(action, it | {"reason": reason})
        say(f"{action.upper()} {src}: {reason}")
        return action
    folders = qmod.remove_empty_folders(src.parent, Path(s.media_root), batch, True, db,
                                        logger.source)
    removed = [f for f in folders if f["result"] == "removed"]
    _status(db, it["suggestion_id"], "applied")
    logger(it["op"], it | {"reason": f"confidence {it['confidence']:.2f}"},
           details={"moves": done, "batch": str(batch),
                    "folders_removed": [f["folder"] for f in removed]})
    for f in removed:
        logger("folder_removed", it | {"old_path": f["folder"], "new_path": None,
                                       "reason": "no book left; leftovers quarantined"},
               details={"batch": str(batch), "junk_moved": f["junk_moved"]})
    say(f"{it['op']}d {src} -> {dst} (+{len(done) - 1} sidecars)"
        + (f", removed {len(removed)} empty folder(s)" if removed else ""))
    return it["op"]


def _quarantine(it: dict[str, Any], s, db, logger: ActionLog, say) -> str:
    src, keeper = Path(it["old_path"]), Path(it["new_path"])
    batch = s.quarantine_root() / date.today().isoformat()
    try:
        rel = src.resolve().relative_to(Path(s.media_root).resolve())
    except ValueError:
        rel = Path(*src.parts[1:])
    sides = [{"src": str(p), "dst": str(_sidecar_dst(src, keeper, p, k))}
             for p, k in book_sidecars(src)]
    plan = {"batch_dir": str(batch), "items": [{
        "group": None, "media_id": None, "keeper": str(keeper), "path": str(src),
        "quarantine_path": str(batch / rel), "size_bytes": src.stat().st_size, "stage": None,
        "sidecars_to_copy": [x for x in sides if not Path(x["dst"]).exists()]}]}
    res = qmod.execute(plan, db, progress=None, source=logger.source,
                       media_root=Path(s.media_root))
    r = res["results"][0]
    if r["result"] != "moved":
        _status(db, it["suggestion_id"], "needs_review")
        logger("needs_review", it | {"reason": f"quarantine refused: {r.get('reason')}"})
        say(f"NEEDS_REVIEW {src}: quarantine refused ({r.get('reason')})")
        return "needs_review"
    _status(db, it["suggestion_id"], "quarantined")
    logger("quarantine", it | {"new_path": r["quarantine_path"]},
           details={"manifest": res["manifest"], "keeper": str(keeper),
                    "sidecars_copied_to_keeper": r.get("sidecars_copied_to_keeper", []),
                    "folders_removed": res.get("folders_removed", [])})
    say(f"quarantined duplicate {src} (identical to {keeper})")
    return "quarantine"


def autoapply(sugs: list[dict[str, Any]], s, db=None, execute: bool = False,
              limit: int | None = None, source: str = "cli", run_id: str | None = None,
              progress: Callable[[str], None] | None = None,
              hasher=full_hash) -> dict[str, Any]:
    """Dry run (default) or execute. ``sugs``: open book suggestions, oldest first."""
    say = progress or (lambda _m: None)
    run_id = run_id or "books-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    logger = ActionLog(db, Path(s.report_dir), run_id, source)
    sugs = sugs[:limit] if limit else sugs
    items, counts = [], {}
    for k, sug in enumerate(sugs, 1):
        it = assess(sug, s, hasher)
        say(f"[{k}/{len(sugs)}] {it['confidence']:.2f} "
            f"{'PASS' if it['passes'] else 'below'} {it['threshold']:.2f} -> {it['decision']}"
            f" {it['old_path']}" + (f"  ({it['reason']})" if it["reason"] else ""))
        if execute:
            if it["decision"] == APPLY:
                it["result"] = _apply(it, s, db, logger, say)
            elif it["decision"] == "quarantine":
                it["result"] = _quarantine(it, s, db, logger, say)
            elif it["decision"] == "needs_review":
                _status(db, it["suggestion_id"], "needs_review")
                logger("needs_review", it)
                it["result"] = "needs_review"
            else:
                if it["reason"] == "source missing":
                    _status(db, it["suggestion_id"], "failed")
                logger("skip", it)
                it["result"] = "skip"
        key = it.get("result") or it["decision"]
        counts[key] = counts.get(key, 0) + 1
        items.append(it)
    return {"dry_run": not execute, "run_id": run_id if execute else None,
            "threshold": s.book_auto_min_confidence, "considered": len(items),
            "counts": counts, "items": items}


def format_result(res: dict[str, Any]) -> str:
    head = "DRY RUN - nothing changed. Add --execute to apply." if res["dry_run"] else \
        f"run {res['run_id']} (undo: aibrarian books undo --run {res['run_id']})"
    lines = [head, f"threshold {res['threshold']:.2f}",
             "id\tconfidence\tpasses\tdecision\ttitle\tauthor\tsource\ttarget\treason"]
    for i in res["items"]:
        lines.append("\t".join(str(x if x is not None else "") for x in (
            i["suggestion_id"], f"{i['confidence']:.2f}", "yes" if i["passes"] else "no",
            i.get("result") or i["decision"], i["title"], i["author"], i["old_path"],
            i["new_path"], i["reason"])))
    lines.append("TOTAL " + ", ".join(f"{k}: {v}" for k, v in sorted(res["counts"].items())))
    return "\n".join(lines) + "\n"


# -- undo -----------------------------------------------------------------------

UNDOABLE = ("rename", "move", "quarantine")


def undo(actions: list[dict[str, Any]], s, db=None, execute: bool = False,
         source: str = "cli", progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Reverse move/rename/quarantine actions (newest first). Dry run by default.
    Never overwrites; folders removed by the action are recreated with their leftovers."""
    say = progress or (lambda _m: None)
    run_id = "undo-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    logger = ActionLog(db, Path(s.report_dir), run_id, source)
    out = []
    for a in sorted(actions, key=lambda a: -a["id"]):
        d = a.get("details") or {}
        if isinstance(d, str):
            d = json.loads(d)
        item = {"id": a["id"], "action": a["action"], "old_path": a["old_path"],
                "new_path": a["new_path"]}
        if a["action"] not in UNDOABLE:
            out.append(item | {"result": "not undoable"})
            continue
        if a.get("undone_by"):
            out.append(item | {"result": f"already undone (#{a['undone_by']})"})
            continue
        if a["action"] == "quarantine":
            r = qmod.restore(Path(d["manifest"]), execute, db, [a["old_path"]], source)
            res = r["items"][0]["result"] if r["items"] else "not in manifest"
            ok = res in ("restored", "would restore")
        else:
            moves = d.get("moves") or []
            blocked = [m for m in moves if not Path(m["to"]).exists() or Path(m["from"]).exists()]
            main_blocked = not moves or moves[0] in blocked
            ok, res = not main_blocked, "would undo" if not execute else "undone"
            if main_blocked:
                res = "blocked: file not at new path or old path occupied"
            elif execute:
                for m in reversed(moves):
                    if m in blocked:
                        continue
                    Path(m["from"]).parent.mkdir(parents=True, exist_ok=True)
                    os.rename(m["to"], m["from"])
                if d.get("batch") and Path(d["batch"]).is_dir():
                    qmod._restore_folders(Path(d["batch"]), [Path(a["old_path"])])
                root = _within(Path(a["new_path"]).parent, library_roots(s))
                if root:
                    remove_empty_parents(Path(a["new_path"]).parent, root)
        if ok and execute:
            if db is not None and a.get("suggestion_id"):
                db.set_suggestion_status(a["suggestion_id"], "undone")
            uid = logger("undo", {**a, "old_path": a["new_path"], "new_path": a["old_path"]},
                         reason=f"undo of #{a['id']} ({a['action']})",
                         details={"undoes": a["id"]})
            if db is not None and uid and hasattr(db, "mark_library_undone"):
                db.mark_library_undone(a["id"], uid)
            res = "undone"
        say(f"#{a['id']} {a['action']}: {res} {a['new_path']} -> {a['old_path']}")
        out.append(item | {"result": res})
    return {"dry_run": not execute, "items": out,
            "undone": sum(o["result"] == "undone" for o in out)}
