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
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .duplicates import full_hash

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


def _verify(keeper: Path, copy: Path, hasher=full_hash) -> str | None:
    """Return a reason to skip, or None if byte-identical right now."""
    if not keeper.is_file():
        return "keeper missing"
    if not copy.is_file():
        return "copy missing"
    if keeper.stat().st_size != copy.stat().st_size:
        return "size differs"
    if hasher(keeper) != hasher(copy):
        return "sha256 differs"
    return None


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


def execute(p: dict[str, Any], db=None, hasher=full_hash) -> dict[str, Any]:
    batch = Path(p["batch_dir"])
    results = []
    for it in p["items"]:
        src, dst, keeper = Path(it["path"]), Path(it["quarantine_path"]), Path(it["keeper"])
        res = {"group": it["group"], "path": it["path"], "quarantine_path": it["quarantine_path"],
               "size_bytes": it["size_bytes"]}
        started = datetime.now(UTC)
        try:
            why = _verify(keeper, src, hasher)
            if not why and dst.exists():
                why = "quarantine target already exists"
            if why:
                results.append(res | {"result": "skipped", "reason": why})
                log.warning("quarantine skipped", extra={"path": it["path"], "reason": why})
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
            from .sidecars import related_sidecars
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
        except OSError as exc:
            results.append(res | {"result": "error", "reason": str(exc)})
            log.error("quarantine failed", extra={"path": it["path"], "error": str(exc)})
    moved = [r for r in results if r["result"] == "moved"]
    return {"dry_run": False, "batch_dir": str(batch), "moved": len(moved),
            "moved_gb": round(sum(r["size_bytes"] for r in moved) / 1e9, 2),
            "skipped": sum(r["result"] == "skipped" for r in results),
            "errors": sum(r["result"] == "error" for r in results),
            "manifest": str(batch / "manifest.jsonl"), "results": results}


def restore(manifest: Path, execute: bool = False, db=None,
            paths: list[str] | None = None) -> dict[str, Any]:
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
    return {"dry_run": not execute, "manifest": str(manifest),
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
    return "\n".join(lines) + "\n"
