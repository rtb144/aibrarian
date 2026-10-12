"""Quarantine extra copies of byte-identical duplicates. Never deletes anything.

Only ``identical`` groups from the duplicates report are considered. Dry run by
default; with ``execute=True`` each extra copy is:

1. re-checked: keeper and copy exist, sizes equal, fresh full sha256 equal
   (no cached hash is trusted), else skipped;
2. its sidecars that exist only next to it are *copied* next to the keeper
   (renamed to the keeper's stem, never overwriting);
3. moved (``os.rename``; same filesystem, instant) to
   ``<quarantine root>/<YYYY-MM-DD>/<path relative to media_root>``;
4. appended to ``manifest.jsonl`` + ``manifest.tsv`` in that dated folder, and the
   catalog row is repointed, marked ``missing`` + stage ``skipped`` with a
   ``quarantine`` event, so the pipeline and the duplicates report ignore it.

``restore`` reads a manifest and moves files back (never overwriting).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from aibrarian.duplicates import full_hash  # absolute: Prefect-safe

log = logging.getLogger(__name__)
QUARANTINE_DIRNAME = "_quarantine"


def quarantine_root(settings) -> Path:
    return Path(settings.quarantine_dir or Path(settings.media_root) / QUARANTINE_DIRNAME)


def plan(rep: dict[str, Any], settings, groups: list[int] | None = None,
         limit: int | None = None, day: date | None = None) -> dict[str, Any]:
    """Pure plan from a duplicates report. ``group`` ids are the 1-based identical
    group numbers shown by ``aibrarian duplicates --tsv`` and /duplicates/ui."""
    media_root = Path(settings.media_root)
    batch = quarantine_root(settings) / (day or date.today()).isoformat()
    items: list[dict[str, Any]] = []
    for gi, g in enumerate(rep.get("identical", []), 1):
        if groups and gi not in groups:
            continue
        keep = g["suggested_keep"]
        for c in g["copies"]:
            if c["path"] == keep:
                continue
            src = Path(c["path"])
            try:
                rel = src.relative_to(media_root)
            except ValueError:
                rel = Path(*src.parts[1:])  # outside media_root: keep full path under batch
            sidecars = [o for o in g.get("sidecars_only_on_others", []) if o["from"] == c["path"]]
            items.append({"group": gi, "media_id": c.get("media_id"), "keeper": keep,
                          "path": c["path"], "quarantine_path": str(batch / rel),
                          "size_bytes": c["size_bytes"], "stage": c.get("stage"),
                          "sidecars_to_copy": [{"src": o["sidecar"], "dst": o["would_be"]}
                                               for o in sidecars]})
        if limit and len(items) >= limit:
            break
    if limit:
        items = items[:limit]
    total = sum(i["size_bytes"] for i in items)
    return {"dry_run": True, "batch_dir": str(batch), "files": len(items),
            "groups": len({i["group"] for i in items}), "total_bytes": total,
            "total_gb": round(total / 1e9, 2), "items": items}


def _verify(keeper: Path, copy: Path, hasher=full_hash) -> tuple[str | None, str | None]:
    """``(reason to skip, sha256)``; reason is None if byte-identical right now."""
    if not keeper.is_file():
        return "keeper missing", None
    if not copy.is_file():
        return "copy missing", None
    if keeper.stat().st_size != copy.stat().st_size:
        return "size differs", None
    hk, hc = hasher(keeper), hasher(copy)
    if hk != hc:
        return "sha256 differs", hc
    return None, hc


ACTION_LOG = "dedup-actions.log"  # at the quarantine root; purge never touches it


def log_action(root: Path, db, source: str | None, **row: Any) -> dict[str, Any]:
    """Durable dedup log: JSONL line in ``<root>/dedup-actions.log`` + aib.dedup_action."""
    row = {"ts": row.pop("ts", None) or datetime.now(UTC).isoformat(), "source": source} | row
    try:
        root.mkdir(parents=True, exist_ok=True)
        with (root / ACTION_LOG).open("a") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
    except OSError as exc:
        log.error("dedup log write failed", extra={"error": str(exc)})
    if db is not None and hasattr(db, "record_dedup_action"):
        try:
            db.record_dedup_action(row)
        except Exception as exc:  # the file move already happened; never fail on logging
            log.error("dedup_action insert failed", extra={"error": str(exc)})
    return row


def backfill_log(root: Path, db) -> int:
    """Insert quarantine/restore/purge rows for manifests written before the action log
    existed (idempotent: ON CONFLICT on action+path+ts)."""
    n = 0
    if db is None or not hasattr(db, "record_dedup_action"):
        return 0
    batches = list(_batches(root))
    arch = root / ARCHIVE_DIRNAME
    if arch.is_dir():
        batches += [d for d in arch.iterdir() if (d / "manifest.jsonl").exists()]
    for b in batches:
        by_path = {}
        for r in _jsonl(b / "manifest.jsonl"):
            by_path[r["path"]] = r
            db.record_dedup_action({
                "ts": r.get("moved_at") or b.name, "action": "quarantine",
                "group_no": r.get("group"), "media_id": r.get("media_id"),
                "keeper_path": r.get("keeper"), "file_path": r["path"],
                "quarantine_path": r.get("quarantine_path"), "size_bytes": r.get("size_bytes"),
                "sidecars_copied": r.get("sidecars_copied_to_keeper") or [],
                "source": "backfill"})
            n += 1
        for name, action in (("restored.jsonl", "restore"), ("purged.jsonl", "purge")):
            for r in _jsonl(b / name):
                m = by_path.get(r["path"], {})
                db.record_dedup_action({
                    "ts": r.get("at"), "action": action, "media_id": m.get("media_id"),
                    "keeper_path": m.get("keeper"), "file_path": r["path"],
                    "quarantine_path": m.get("quarantine_path"),
                    "size_bytes": m.get("size_bytes"), "source": "backfill"})
                n += 1
    return n


def _write_manifest(batch: Path, rec: dict[str, Any]) -> None:
    with (batch / "manifest.jsonl").open("a") as fh:
        fh.write(json.dumps(rec, default=str) + "\n")
    tsv = batch / "manifest.tsv"
    cols = ["moved_at", "group", "media_id", "size_bytes", "keeper", "path", "quarantine_path"]
    new = not tsv.exists()
    with tsv.open("a") as fh:
        if new:
            fh.write("\t".join(cols) + "\n")
        fh.write("\t".join(str(rec.get(k, "")) for k in cols) + "\n")


def execute(p: dict[str, Any], db=None, hasher=full_hash,
            progress: Callable[[str], None] | None = None,
            source: str | None = "cli", media_root: Path | None = None) -> dict[str, Any]:
    """Move the planned copies. With ``media_root`` set, source folders left without
    media are removed afterwards (junk quarantined, see remove_empty_folders)."""
    batch = Path(p["batch_dir"])
    root = batch.parent
    results = []
    say = progress or (lambda _m: None)
    n = len(p["items"])
    for k, it in enumerate(p["items"], 1):
        src, dst, keeper = Path(it["path"]), Path(it["quarantine_path"]), Path(it["keeper"])
        res = {"group": it["group"], "path": it["path"], "quarantine_path": it["quarantine_path"],
               "size_bytes": it["size_bytes"]}
        started = datetime.now(UTC)
        say(f"[{k}/{n}] group {it['group']}: verifying sha256 "
            f"({it['size_bytes'] / 1e9:.2f} GB x2) {src}")
        base = {"group_no": it["group"], "media_id": it["media_id"], "keeper_path": it["keeper"],
                "file_path": it["path"], "quarantine_path": it["quarantine_path"],
                "size_bytes": it["size_bytes"]}
        sha = None
        try:
            why, sha = _verify(keeper, src, hasher)
            if not why and dst.exists():
                why = "quarantine target already exists"
            if why:
                results.append(res | {"result": "skipped", "reason": why})
                log.warning("quarantine skipped", extra={"path": it["path"], "reason": why})
                say(f"[{k}/{n}] SKIPPED ({why}) {src}")
                log_action(root, db, source, action="skip", sha256=sha, reason=why, **base)
                continue
            copied = []
            for sc in it["sidecars_to_copy"]:
                s, d = Path(sc["src"]), Path(sc["dst"])
                if s.is_file() and not d.exists():
                    shutil.copy2(s, d)
                    copied.append(str(d))
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.stat().st_dev != dst.parent.stat().st_dev:
                raise OSError("quarantine dir is on another filesystem; refusing slow copy")
            # move the copy's own sidecars along so the original folder is left clean
            from aibrarian.sidecars import related_sidecars
            sides = [(sp, dst.with_name(dst.stem + sp.name[len(src.stem):]))
                     for sp in related_sidecars(src)]
            os.rename(src, dst)
            moved_sides = []
            for sp, sd in sides:
                if not sd.exists():
                    os.rename(sp, sd)
                    moved_sides.append({"from": str(sp), "to": str(sd)})
            rec = {"moved_at": datetime.now(UTC).isoformat(), "group": it["group"],
                   "media_id": it["media_id"], "size_bytes": it["size_bytes"],
                   "keeper": it["keeper"], "path": it["path"], "quarantine_path": str(dst),
                   "prev_stage": it.get("stage"), "sidecars_copied_to_keeper": copied,
                   "sidecars_moved": moved_sides}
            _write_manifest(batch, rec)
            if db is not None and it["media_id"]:
                db.quarantine_media(it["media_id"], it["path"], str(dst), started,
                                    {"keeper": it["keeper"], "group": it["group"],
                                     "sidecars_copied_to_keeper": copied})
            results.append(res | {"result": "moved", "sidecars_copied_to_keeper": copied})
            log_action(root, db, source, action="quarantine", sha256=sha,
                       sidecars_copied=copied, **base)
            say(f"[{k}/{n}] moved -> {dst}" + (f" (+{len(copied)} sidecars copied to keeper)"
                                                if copied else ""))
        except OSError as exc:
            results.append(res | {"result": "error", "reason": str(exc)})
            log.error("quarantine failed", extra={"path": it["path"], "error": str(exc)})
            say(f"[{k}/{n}] ERROR {exc} {src}")
            log_action(root, db, source, action="error", sha256=sha, reason=str(exc), **base)
    moved = [r for r in results if r["result"] == "moved"]
    folders: list[dict[str, Any]] = []
    if media_root is not None:
        for parent in dict.fromkeys(Path(r["path"]).parent for r in moved):
            folders += remove_empty_folders(parent, Path(media_root), batch, True, db, source,
                                            say)
    return {"dry_run": False, "batch_dir": str(batch), "moved": len(moved),
            "folders_removed": [f["folder"] for f in folders if f["result"] == "removed"],
            "moved_gb": round(sum(r["size_bytes"] for r in moved) / 1e9, 2),
            "skipped": sum(r["result"] == "skipped" for r in results),
            "errors": sum(r["result"] == "error" for r in results),
            "manifest": str(batch / "manifest.jsonl"), "results": results}


def restore(manifest: Path, execute: bool = False, db=None,
            paths: list[str] | None = None, source: str | None = "cli") -> dict[str, Any]:
    """Move files listed in ``manifest.jsonl`` back. Dry run unless ``execute``.
    Never overwrites; sidecars copied to the keeper are left in place."""
    recs = [json.loads(line) for line in manifest.read_text().splitlines() if line.strip()]
    restored_log = manifest.with_name("restored.jsonl")
    done = set()
    if restored_log.exists():
        done = {json.loads(x)["path"] for x in restored_log.read_text().splitlines() if x.strip()}
    out = []
    for r in recs:
        if paths and r["path"] not in paths and r["quarantine_path"] not in paths:
            continue
        src, dst = Path(r["quarantine_path"]), Path(r["path"])
        item = {"path": r["path"], "quarantine_path": r["quarantine_path"]}
        if r["path"] in done:
            out.append(item | {"result": "already restored"})
            continue
        if not src.exists():
            out.append(item | {"result": "skipped", "reason": "not in quarantine"})
            continue
        if dst.exists():
            out.append(item | {"result": "skipped", "reason": "original path is occupied"})
            continue
        if not execute:
            out.append(item | {"result": "would restore"})
            continue
        started = datetime.now(UTC)
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.rename(src, dst)
        for s in r.get("sidecars_moved", []):
            if Path(s["to"]).exists() and not Path(s["from"]).exists():
                os.rename(s["to"], s["from"])
        with restored_log.open("a") as fh:
            fh.write(json.dumps({"path": r["path"], "at": started.isoformat()}) + "\n")
        if db is not None and r.get("media_id"):
            db.restore_media(r["media_id"], r["quarantine_path"], r["path"],
                             r.get("prev_stage") or "discovered", started)
        out.append(item | {"result": "restored"})
        log_action(manifest.parent.parent, db, source, action="restore",
                   group_no=r.get("group"), media_id=r.get("media_id"),
                   keeper_path=r.get("keeper"), file_path=r["path"],
                   quarantine_path=r["quarantine_path"], size_bytes=r.get("size_bytes"))
    restored = [Path(o["path"]) for o in out if o["result"] == "restored"]
    folders = _restore_folders(manifest.parent, restored) if restored else 0
    return {"dry_run": not execute, "manifest": str(manifest), "folders_recreated": folders,
            "restored": sum(o["result"] == "restored" for o in out), "items": out}


def format_plan(res: dict[str, Any]) -> str:
    """Human table for a plan (dry run) or an execute result."""
    gb = lambda b: f"{b / 1e9:.2f}"  # noqa: E731
    if res.get("dry_run"):
        lines = ["DRY RUN - nothing moved. Add --execute to move these files.",
                 "group\tsize_gb\tkeeper\textra_copy\tquarantine_path\tsidecars_to_copy"]
        for i in res["items"]:
            lines.append(f"{i['group']}\t{gb(i['size_bytes'])}\t{i['keeper']}\t{i['path']}\t"
                         f"{i['quarantine_path']}\t{len(i['sidecars_to_copy'])}")
        lines.append(f"TOTAL: {res['files']} files in {res['groups']} groups, "
                     f"{res['total_gb']} GB -> {res['batch_dir']}")
    else:
        lines = ["result\tgroup\tsize_gb\tpath\tquarantine_path / reason"]
        for r in res["results"]:
            lines.append(f"{r['result']}\t{r['group']}\t{gb(r['size_bytes'])}\t{r['path']}\t"
                         f"{r.get('reason') or r['quarantine_path']}")
        lines.append(f"MOVED {res['moved']} ({res['moved_gb']} GB), skipped {res['skipped']}, "
                     f"errors {res['errors']}. Manifest: {res['manifest']}")
        for f in res.get("folders_removed", []):
            lines.append(f"removed empty folder\t{f}")
    return "\n".join(lines) + "\n"


# -- retention ----------------------------------------------------------------

PURGE_CRON = "37 4 * * *"  # quarantine-purge deployment (daily 04:37 UTC)
_BOOKKEEPING = {"manifest.jsonl", "manifest.tsv", "restored.jsonl", "purged.jsonl",
                "folders.jsonl", "folders_restored.jsonl", "folders_purged.jsonl"}
ARCHIVE_DIRNAME = ".manifests"  # emptied batches' manifests are kept here (hidden)


def _jsonl(p: Path) -> list[dict[str, Any]]:
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def _moved_at(rec: dict[str, Any], batch: Path) -> datetime | None:
    try:
        t = datetime.fromisoformat(rec["moved_at"])
        return t if t.tzinfo else t.replace(tzinfo=UTC)
    except (KeyError, TypeError, ValueError):
        try:
            return datetime.fromisoformat(batch.name).replace(tzinfo=UTC)
        except ValueError:
            return None


def _inside(root: Path, p: Path) -> bool:
    """Hard safety check: ``p`` resolves (symlinks followed) strictly inside ``root``."""
    try:
        r, rp = root.resolve(strict=True), p.resolve(strict=True)
    except OSError:
        return False
    return rp != r and rp.is_relative_to(r)


def _batches(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(d for d in root.iterdir()
                  if d.is_dir() and not d.name.startswith(".") and (d / "manifest.jsonl").exists())


def _entries(root: Path, now: datetime) -> list[dict[str, Any]]:
    """Every manifest record still in quarantine (not restored, not purged)."""
    out = []
    for b in _batches(root):
        gone = {r["path"] for r in _jsonl(b / "restored.jsonl")} | \
               {r["path"] for r in _jsonl(b / "purged.jsonl")}
        for rec in _jsonl(b / "manifest.jsonl"):
            if rec["path"] in gone:
                continue
            t = _moved_at(rec, b)
            out.append({"batch": b, "rec": rec, "moved_at": t,
                        "age_days": (now - t).total_seconds() / 86400 if t else None})
    return out


def purge(root: Path, older_than_days: int, execute: bool = False, db=None,
          now: datetime | None = None,
          progress: Callable[[str], None] | None = None,
          source: str | None = "cli") -> dict[str, Any]:
    """Permanently delete quarantined copies older than ``older_than_days``.

    Only files listed in a manifest, only if they resolve inside ``root``, never
    symlinks or directories. Dry run unless ``execute``."""
    now = now or datetime.now(UTC)
    say = progress or (lambda _m: None)
    root = Path(root)
    items, freed = [], 0
    for e in _entries(root, now):
        rec, b = e["rec"], e["batch"]
        it = {"path": rec["path"], "quarantine_path": rec["quarantine_path"],
              "size_bytes": rec.get("size_bytes", 0), "moved_at": str(e["moved_at"]),
              "age_days": round(e["age_days"], 1) if e["age_days"] is not None else None}
        if e["age_days"] is None or e["age_days"] < older_than_days:
            items.append(it | {"result": "kept"})
            continue
        targets = [Path(rec["quarantine_path"])] + [Path(s["to"]) for s in
                                                    rec.get("sidecars_moved", [])]
        main = targets[0]
        if main.is_symlink() or not main.is_file() or not _inside(root, main):
            items.append(it | {"result": "skipped", "reason": "missing, not a regular file, "
                               "or outside the quarantine dir"})
            continue
        if not execute:
            items.append(it | {"result": "would delete"})
            say(f"would delete ({it['age_days']} days) {main}")
            continue
        started = datetime.now(UTC)
        deleted = []
        for t in targets:
            if t.is_file() and not t.is_symlink() and _inside(root, t):
                t.unlink()
                deleted.append(str(t))
        freed += it["size_bytes"] or 0
        with (b / "purged.jsonl").open("a") as fh:
            fh.write(json.dumps({"path": rec["path"], "at": started.isoformat(),
                                 "deleted": deleted}) + "\n")
        if db is not None and rec.get("media_id"):
            db.purge_media(rec["media_id"], rec["quarantine_path"], started,
                           {"age_days": it["age_days"], "deleted": deleted,
                            "keeper": rec.get("keeper")})
        items.append(it | {"result": "deleted"})
        log_action(root, db, source, action="purge", group_no=rec.get("group"),
                   media_id=rec.get("media_id"), keeper_path=rec.get("keeper"),
                   file_path=rec["path"], quarantine_path=rec["quarantine_path"],
                   size_bytes=rec.get("size_bytes"),
                   reason=f"older than {older_than_days} days; deleted {len(deleted)} file(s)")
        say(f"deleted ({it['age_days']} days) {main}")
    for b in _batches(root):
        gone = {r["folder"] for r in _jsonl(b / "folders_restored.jsonl")} | \
               {r["folder"] for r in _jsonl(b / "folders_purged.jsonl")}
        for rec in _jsonl(b / FOLDER_LOG):
            t = _moved_at({"moved_at": rec.get("at")}, b)
            if rec["folder"] in gone or not t or (now - t).days < older_than_days:
                continue
            junk = [Path(m["to"]) for m in rec["junk_moved"]]
            if execute:
                for j in junk:
                    if j.is_file() and not j.is_symlink() and _inside(root, j):
                        j.unlink()
                with (b / "folders_purged.jsonl").open("a") as fh:
                    fh.write(json.dumps({"folder": rec["folder"], "at": now.isoformat()}) + "\n")
            items.append({"path": rec["folder"], "quarantine_path": str(b / rec["rel"]),
                          "size_bytes": 0, "result": "deleted" if execute else "would delete",
                          "age_days": (now - t).days, "folder_leftovers": len(junk)})
    removed = _cleanup_batches(root) if execute else []
    return {"dry_run": not execute, "older_than_days": older_than_days, "root": str(root),
            "deleted": sum(i["result"] == "deleted" for i in items),
            "would_delete": sum(i["result"] == "would delete" for i in items),
            "freed_gb": round(freed / 1e9, 2), "removed_batch_dirs": removed, "items": items}


def _cleanup_batches(root: Path) -> list[str]:
    """Remove dated folders that hold no files except bookkeeping; their manifests
    are archived under ``<root>/.manifests/<date>/``."""
    removed = []
    for b in _batches(root):
        if any(f.is_file() and not (f.parent == b and f.name in _BOOKKEEPING)
               for f in b.rglob("*")) or not _inside(root, b):
            continue
        arch = root / ARCHIVE_DIRNAME / b.name
        arch.mkdir(parents=True, exist_ok=True)
        for name in _BOOKKEEPING:
            if (b / name).exists():
                os.replace(b / name, arch / name)
        for d in sorted((d for d in b.rglob("*") if d.is_dir()), key=lambda d: -len(d.parts)):
            d.rmdir()
        b.rmdir()
        removed.append(str(b))
    return removed


def next_cron_run(now: datetime, cron: str = PURGE_CRON) -> datetime:
    """Next run of a daily ``M H * * *`` cron (UTC)."""
    minute, hour = (int(x) for x in cron.split()[:2])
    t = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return t if t > now else t + timedelta(days=1)


def status(root: Path, retention_days: int, now: datetime | None = None) -> dict[str, Any]:
    """Read-only view of what is in quarantine and when it will be purged."""
    now = now or datetime.now(UTC)
    nxt = next_cron_run(now)
    rows = []
    for e in _entries(Path(root), now):
        r = e["rec"]
        due = e["moved_at"] + timedelta(days=retention_days) if e["moved_at"] else None
        # first purge run at/after the due time
        purge_on = next_cron_run(max(due, now) - timedelta(seconds=1)) if due else None
        rows.append({"path": r["path"], "quarantine_path": r["quarantine_path"],
                     "keeper": r.get("keeper"), "size_bytes": r.get("size_bytes", 0),
                     "moved_at": str(e["moved_at"]),
                     "age_days": round(e["age_days"], 1) if e["age_days"] is not None else None,
                     "purge_on": str(purge_on) if purge_on else None})
    total = sum(x["size_bytes"] or 0 for x in rows)
    return {"root": str(root), "retention_days": retention_days, "files": len(rows),
            "total_gb": round(total / 1e9, 2), "next_purge_run": str(nxt),
            "items": sorted(rows, key=lambda x: x["moved_at"])}


# -- empty source folders -------------------------------------------------------

JUNK_SUFFIXES = {".nfo", ".jpg", ".jpeg", ".png", ".txt", ".url"}
JUNK_NAMES = {"thumbs.db", ".ds_store"}
FOLDER_LOG = "folders.jsonl"
FOLDER_RESTORED = "folders_restored.jsonl"


def _is_junk(p: Path) -> bool:
    return p.name.lower() in JUNK_NAMES or p.suffix.lower() in JUNK_SUFFIXES


def _removable(folder: Path, media_root: Path, qroot: Path) -> bool:
    """At least two levels below media_root (never /media or a library root like
    /media/01-movies), not inside the quarantine dir, no symlinks in the way."""
    try:
        f, m = folder.resolve(strict=True), media_root.resolve(strict=True)
    except OSError:
        return False
    if folder.is_symlink() or not f.is_relative_to(m) or len(f.relative_to(m).parts) < 2:
        return False
    try:
        return not f.is_relative_to(qroot.resolve())
    except OSError:
        return True


def _junk_only(folder: Path) -> list[Path] | None:
    """All files under ``folder`` if every one is junk (and nothing is a symlink),
    else None. Subtitles / media / anything else make the folder non-empty."""
    files = []
    for p in folder.rglob("*"):
        if p.is_symlink():
            return None
        if p.is_file():
            if not _is_junk(p):
                return None
            files.append(p)
    return files


def remove_empty_folders(start: Path, media_root: Path, batch: Path, execute: bool = False,
                         db=None, source: str | None = "cli",
                         say: Callable[[str], None] | None = None) -> list[dict[str, Any]]:
    """Remove ``start`` and then its parents while they hold no media (only junk).
    Junk files are moved into ``batch`` under their media-relative path, never deleted;
    each removal is logged (``folder_removed``) and written to ``folders.jsonl``."""
    media_root, out = Path(media_root), []
    qroot = batch.parent
    folder = Path(start)
    while folder.is_dir() and _removable(folder, media_root, qroot):
        junk = _junk_only(folder)
        if junk is None:
            break
        rel = folder.relative_to(media_root)
        moves = [{"from": str(j), "to": str(batch / j.relative_to(media_root))} for j in junk]
        item = {"folder": str(folder), "junk_moved": moves}
        if not execute:
            out.append(item | {"result": "would remove"})
            if say:
                say(f"would remove empty folder {folder} ({len(junk)} leftover files)")
            folder = folder.parent
            continue  # report parents too, assuming this one would be gone
        try:
            for m in moves:
                Path(m["to"]).parent.mkdir(parents=True, exist_ok=True)
                if Path(m["to"]).exists():
                    raise OSError(f"quarantine target exists: {m['to']}")
                os.rename(m["from"], m["to"])
            for d in sorted((d for d in folder.rglob("*") if d.is_dir()),
                            key=lambda d: -len(d.parts)):
                d.rmdir()
            folder.rmdir()
        except OSError as exc:
            out.append(item | {"result": "error", "reason": str(exc)})
            log_action(qroot, db, source, action="error", file_path=str(folder),
                       reason=f"folder cleanup: {exc}")
            break
        batch.mkdir(parents=True, exist_ok=True)
        with (batch / FOLDER_LOG).open("a") as fh:
            fh.write(json.dumps({"at": datetime.now(UTC).isoformat(), "folder": str(folder),
                                 "rel": str(rel), "junk_moved": moves}) + "\n")
        log_action(qroot, db, source, action="folder_removed", file_path=str(folder),
                   quarantine_path=str(batch / rel), sidecars_copied=[m["to"] for m in moves],
                   reason=f"no media left; {len(moves)} leftover file(s) moved to quarantine")
        out.append(item | {"result": "removed"})
        if say:
            say(f"removed empty folder {folder} ({len(moves)} leftover files quarantined)")
        folder = folder.parent
    return out


def cleanup_empty(root: Path, media_root: Path, execute: bool = False, db=None,
                  source: str | None = "cli",
                  say: Callable[[str], None] | None = None) -> dict[str, Any]:
    """One-off: clean the original folders of everything still in quarantine."""
    seen, out = set(), []
    for e in _entries(Path(root), datetime.now(UTC)):
        folder = Path(e["rec"]["path"]).parent
        if folder in seen:
            continue
        seen.add(folder)
        out += remove_empty_folders(folder, media_root, e["batch"], execute, db, source, say)
    return {"dry_run": not execute,
            "removed": sum(o["result"] == "removed" for o in out),
            "would_remove": sum(o["result"] == "would remove" for o in out),
            "errors": sum(o["result"] == "error" for o in out), "folders": out}


def _restore_folders(batch: Path, restored_paths: list[Path]) -> int:
    """Recreate removed folders (and put their leftover junk back) that contain or
    are parents of a restored file."""
    done = {r["folder"] for r in _jsonl(batch / FOLDER_RESTORED)}
    n = 0
    for rec in _jsonl(batch / FOLDER_LOG):
        f = Path(rec["folder"])
        if rec["folder"] in done or not any(p.is_relative_to(f) for p in restored_paths):
            continue
        f.mkdir(parents=True, exist_ok=True)
        for m in rec["junk_moved"]:
            if Path(m["to"]).exists() and not Path(m["from"]).exists():
                Path(m["from"]).parent.mkdir(parents=True, exist_ok=True)
                os.rename(m["to"], m["from"])
        with (batch / FOLDER_RESTORED).open("a") as fh:
            fh.write(json.dumps({"folder": rec["folder"],
                                 "at": datetime.now(UTC).isoformat()}) + "\n")
        n += 1
    return n
