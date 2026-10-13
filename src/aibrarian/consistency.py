"""Read-only catalog <-> filesystem consistency check (+ an explicit, limited fixer).

``aibrarian consistency check`` walks the transcribe roots once and cross-checks them with
``aib.media_file``, ``aib.transcript``, ``aib.transcript_lang``, open suggestions,
``aib.file_op`` and ``aib.change_log``. Every ``*.transcript.json`` is classified:

==========================  ===============================================================
``linked``                  same-stem video on disk, catalog row (not missing) with a
                            transcript - fine (not reported as an issue)
``row_not_transcribed``     same-stem video + row, but the row has no ``aib.transcript``
                            (langcheck cannot link it -> NO_MEDIA). Typical after a move
                            the catalog did not see: discover registered the new path as a
                            NEW row; the transcript/segments stay on the old, now-missing
                            row (see ``stranded_transcript_twin``)
``row_flagged_missing``     same-stem video exists, its row is marked missing
``stale_row_path``          same-stem video exists without a row, and a row whose path is
                            gone matches it by size+name (or fingerprint) -> SAFE FIX
``needs_scan``              same-stem video exists, no row at all -> run ``discover``
``stem_mismatch``           no same-stem video, exactly one plausible video in the same
                            dir (title/part match) -> SAFE FIX: rename the transcript (+srt)
``stem_mismatch_review``    other videos in the dir, none clearly the right one
``duplicate_transcript``    the matching video already has its own transcript
``orphan_video_moved``      no video in the dir; change_log/file_op/catalog know where the
                            video went (moved/renamed/quarantined, transcript left behind)
``orphan``                  no video in the dir and no record of where it went
==========================  ===============================================================

Plus: rows whose path is gone, ``transcript_lang``/suggestion/file_op paths pointing at
missing files, videos without a row, rows sharing one inode, transcripts stranded on a
missing row whose file reappeared under a new path (same fingerprint), and ``pending``
change_log rows older than N minutes. Nothing is written except the JSON report.

``aibrarian consistency fix`` (dry run unless ``--execute``) only performs the two safe
classes (``stem_mismatch`` relink, ``stale_row_path`` repoint), each through
:func:`aibrarian.changelog.change`. It never deletes files or rows.
"""

from __future__ import annotations

import difflib
import json
import os
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aibrarian.config import VIDEO_EXTENSIONS

SUFFIX = ".transcript.json"
TRANSCRIPT_CLASSES = ("row_not_transcribed", "row_flagged_missing", "stale_row_path",
                      "needs_scan", "stem_mismatch", "stem_mismatch_review",
                      "duplicate_transcript", "orphan_video_moved", "orphan")
OTHER_CLASSES = ("row_path_missing", "transcript_lang_missing", "suggestion_source_missing",
                 "file_op_dst_missing", "video_no_row", "duplicate_inode",
                 "stranded_transcript_twin", "pending_stale")
ALL_CLASSES = TRANSCRIPT_CLASSES + OTHER_CLASSES
SAFE_FIXES = ("stem_mismatch", "stale_row_path")
DEFAULT_PENDING_MINUTES = 30
MIN_TITLE_RATIO = 0.6
_PART_RE = re.compile(r"(?:^|[ ._\-\[(])(?:cd|disc|disk|part|pt)[ ._\-]?(\d{1,2})(?=$|[ ._\-\])])",
                      re.IGNORECASE)
_JUNK = re.compile(r"\b(1080p|2160p|720p|480p|4k|uhd|bluray|blu ray|brrip|bdrip|webrip|web dl|"
                   r"web|hdtv|dvdrip|x264|x265|h264|h265|hevc|aac|ac3|dts|multi|remux|hdr|"
                   r"proper|repack|extended|unrated|subs?|dual|atmos|truehd)\b.*$")


# --- naming helpers -----------------------------------------------------------

