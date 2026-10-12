"""Automatic book librarian (``book-autoapply``) - Calibre-aware.

Per open book suggestion (oldest first, up to ``limit``), metadata is re-evaluated with
:func:`aibrarian.bookmeta.evaluate` (clean title/series/subtitle + evidence score):

* score < ``book_auto_min_confidence`` or no title/author -> ``needs_review`` (untouched)
* source must be a regular file under ``media_root`` (book_drop / downloads)

**Ebooks** go into the Calibre library through ``calibredb`` (never by copying into it):

* byte-identical to a format already in Calibre -> the drop copy is quarantined
* same book (ISBN or title+author) and Calibre lacks this format -> ``calibredb add_format``
* same book with this format but different bytes -> ``needs_review``
* else ``calibredb add`` with title/authors/series/index/isbn/tags
* after a successful add, the source is moved to quarantine (manifest + 30-day purge);
  if calibredb fails, the source is left alone and the suggestion is ``needs_review``

**Audiobooks/podcasts** are moved to ``<audiobooks>/Author/[Series/]Title/`` (Audiobookshelf
scans folders). Same mount: ``os.rename``. Other mount: copy, verify sha256, then the source
is quarantined. Nothing is ever overwritten.

Every outcome is logged in ``aib.library_action`` and ``<report_dir>/library-actions.log``.
``undo`` reverses calibre_add / add_format / move / rename / copy / quarantine actions.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import shlex
import shutil
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from aibrarian import quarantine as qmod
from aibrarian.bookmeta import evaluate
from aibrarian.calibre import Calibre
from aibrarian.config import AUDIO_EXTENSIONS, BOOK_EXTENSIONS
from aibrarian.duplicates import full_hash
from aibrarian.fileops import remove_empty_parents
from aibrarian.video_librarian import safe_component

log = logging.getLogger(__name__)
LOG_FILE = "library-actions.log"
FOLDER_SIDECARS = {"cover.jpg", "cover.jpeg", "cover.png", "folder.jpg", "metadata.opf",
                   "desc.txt", "reader.txt"}


def make_calibre(s) -> Calibre:
    return Calibre(Path(s.calibre_dir), Path(s.report_dir) / ".calibre.lock", s.calibredb_bin)


def book_sidecars(src: Path) -> list[tuple[Path, str]]:
    """``(path, kind)``; kind ``stem`` (renamed with the book) or ``folder`` (name kept)."""
    out: list[tuple[Path, str]] = []
    if not src.parent.is_dir():
        return out
    exts = BOOK_EXTENSIONS | AUDIO_EXTENSIONS
    books = [p for p in src.parent.iterdir() if p.is_file() and p.suffix.lower() in exts]
    for p in sorted(src.parent.iterdir()):
        if p == src or not p.is_file() or p.is_symlink() or p.suffix.lower() in exts:
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


def _within(p: Path, roots: list[Path]) -> Path | None:
    rp = p.resolve()
    return next((r for r in roots if rp.is_relative_to(r.resolve())), None)


def library_roots(s) -> list[Path]:
    return [Path(s.calibre_dir), Path(s.audiobooks_dir), Path(s.podcasts_dir)]


def _is_ebook(src: Path, kind: str | None) -> bool:
    return src.suffix.lower() in BOOK_EXTENSIONS and kind in (None, "ebook", "other")


def audio_target(s, ev: dict[str, Any], src: Path, sug: dict[str, Any]) -> Path | None:
    if ev["kind"] == "podcast":
        return Path(sug["target_path"]) if sug.get("target_path") else None
    if not ev["title"] or not ev["author"]:
        return None
    base = Path(s.audiobooks_dir) / safe_component(ev["author"])
    if ev["series"]:
        base = base / safe_component(ev["series"])
    title = ev["title"]
    if ev["series"] and ev["series_index"] is not None:
        title = f"{ev['series_index']} - {title}"
    return base / safe_component(title) / src.name


def assess(sug: dict[str, Any], s, calibre: Calibre | None = None,
           hasher=full_hash) -> dict[str, Any]:
    """Pure check (no writes). ``decision``: calibre_add | add_format | quarantine | move |
    needs_review | skip. Dry runs print exactly this."""
    src = Path(sug["source_path"])
    ev = evaluate(sug)
    thr = s.book_auto_min_confidence
    base = {"suggestion_id": sug["id"], "old_path": str(src), "new_path": None,
            "threshold": thr, "passes": ev["confidence"] >= thr, **ev}

    def out(decision: str, reason: str = "", **kw: Any) -> dict[str, Any]:
        return base | {"decision": decision, "reason": reason} | kw

    if sug.get("op") == "none" or (sug.get("target_path") or "") == str(src):
        return out("skip", "no-op suggestion")
    if not base["passes"]:
        return out("needs_review", f"score {ev['confidence']:.2f} < {thr:.2f} "
                                   f"({ev['score_reason']})")
    if not ev["title"] or not ev["author"]:
        return out("needs_review", "missing title or author")
    if not src.is_file():
        return out("skip", "source missing")
    if src.is_symlink() or not _within(src, [Path(s.media_root)]):
        return out("needs_review", "source not under media_root")

    if _is_ebook(src, ev["kind"]):
        cal = calibre or make_calibre(s)
        if not cal.db_path.exists():
            return out("needs_review", f"no Calibre library at {cal.library}")
        fmt = src.suffix.lower().lstrip(".").upper()
        try:
            dup = cal.find_duplicate(src, ev, hasher)
        except Exception as exc:  # sqlite busy/corrupt: never guess
            return out("needs_review", f"could not read Calibre DB: {exc}")
        if dup and dup["kind"] == "exact":
            return out("quarantine", f"identical file already in Calibre (book "
                       f"{dup['book']['id']})", new_path=dup["file"],
                       calibre_id=dup["book"]["id"])
        if dup and dup["has_format"]:
            return out("needs_review", f"Calibre already has {dup['book']['title']!r} (book "
                       f"{dup['book']['id']}, {dup['match']}) as {fmt} with different bytes",
                       calibre_id=dup["book"]["id"])
        if dup:
            args = ["add_format", "--dont-replace", str(dup["book"]["id"]), str(src)]
            return out("add_format", f"Calibre book {dup['book']['id']} ({dup['match']}) "
                       f"lacks {fmt}", calibre_id=dup["book"]["id"], format=fmt,
                       calibredb_cmd=shlex.join(cal.command(*args)))
        return out("calibre_add", "new book", format=fmt,
                   calibredb_cmd=shlex.join(cal.command(*Calibre.add_args(src, ev))))

    if ev["kind"] not in ("audiobook", "podcast"):
        return out("needs_review", f"unsupported kind {ev['kind']!r}")
    dst = audio_target(s, ev, src, sug)
    if dst is None:
        return out("needs_review", "no audiobook target")
    root = next((r for r in library_roots(s) if dst.parent.is_relative_to(r)), None)
    if root is not None and not root.is_dir():
        return out("needs_review", f"library root {root} does not exist (not mounted?)")
    if not (_within(_existing_ancestor(dst.parent), library_roots(s)) and
            _within(dst.parent, library_roots(s))) or ".." in dst.parts:
        return out("needs_review", "target outside the book library roots")
    if dst.exists():
        try:
            same = dst.is_file() and dst.stat().st_size == src.stat().st_size and \
                hasher(dst) == hasher(src)
        except OSError as exc:
            return out("needs_review", f"could not compare with existing target: {exc}")
        if same:
            return out("quarantine", "identical copy (sha256) already at target",
                       new_path=str(dst))
        return out("needs_review", "target exists with different content", new_path=str(dst))
    sides = [(p, k, _sidecar_dst(src, dst, p, k)) for p, k in book_sidecars(src)]
    clash = [str(d) for _, _, d in sides if d.exists()]
    if clash:
        return out("needs_review", f"sidecar target exists: {clash[0]}", new_path=str(dst))
    same_dev = src.stat().st_dev == _existing_ancestor(dst.parent).stat().st_dev
    return out("move", "same filesystem: rename" if same_dev else
               "other filesystem: copy + verify sha256 + quarantine source",
               new_path=str(dst), sidecars=[{"from": str(p), "to": str(d)} for p, _, d in sides])


class ActionLog:
    FIELDS = ("suggestion_id", "old_path", "new_path", "title", "author", "series",
              "series_index", "confidence")

    def __init__(self, db, log_dir: Path, run_id: str, source: str):
        self.db, self.file, self.run_id, self.source = db, log_dir / LOG_FILE, run_id, source

    def __call__(self, action: str, item: dict[str, Any], **kw: Any) -> int | None:
        row = {"ts": datetime.now(UTC).isoformat(), "run_id": self.run_id, "action": action,
               "domain": "book", "source": self.source} | \
              {k: item.get(k) for k in self.FIELDS} | {"reason": item.get("reason")} | kw
        if row.get("series_index") is not None:
            row["series_index"] = str(row["series_index"])
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


def _batch(s) -> Path:
    return s.quarantine_root() / date.today().isoformat()


def _quarantine_source(it: dict[str, Any], keeper: Path, s, db, logger: ActionLog,
                       why: str) -> dict[str, Any]:
    """Move the drop copy into quarantine (keeper must be byte-identical; re-verified)."""
    src = Path(it["old_path"])
    batch = _batch(s)
    try:
        rel = src.resolve().relative_to(Path(s.media_root).resolve())
    except ValueError:
        rel = Path(*src.parts[1:])
    sides = [{"src": str(p), "dst": str(_sidecar_dst(src, keeper, p, k))}
             for p, k in book_sidecars(src)] if not _within(keeper, [Path(s.calibre_dir)]) \
        else []  # never write into the Calibre library behind calibredb's back
    plan = {"batch_dir": str(batch), "items": [{
        "group": None, "media_id": None, "keeper": str(keeper), "path": str(src),
        "quarantine_path": str(batch / rel), "size_bytes": src.stat().st_size, "stage": None,
        "sidecars_to_copy": [x for x in sides if not Path(x["dst"]).exists()]}]}
    res = qmod.execute(plan, db, source=logger.source, media_root=Path(s.media_root))
    r = res["results"][0]
    if r["result"] == "moved":
        logger("quarantine", it | {"new_path": r["quarantine_path"], "reason": why},
               details={"manifest": res["manifest"], "keeper": str(keeper),
                        "folders_removed": res.get("folders_removed", [])})
    return r | {"manifest": res["manifest"]}


def _calibre_write(it, s, db, logger, cal: Calibre, say) -> str:
    src = Path(it["old_path"])
    sha = full_hash(src)
    if it["decision"] == "calibre_add":
        res = cal.add(src, it)
        bid = res.book_ids[0] if res.book_ids else None
    else:
        res = cal.add_format(int(it["calibre_id"]), src)
        bid = int(it["calibre_id"]) if res.ok else None
    details = {"calibre": res.as_log(), "calibre_id": bid, "sha256": sha,
               "format": it.get("format"), "library": str(cal.library)}
    if not res.ok or bid is None:
        _status(db, it["suggestion_id"], "needs_review")
        logger("error", it | {"reason": f"calibredb {it['decision']} failed "
                              f"(exit {res.exit_code}); source untouched"}, details=details)
        say(f"ERROR calibredb exit {res.exit_code} for {src}; needs_review")
        return "needs_review"
    book = next((b for b in cal.books() if b["id"] == bid), None)
    fmt_file = next((f["path"] for f in (book or {}).get("formats", [])
                     if f["format"] == it.get("format")), None)
    details["calibre_file"] = fmt_file
    q = _quarantine_source(it, Path(fmt_file), s, db, logger,
                           f"ingested into Calibre as book {bid}") if fmt_file else \
        {"result": "skipped", "reason": "Calibre file not found after add"}
    details["source_quarantine"] = {k: q.get(k) for k in ("result", "reason", "manifest",
                                                         "quarantine_path")}
    action_id = logger(it["decision"], it | {"new_path": fmt_file,
                                             "reason": f"Calibre book {bid}"}, details=details)
    _status(db, it["suggestion_id"], "applied")
    if q["result"] != "moved":
        logger("needs_review", it | {"reason": f"added to Calibre (book {bid}) but source not "
                                     f"quarantined: {q.get('reason')}"},
               details={"related_action": action_id})
    say(f"{it['decision']} -> Calibre book {bid} ({fmt_file}); source "
        f"{'quarantined' if q['result'] == 'moved' else 'kept: ' + str(q.get('reason'))}")
    return it["decision"]


def _copy_verify(src: Path, dst: Path) -> None:
    tmp = dst.with_name(f".{dst.name}.aibpart")
    shutil.copy2(src, tmp)
    with tmp.open("rb") as fh:
        os.fsync(fh.fileno())
    if full_hash(tmp) != full_hash(src):
        tmp.unlink()
        raise OSError("sha256 mismatch after copy")
    if dst.exists():
        tmp.unlink()
        raise FileExistsError(str(dst))
    os.rename(tmp, dst)


def _move_audio(it, s, db, logger, say) -> str:
    src, dst = Path(it["old_path"]), Path(it["new_path"])
    batch = _batch(s)
    pairs = [{"from": str(src), "to": str(dst)}] + it["sidecars"]
    done: list[dict[str, str]] = []
    method = "rename"
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            raise FileExistsError(str(dst))
        try:
            os.rename(src, dst)
            done.append(pairs[0])
            for m in pairs[1:]:
                if not Path(m["to"]).exists():
                    os.rename(m["from"], m["to"])
                    done.append(m)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            method = "copy"
            for m in pairs:
                if not Path(m["to"]).exists():
                    _copy_verify(Path(m["from"]), Path(m["to"]))
                    done.append(m)
    except OSError as exc:
        for m in reversed(done):  # roll back a partial move / remove partial copies
            if method == "rename" and not Path(m["from"]).exists():
                os.rename(m["to"], m["from"])
        if method == "copy" and done:
            qp = [_quarantine_copy_back(Path(m["to"]), Path(m["from"]), s, db, logger)
                  for m in done]
            log.warning("partial copy rolled back", extra={"files": qp})
        action = "needs_review" if isinstance(exc, FileExistsError) or \
            exc.errno == errno.EEXIST else "error"
        _status(db, it["suggestion_id"], "needs_review" if action == "needs_review" else "failed")
        logger(action, it | {"reason": str(exc)})
        say(f"{action.upper()} {src}: {exc}")
        return action
    details = {"moves": done, "method": method, "batch": str(batch)}
    if method == "copy":
        q = _quarantine_source(it, dst, s, db, logger, "copied to Audiobookshelf (verified)")
        details["source_quarantine"] = {k: q.get(k) for k in ("result", "reason", "manifest")}
        action = "copy"
    else:
        folders = qmod.remove_empty_folders(src.parent, Path(s.media_root), batch, True, db,
                                            logger.source)
        details["folders_removed"] = [f["folder"] for f in folders if f["result"] == "removed"]
        for f in folders:
            if f["result"] == "removed":
                logger("folder_removed", it | {"old_path": f["folder"], "new_path": None,
                                               "reason": "no book left; leftovers quarantined"},
                       details={"batch": str(batch), "junk_moved": f["junk_moved"]})
        action = "rename" if src.parent == dst.parent else "move"
    _status(db, it["suggestion_id"], "applied")
    logger(action, it | {"reason": f"score {it['confidence']:.2f}; {method}"}, details=details)
    say(f"{action} {src} -> {dst} (+{len(done) - 1} sidecars)")
    return action


TRASH_DIRNAME = ".aibrarian-trash"  # hidden, same mount; Audiobookshelf skips dot-folders


def _quarantine_copy_back(copy: Path, original: Path, s, db, logger) -> str | None:
    """Undo/rollback helper for a copy we created in a library mount: it is renamed
    (same mount, instant) into ``<library root>/.aibrarian-trash/<date>/`` - never deleted."""
    if not copy.exists() or not original.exists():
        return None
    root = _within(copy, library_roots(s))
    if root is None:
        return None
    dst = root / TRASH_DIRNAME / date.today().isoformat() / copy.resolve().relative_to(
        root.resolve())
    if dst.exists():
        return None
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.rename(copy, dst)
    logger("folder_removed" if copy.is_dir() else "quarantine",
           {"old_path": str(copy), "new_path": str(dst),
            "reason": f"extra copy of {original} moved to {TRASH_DIRNAME}"})
    return str(dst)


def autoapply(sugs: list[dict[str, Any]], s, db=None, execute: bool = False,
              limit: int | None = None, source: str = "cli", run_id: str | None = None,
              progress: Callable[[str], None] | None = None, hasher=full_hash,
              calibre: Calibre | None = None) -> dict[str, Any]:
    """Dry run (default) or execute. ``sugs``: open book suggestions, oldest first."""
    say = progress or (lambda _m: None)
    cal = calibre or make_calibre(s)
    run_id = run_id or "books-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    logger = ActionLog(db, Path(s.report_dir), run_id, source)
    sugs = sugs[:limit] if limit else sugs
    items, counts = [], {}
    for k, sug in enumerate(sugs, 1):
        it = assess(sug, s, cal, hasher)
        say(f"[{k}/{len(sugs)}] {it['confidence']:.2f} "
            f"{'PASS' if it['passes'] else 'below'} {it['threshold']:.2f} -> {it['decision']}: "
            f"{it['title']!r} by {it['author']!r}"
            + (f" [{it['series']} #{it['series_index']}]" if it["series"] else "")
            + f"  ({it['reason']})")
        if it.get("calibredb_cmd"):
            say(f"      {it['calibredb_cmd']}")
        if execute:
            try:
                d = it["decision"]
                if d in ("calibre_add", "add_format"):
                    it["result"] = _calibre_write(it, s, db, logger, cal, say)
                elif d == "move":
                    it["result"] = _move_audio(it, s, db, logger, say)
                elif d == "quarantine":
                    q = _quarantine_source(it, Path(it["new_path"]), s, db, logger, it["reason"])
                    ok = q["result"] == "moved"
                    _status(db, it["suggestion_id"], "quarantined" if ok else "needs_review")
                    if not ok:
                        logger("needs_review", it | {"reason": f"quarantine refused: "
                                                     f"{q.get('reason')}"})
                    it["result"] = "quarantine" if ok else "needs_review"
                elif d == "needs_review":
                    _status(db, it["suggestion_id"], "needs_review")
                    logger("needs_review", it)
                    it["result"] = "needs_review"
                else:
                    if it["reason"] == "source missing":
                        _status(db, it["suggestion_id"], "failed")
                    logger("skip", it)
                    it["result"] = "skip"
            except Exception as exc:  # one bad book must not stop the batch
                log.exception("book autoapply failed")
                logger("error", it | {"reason": f"{type(exc).__name__}: {exc}"})
                it["result"] = "error"
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
             "id\tscore\tpasses\tdecision\ttitle\tauthor\tseries\t#\tsubtitle\tsource\t"
             "target / calibredb\treason / score basis"]
    for i in res["items"]:
        lines.append("\t".join("" if x is None else str(x) for x in (
            i["suggestion_id"], f"{i['confidence']:.2f}", "yes" if i["passes"] else "no",
            i.get("result") or i["decision"], i["title"], i["author"], i["series"],
            i["series_index"], i["subtitle"], i["old_path"],
            i.get("calibredb_cmd") or i.get("new_path"),
            f"{i['reason']} | {i['score_reason']}")))
    lines.append("TOTAL " + ", ".join(f"{k}: {v}" for k, v in sorted(res["counts"].items())))
    return "\n".join(lines) + "\n"


# -- undo -----------------------------------------------------------------------

UNDOABLE = ("calibre_add", "add_format", "rename", "move", "copy", "quarantine")


def _restore_from(d: dict[str, Any], path: str, execute: bool, db, source: str) -> str:
    sq = d.get("source_quarantine") or {}
    manifest = d.get("manifest") or sq.get("manifest")
    if not manifest or not Path(manifest).exists():
        return "no manifest"
    r = qmod.restore(Path(manifest), execute, db, [path], source)
    return r["items"][0]["result"] if r["items"] else "not in manifest"


def undo(actions: list[dict[str, Any]], s, db=None, execute: bool = False,
         source: str = "cli", progress: Callable[[str], None] | None = None,
         calibre: Calibre | None = None) -> dict[str, Any]:
    """Reverse actions newest first. Dry run by default. Never overwrites or deletes:
    Calibre books are removed with ``calibredb remove`` (Calibre's trash bin), extra
    copies go to quarantine, quarantined sources are restored."""
    say = progress or (lambda _m: None)
    cal = calibre or make_calibre(s)
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
        extra: dict[str, Any] = {}
        if a["action"] == "quarantine":
            res = _restore_from(d, a["old_path"], execute, db, source)
            ok = res in ("restored", "would restore")
        elif a["action"] in ("calibre_add", "add_format"):
            back = _restore_from(d, a["old_path"], execute, db, source)
            src_ok = Path(a["old_path"]).exists() or back in ("restored", "would restore",
                                                                "already restored")
            if not src_ok:
                ok, res = False, f"blocked: source not restorable ({back})"
            elif not execute:
                ok, res = True, (f"would restore source and run calibredb "
                                 f"{'remove' if a['action'] == 'calibre_add' else 'remove_format'}"
                                 f" {d.get('calibre_id')}")
            else:
                r = cal.remove(int(d["calibre_id"])) if a["action"] == "calibre_add" else \
                    cal.remove_format(int(d["calibre_id"]), d.get("format") or "")
                extra = {"calibre": r.as_log()}
                ok, res = r.ok, "undone" if r.ok else f"calibredb exit {r.exit_code}"
        else:
            moves = d.get("moves") or []
            if a["action"] == "copy":
                back = _restore_from(d, a["old_path"], execute, db, source)
                ok = back in ("restored", "would restore", "already restored")
                res = "would restore source and quarantine the copy" if ok and not execute \
                    else back
                if ok and execute:
                    for m in moves:
                        _quarantine_copy_back(Path(m["to"]), Path(m["from"]), s, db, logger)
                    res = "undone"
            else:
                blocked = [m for m in moves
                           if not Path(m["to"]).exists() or Path(m["from"]).exists()]
                ok = bool(moves) and moves[0] not in blocked
                res = "would undo" if ok else "blocked: file not at new path or old path occupied"
                if ok and execute:
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
                         details={"undoes": a["id"]} | extra)
            if db is not None and uid and hasattr(db, "mark_library_undone"):
                db.mark_library_undone(a["id"], uid)
            res = "undone"
        say(f"#{a['id']} {a['action']}: {res}")
        out.append(item | {"result": res})
    return {"dry_run": not execute, "items": out,
            "undone": sum(o["result"] == "undone" for o in out)}


# -- validation -----------------------------------------------------------------

def validate(s, db=None, source: str = "cli", calibre: Calibre | None = None,
             progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Read-only Calibre check; result logged as a ``validate`` library_action."""
    cal = calibre or make_calibre(s)
    ingests = []
    if db is not None and hasattr(db, "library_actions"):
        for act in ("calibre_add", "add_format"):
            rows, _ = db.library_actions(action=act, limit=100000)
            ingests += [r for r in rows if not r.get("undone_by")]
    res = cal.validate(ingests)
    res["checked_ingests"] = len(ingests)
    summary = "ok" if res["ok"] else ", ".join(
        f"{k}: {len(v)}" for k, v in res["issues"].items() if v)
    run_id = "validate-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    ActionLog(db, Path(s.report_dir), run_id, source)(
        "validate", {"old_path": str(cal.library)},
        reason=f"{res['books']} books, {len(ingests)} logged ingests: {summary}", details=res)
    if progress:
        progress(f"validate: {summary}")
    return res
