"""Safe file operations: apply suggestions (move/rename) with sidecars, audit
everything, and the maintenance utilities that used to be separate scripts
(apply_moves.py, apply_renames.py, delete_skip_dst_exists.py,
sync_srt_to_media_from_logs.py, compare_rename_move_srt.py).

Every function is dry-run by default; pass ``apply=True`` to touch disk.
Sidecars (.srt/.ass/.vtt/.nfo/.transcript.json) always travel with the video,
which fixes the old "subtitles left behind after rename" problem at the source.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from .sidecars import catalog_srt_files, related_sidecars


@dataclass
class OpResult:
    op: str
    src: str
    dst: str | None
    result: str  # ok | dry_run | skip_dst_exists | skip_missing | skip_outside_root | error
    error: str | None = None


AuditFn = Callable[[OpResult], None]


def _noop(_: OpResult) -> None:
    pass


def jsonl_audit(path: Path) -> AuditFn:
    path.parent.mkdir(parents=True, exist_ok=True)

    def write(r: OpResult) -> None:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": datetime.now(UTC).isoformat(), **asdict(r)}) + "\n")
    return write


def chain_audit(*fns: AuditFn) -> AuditFn:
    def write(r: OpResult) -> None:
        for f in fns:
            f(r)
    return write


def is_within(path: Path, roots: Iterable[Path]) -> bool:
    rp = path.resolve()
    return any(rp.is_relative_to(r.resolve()) for r in roots)


def remove_empty_parents(start: Path, stop: Path) -> None:
    cur = start
    stop = stop.resolve()
    while cur.resolve() != stop and cur.resolve().is_relative_to(stop):
        try:
            cur.rmdir()
        except OSError:
            return
        cur = cur.parent


def move_with_sidecars(src: Path, dst: Path, *, apply: bool, allowed_roots: Iterable[Path],
                       audit: AuditFn = _noop, cleanup_stop: Path | None = None
                       ) -> list[OpResult]:
    allowed_roots = list(allowed_roots)
    results: list[OpResult] = []

    def emit(r: OpResult) -> None:
        results.append(r)
        audit(r)

    op = "rename" if src.parent == dst.parent else "move"
    if not src.exists():
        emit(OpResult(op, str(src), str(dst), "skip_missing"))
        return results
    if not is_within(dst, allowed_roots):
        emit(OpResult(op, str(src), str(dst), "skip_outside_root"))
        return results
    if dst.exists():
        emit(OpResult(op, str(src), str(dst), "skip_dst_exists"))
        return results
    pairs = [(src, dst)] + [
        (sc, dst.with_name(dst.stem + sc.name[len(src.stem):])) for sc in related_sidecars(src)]
    for i, (a, b) in enumerate(pairs):
        kind = op if i == 0 else "sidecar"
        if not apply:
            emit(OpResult(kind, str(a), str(b), "dry_run"))
            continue
        if b.exists():
            emit(OpResult(kind, str(a), str(b), "skip_dst_exists"))
            continue
        try:
            b.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(a), str(b))
            emit(OpResult(kind, str(a), str(b), "ok"))
        except OSError as exc:
            emit(OpResult(kind, str(a), str(b), "error", str(exc)))
            if i == 0:
                return results
    if apply and cleanup_stop is not None:
        remove_empty_parents(src.parent, cleanup_stop)
    return results


def parse_path_maps(specs: Iterable[str]) -> list[tuple[str, str]]:
    out = []
    for spec in specs:
        if "=" not in spec:
            raise ValueError(f"path map must be FROM=TO, got {spec!r}")
        a, b = spec.split("=", 1)
        out.append((a.rstrip("/"), b.rstrip("/")))
    return out


def map_path(p: str, maps: list[tuple[str, str]]) -> str:
    for a, b in maps:
        if p == a or p.startswith(a + "/"):
            return b + p[len(a):]
    return p


def delete_skipped_duplicates(records: Iterable[dict], *, apply: bool,
                              path_maps: list[tuple[str, str]] | None = None,
                              audit: AuditFn = _noop) -> list[OpResult]:
    """For ops that were skipped because the destination already existed, delete
    the leftover source - but only if destination exists and sizes match (safer
    than the old delete_skip_dst_exists.py, which only checked existence)."""
    maps = path_maps or []
    out = []
    for rec in records:
        if rec.get("result") != "skip_dst_exists" or not rec.get("dst"):
            continue
        src, dst = Path(map_path(rec["src"], maps)), Path(map_path(rec["dst"], maps))
        if not src.is_file() or not dst.is_file():
            r = OpResult("delete", str(src), str(dst), "skip_missing")
        elif src.stat().st_size != dst.stat().st_size:
            r = OpResult("delete", str(src), str(dst), "error", "size mismatch; kept")
        elif not apply:
            r = OpResult("delete", str(src), str(dst), "dry_run")
        else:
            try:
                src.unlink()
                r = OpResult("delete", str(src), str(dst), "ok")
            except OSError as exc:
                r = OpResult("delete", str(src), str(dst), "error", str(exc))
        audit(r)
        out.append(r)
    return out


def build_successor_map(records: Iterable[dict]) -> dict[str, str]:
    """Final destination for every source path that was moved/renamed (follows chains)."""
    succ = {r["src"]: r["dst"] for r in records
            if r.get("result") == "ok" and r.get("op") in ("move", "rename") and r.get("dst")}
    final: dict[str, str] = {}
    for start in succ:
        cur, seen = start, {start}
        while cur in succ and succ[cur] not in seen:
            cur = succ[cur]
            seen.add(cur)
        final[start] = cur
    return final


def sync_orphan_subtitles(records: Iterable[dict], *, apply: bool,
                          audit: AuditFn = _noop) -> list[OpResult]:
    """Repair subtitles left behind by older tools: for each media file moved per
    the audit log, move sidecars still sitting next to the old path."""
    out: list[OpResult] = []
    for src, dst in build_successor_map(records).items():
        s, d = Path(src), Path(dst)
        for sc in related_sidecars(s):
            target = d.with_name(d.stem + sc.name[len(s.stem):])
            if target.exists():
                r = OpResult("sidecar", str(sc), str(target), "skip_dst_exists")
            elif not apply:
                r = OpResult("sidecar", str(sc), str(target), "dry_run")
            else:
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(sc), str(target))
                    r = OpResult("sidecar", str(sc), str(target), "ok")
                except OSError as exc:
                    r = OpResult("sidecar", str(sc), str(target), "error", str(exc))
            audit(r)
            out.append(r)
    return out


def subtitle_report(media_root: Path, records: Iterable[dict]) -> dict:
    """Cross-reference SRT files with move history: orphaned subtitles (no paired
    video) and moved media that lost their subtitle."""
    srts = list(catalog_srt_files(media_root))
    moved = build_successor_map(records)
    lost = [{"media": dst, "old": src} for src, dst in moved.items()
            if Path(dst).exists() and not Path(dst).with_suffix(".srt").exists()
            and Path(src).with_suffix(".srt").exists()]
    return {"srt_total": len(srts), "orphans": [s for s in srts if not s["paired"]],
            "lost_after_move": lost}


def load_jsonl(path: Path) -> list[dict]:
    out = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def legacy_records(rows: Iterable[dict]) -> list[dict]:
    """Normalize old move_log/rename_log JSONL rows (varying key names) to
    {op, src, dst, result}."""
    def get(r: dict, *keys: str):
        for k in keys:
            if r.get(k):
                return r[k]
        return None
    out = []
    for r in rows:
        src = get(r, "src", "source", "from", "old_path")
        dst = get(r, "dst", "dest", "destination", "to", "new_path")
        if not src:
            continue
        res = get(r, "result", "status") or "ok"
        res = {"moved": "ok", "renamed": "ok", "applied": "ok"}.get(res, res)
        out.append({"op": get(r, "op", "action", "kind") or "move", "src": src, "dst": dst,
                    "result": res})
    return out


def file_size(p: str) -> int | None:
    try:
        return os.path.getsize(p)
    except OSError:
        return None