def tstem(p: Path | str) -> str:
    n = Path(p).name
    return n[: -len(SUFFIX)] if n.endswith(SUFFIX) else Path(n).stem


def part_no(stem: str) -> int | None:
    m = _PART_RE.search(stem)
    return int(m.group(1)) if m else None


def norm_title(stem: str) -> str:
    s = stem.lower()
    s = _PART_RE.sub(" ", s)
    s = re.sub(r"[._\-\[\]()]+", " ", s)
    s = _JUNK.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def title_ratio(a: str, b: str) -> float:
    na, nb = norm_title(a), norm_title(b)
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:
        return 1.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def best_video_for(stem: str, videos: list[Path]) -> tuple[Path | None, float]:
    """The one video in the dir this transcript most plausibly belongs to."""
    tp = part_no(stem)
    cands = videos
    if tp is not None:
        same = [v for v in videos if part_no(v.stem) == tp]
        cands = same or [v for v in videos if part_no(v.stem) is None and len(videos) == 1]
    elif len(videos) > 1:
        unparted = [v for v in videos if part_no(v.stem) is None]
        cands = unparted or videos
    if not cands:
        return None, 0.0
    scored = sorted(((title_ratio(stem, v.stem), v) for v in cands), key=lambda x: -x[0])
    best, v = scored[0]
    if len(cands) == 1:
        return v, best
    if len(scored) > 1 and scored[1][0] >= best - 0.05:
        return None, best  # ambiguous
    return v, best


# --- filesystem scan ----------------------------------------------------------

def scan(roots: Iterable[Path], exclude: Iterable[Path] = (),
         tick: Callable[..., Any] | None = None) -> dict[str, dict[str, list[str]]]:
    """dir -> {'videos': [names], 'transcripts': [names], 'other': [names]}."""
    skip = {os.path.abspath(e) for e in exclude}
    out: dict[str, dict[str, list[str]]] = {}
    n = 0
    for root in roots:
        if not Path(root).is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith((".", "_quarantine"))
                                 and os.path.abspath(os.path.join(dirpath, d)) not in skip)
            vids, trs = [], []
            for f in filenames:
                if f.startswith("."):
                    continue
                if f.endswith(SUFFIX):
                    trs.append(f)
                elif Path(f).suffix.lower() in VIDEO_EXTENSIONS:
                    vids.append(f)
            if vids or trs:
                out[dirpath] = {"videos": sorted(vids), "transcripts": sorted(trs)}
            n += 1
            if tick and n % 200 == 0:
                tick(current=dirpath, dirs=n)
    return out


def _stat(p: str) -> os.stat_result | None:
    try:
        return os.stat(p)
    except OSError:
        return None


def _fingerprint(p: Path) -> str | None:
    from aibrarian.media import fingerprint

    try:
        return fingerprint(p)
    except OSError:
        return None


# --- the check ----------------------------------------------------------------

def check(snapshot: dict[str, list[dict[str, Any]]], roots: Iterable[Path],
          exclude: Iterable[Path] = (), pending: list[dict[str, Any]] | None = None,
          reporter: Any = None, fs: dict[str, dict[str, list[str]]] | None = None,
          stat: Callable[[str], Any] = _stat,
          fingerprint: Callable[[Path], str | None] = _fingerprint) -> dict[str, Any]:
    roots = [Path(r) for r in roots]
    tick = reporter.tick if reporter else None
    if reporter:
        reporter.start()
    fs = scan(roots, exclude, tick) if fs is None else fs
    media = snapshot.get("media", [])
    by_path = {m["path"]: m for m in media}
    issues: dict[str, list[dict[str, Any]]] = {k: [] for k in ALL_CLASSES}
    linked = 0

    exists_cache: dict[str, bool] = {}

    def exists(p: str | None) -> bool:
        if not p:
            return False
        if p not in exists_cache:
            exists_cache[p] = stat(p) is not None
        return exists_cache[p]

    for d, ent in fs.items():
        for v in ent["videos"]:
            exists_cache[os.path.join(d, v)] = True
        for t in ent["transcripts"]:
            exists_cache[os.path.join(d, t)] = True

    # rows whose file is gone (candidates for stale_row_path); index by size / name
    gone_rows = [m for m in media if not exists(m["path"])]
    gone_by_size: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for m in gone_rows:
        gone_by_size[int(m.get("size_bytes") or -1)].append(m)
    for m in gone_rows:
        if not m.get("missing"):
            issues["row_path_missing"].append(_mrow(m))

    def stale_match(video: Path) -> dict[str, Any] | None:
        st = stat(str(video))
        if st is None:
            return None
        cands = gone_by_size.get(st.st_size, [])
        same_name = [m for m in cands if Path(m["path"]).name == video.name]
        if len(same_name) == 1:
            return same_name[0] | {"matched_by": "size+name"}
        if cands:
            fp = fingerprint(video)
            hits = [m for m in cands if fp and m.get("fingerprint") == fp]
            if len(hits) == 1:
                return hits[0] | {"matched_by": "fingerprint"}
        return None

    # where did a video go? committed change_log / file_op moves keyed by old path
    moved_to: dict[str, str] = {}
    for f in snapshot.get("file_ops", []):
        if f.get("dst"):
            moved_to[f["src"]] = f["dst"]
    for c in snapshot.get("changes", []):
        if c.get("old_path") and c.get("new_path"):
            moved_to[c["old_path"]] = c["new_path"]
    rows_by_stem: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for m in media:
        rows_by_stem[Path(m["path"]).stem].append(m)

    def follow(p: str) -> str:
        seen = {p}
        while p in moved_to and moved_to[p] not in seen:
            p = moved_to[p]
            seen.add(p)
        return p

    tclass: dict[str, str] = {}
    for d, ent in sorted(fs.items()):
        vids = [Path(d) / v for v in ent["videos"]]
        vid_by_stem = {v.stem: v for v in vids}
        tr_stems = {tstem(t) for t in ent["transcripts"]}
        for t in ent["transcripts"]:
            tp = Path(d) / t
            s = tstem(t)
            item: dict[str, Any] = {"transcript": str(tp)}
            v = vid_by_stem.get(s)
            if v is not None:
                row = by_path.get(str(v))
                item["video"] = str(v)
                if row is None:
                    sm = stale_match(v)
                    if sm:
                        cls = "stale_row_path"
                        item |= {"media_id": sm["id"], "row_path": sm["path"],
                                 "matched_by": sm["matched_by"]}
                    else:
                        cls = "needs_scan"
                elif row.get("missing"):
                    cls = "row_flagged_missing"
                    item["media_id"] = row["id"]
                elif row.get("has_transcript"):
                    cls = "linked"
                else:
                    cls = "row_not_transcribed"
                    item |= {"media_id": row["id"], "stage": row.get("stage")}
            elif vids:
                bv, score = best_video_for(s, vids)
                item["candidates"] = [str(x) for x in vids]
                item["score"] = round(score, 2)
                if bv is not None and bv.stem in tr_stems:
                    cls = "duplicate_transcript"
                    item["video"] = str(bv)
                elif bv is not None and score >= MIN_TITLE_RATIO:
                    cls = "stem_mismatch"
                    item |= {"video": str(bv),
                             "relink_to": str(bv.with_name(bv.stem + SUFFIX))}
                    row = by_path.get(str(bv))
                    item["media_id"] = row["id"] if row else None
                else:
                    cls = "stem_mismatch_review"
            else:
                where = []
                for ext in VIDEO_EXTENSIONS:
                    old = str(Path(d) / (s + ext))
                    if old in moved_to:
                        where.append({"from": old, "to": follow(old), "via": "log"})
                if not where:
                    for m in rows_by_stem.get(s, []):
                        if Path(m["path"]).parent != Path(d):
                            where.append({"to": m["path"], "via": "catalog",
                                          "media_id": m["id"], "missing": m.get("missing")})
                if where:
                    cls = "orphan_video_moved"
                    item["video_now"] = where[:5]
                else:
                    cls = "orphan"
            tclass[str(tp)] = cls
            if cls == "linked":
                linked += 1
            else:
                issues[cls].append(item | {"class": cls})
        if tick:
            tick(current=d, transcripts=len(tclass))

    # videos without a row; inode duplicates
    on_disk = {str(Path(d) / v) for d, ent in fs.items() for v in ent["videos"]}
    for p in sorted(on_disk - set(by_path)):
        issues["video_no_row"].append({"path": p})
    inode: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for m in media:
        if exists(m["path"]):
            st = stat(m["path"])
            if st is not None and getattr(st, "st_ino", 0):
                inode[(st.st_dev, st.st_ino)].append(m)
    for (_dev, ino), ms in inode.items():
        if len(ms) > 1:
            issues["duplicate_inode"].append({"inode": ino, "rows": [_mrow(m) for m in ms]})

    # transcript/segments stranded on a missing row whose file now lives under a new row
    live_by_fp: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for m in media:
        if not m.get("missing") and m.get("fingerprint"):
            live_by_fp[m["fingerprint"]].append(m)
    for m in media:
        if m.get("missing") and m.get("has_transcript") and not exists(m["path"]):
            for twin in live_by_fp.get(m.get("fingerprint") or "", []):
                if not twin.get("has_transcript"):
                    issues["stranded_transcript_twin"].append(
                        {"stale": _mrow(m), "live": _mrow(twin)})

    # dependent tables
    logged_old = {c["old_path"] for c in snapshot.get("changes", []) if c.get("old_path")}
    no_media: Counter[str] = Counter()
    for r in snapshot.get("transcript_lang", []):
        if not exists(r["path"]):
            issues["transcript_lang_missing"].append(
                {"id": r["id"], "path": r["path"], "media_id": r.get("media_id"),
                 "logged_move": r["path"] in logged_old})
            if r.get("media_id") is None:
                no_media["transcript_missing"] += 1
        elif r.get("media_id") is None:
            no_media[tclass.get(r["path"], "outside_scanned_roots")] += 1
    for sg in snapshot.get("suggestions", []):
        if not exists(sg["source_path"]):
            issues["suggestion_source_missing"].append(
                {"id": sg["id"], "domain": sg["domain"], "source_path": sg["source_path"],
                 "status": sg["status"]})
    srcs = {f["src"] for f in snapshot.get("file_ops", [])}
    for f in snapshot.get("file_ops", []):
        if f.get("dst") and f["dst"] not in srcs and not exists(f["dst"]):
            if roots and not any(Path(f["dst"]).is_relative_to(r) for r in roots):
                continue  # only judge paths inside the scanned roots
            issues["file_op_dst_missing"].append({"id": f["id"], "op": f["op"], "src": f["src"],
                                                  "dst": f["dst"]})
    for c in pending or []:
        issues["pending_stale"].append({"id": c["id"], "ts": str(c["ts"]), "op": c["op"],
                                        "actor": c.get("actor"), "old_path": c.get("old_path"),
                                        "new_path": c.get("new_path")})

    counts = {k: len(v) for k, v in issues.items()}
    if reporter:
        reporter.finish(transcripts=len(tclass), **{k: v for k, v in counts.items() if v})
    return {"generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "roots": [str(r) for r in roots], "transcripts_on_disk": len(tclass),
            "linked": linked, "counts": counts, "no_media_breakdown": dict(no_media),
            "issues": issues}


def _mrow(m: dict[str, Any]) -> dict[str, Any]:
    return {k: m.get(k) for k in ("id", "path", "missing", "stage", "has_transcript",
                                  "size_bytes")}


# --- fix plan / fixer ---------------------------------------------------------

def fix_plan(rep: dict[str, Any]) -> list[dict[str, Any]]:
    """Dry-run actions. ``safe`` ones are what ``consistency fix --execute`` would do."""
    plan: list[dict[str, Any]] = []
    iss = rep["issues"]
    for it in iss.get("stem_mismatch", []):
        plan.append({"action": "relink_transcript", "safe": True, "from": it["transcript"],
                     "to": it["relink_to"], "video": it["video"],
                     "media_id": it.get("media_id")})
    for it in iss.get("stale_row_path", []):
        plan.append({"action": "update_row_path", "safe": True, "media_id": it["media_id"],
                     "from": it["row_path"], "to": it["video"], "matched_by": it["matched_by"]})
    hint = {"needs_scan": "run `aibrarian discover` (or wait for the discover flow)",
            "row_not_transcribed": "pipeline will reuse the sidecar at the transcribe stage; "
                                   "check stranded_transcript_twin for the old row",
            "row_flagged_missing": "run `aibrarian discover` (clears missing)",
            "stem_mismatch_review": "manual: pick the right video, rename the transcript",
            "duplicate_transcript": "manual: compare and quarantine the old-name transcript",
            "orphan_video_moved": "manual: move the transcript next to video_now "
                                  "(or let the pipeline re-transcribe)",
            "orphan": "manual: video gone (quarantined/deleted?) - transcript can be "
                      "quarantined",
            "stranded_transcript_twin": "manual merge: transcript/segments sit on the stale "
                                        "row; not automated",
            "pending_stale": "inspect: change started but never finished (crash?)"}
    for cls, h in hint.items():
        n = len(iss.get(cls, []))
        if n:
            plan.append({"action": "report_only", "safe": False, "class": cls, "count": n,
                         "hint": h})
    return plan


def execute_fixes(ctx: Any, rep: dict[str, Any], execute: bool = False,
                  only: Iterable[str] = SAFE_FIXES, limit: int | None = None,
                  source: str = "cli", say: Callable[[str], None] | None = None
                  ) -> dict[str, Any]:
    from aibrarian import changelog

    say = say or (lambda _m: None)
    plan = [p for p in fix_plan(rep) if p["safe"]]
    want = {"stem_mismatch": "relink_transcript", "stale_row_path": "update_row_path"}
    acts = {want[c] for c in only if c in want}
    plan = [p for p in plan if p["action"] in acts][: limit or None]
    results = []
    for p in plan:
        r = dict(p)
        src, dst = Path(p["from"]), Path(p["to"])
        try:
            if p["action"] == "relink_transcript":
                if not src.is_file() or dst.exists():
                    r["result"] = "skipped: source gone or target exists"
                elif not Path(p["video"]).is_file():
                    r["result"] = "skipped: video gone"
                elif not execute:
                    r["result"] = "would_relink"
                else:
                    ssrc = src.with_name(tstem(src) + ".srt")
                    sdst = dst.with_name(tstem(dst) + ".srt")
                    with changelog.change(ctx.db, "relink", "transcript", old_path=src,
                                          new_path=dst, entity_id=p.get("media_id"),
                                          media_ids=[p["media_id"]] if p.get("media_id") else [],
                                          extra_lock_paths=[p["video"]], source=source,
                                          details={"video": p["video"]}) as ch:
                        os.rename(src, dst)
                        ch.add_undo(lambda src=src, dst=dst: os.rename(dst, src))
                        if ssrc.is_file() and not sdst.exists():
                            os.rename(ssrc, sdst)
                            ch.add_undo(lambda a=ssrc, b=sdst: os.rename(b, a))
                            ch.sidecars([(ssrc, sdst)])
                        ch.repoint(src, dst)  # transcript_lang.path follows
                    r["result"] = "relinked"
            elif p["action"] == "update_row_path":
                row = ctx.db.get_media(p["media_id"])
                if not row or row["path"] != p["from"] or Path(p["from"]).exists():
                    r["result"] = "skipped: row changed or old path exists again"
                elif not dst.is_file() or ctx.db.get_media_by_path(str(dst)):
                    r["result"] = "skipped: file gone or already cataloged"
                elif not execute:
                    r["result"] = "would_update_path"
                else:
                    with changelog.change(ctx.db, "update_path", "media_file",
                                          old_path=src, new_path=dst,
                                          entity_id=p["media_id"], source=source,
                                          details={"matched_by": p.get("matched_by")}) as ch:
                        ch.unmissing = True
                        ch.repoint(src, dst)
                    r["result"] = "updated"
        except changelog.Busy as exc:
            r["result"] = f"skipped: busy ({exc})"
        except OSError as exc:
            r["result"] = f"error: {exc}"
        say(f"{r['result']}: {p['from']} -> {p['to']}")
        results.append(r)
    return {"execute": execute, "planned": len(plan),
            "done": sum(r["result"] in ("relinked", "updated") for r in results),
            "results": results}


# --- glue -----------------------------------------------------------------------

def run(ctx: Any, pending_minutes: int = DEFAULT_PENDING_MINUTES, reporter: Any = None,
        write: bool = True) -> dict[str, Any]:
    s = ctx.settings
    snap = ctx.db.consistency_snapshot()
    try:
        pending = ctx.db.stale_pending_changes(pending_minutes)
    except Exception:
        pending = []
    rep = check(snap, s.transcribe_paths(), [s.quarantine_root()], pending, reporter)
    rep["fix_plan"] = fix_plan(rep)
    rep["pending_minutes"] = pending_minutes
    if write:
        rep["report_path"] = str(write_report(rep, Path(s.report_dir)))
    return rep


def write_report(rep: dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / "consistency.json"
    tmp = p.with_name(".consistency.json.tmp")
    tmp.write_text(json.dumps(rep, default=str, indent=1))
    os.replace(tmp, p)
    return p


def load_report(report_dir: Path) -> dict[str, Any] | None:
    p = Path(report_dir) / "consistency.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def format_summary(rep: dict[str, Any], plan: bool = False, sample: int = 5) -> str:
    c = rep["counts"]
    lines = [f"consistency check {rep['generated_at']}  roots: {', '.join(rep['roots'])}",
             f"transcripts on disk: {rep['transcripts_on_disk']}  linked OK: {rep['linked']}",
             "", "TRANSCRIPTS NOT LINKED TO A TRANSCRIBED CATALOG ROW:"]
    for k in TRANSCRIPT_CLASSES:
        lines.append(f"  {k:<24} {c.get(k, 0)}")
    lines += ["", "OTHER:"]
    for k in OTHER_CLASSES:
        lines.append(f"  {k:<26} {c.get(k, 0)}")
    if rep.get("no_media_breakdown"):
        lines += ["", "aib.transcript_lang rows without media (requeue-lang NO_MEDIA) by class:"]
        lines += [f"  {k:<24} {v}" for k, v in sorted(rep["no_media_breakdown"].items(),
                                                      key=lambda x: -x[1])]
    for k in ALL_CLASSES:
        items = rep["issues"].get(k) or []
        if items and sample:
            lines += ["", f"{k} (first {min(sample, len(items))} of {len(items)}):"]
            lines += [f"  {json.dumps(i, default=str)[:300]}" for i in items[:sample]]
    if plan:
        lines += ["", "FIX PLAN (dry run; `consistency fix --execute` does only safe=yes):"]
        for p in rep.get("fix_plan") or fix_plan(rep):
            if p["action"] == "report_only":
                lines.append(f"  [manual] {p['class']} x{p['count']}: {p['hint']}")
            else:
                lines.append(f"  [safe]   {p['action']}: {p['from']} -> {p['to']}")
    if rep.get("report_path"):
        lines += ["", f"report: {rep['report_path']}"]
    return "\n".join(lines) + "\n"
