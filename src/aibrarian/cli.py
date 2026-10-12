"""``aibrarian`` command line. Every mutating command is dry-run unless --apply."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

from .config import get_settings
from .logging_setup import setup_logging


def _ctx():
    from .actions import Context

    return Context.from_settings(get_settings())


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _records(log_path: Path | None, ctx) -> list[dict]:
    from . import fileops

    if log_path:
        return fileops.legacy_records(fileops.load_jsonl(log_path))
    return [dict(r) for r in ctx.db.file_ops()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aibrarian", description=__doc__)
    p.add_argument("--plain-logs", action="store_true", help="human-readable logs")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("migrate", help="apply SQL migrations")
    sub.add_parser("api", help="run the HTTP API")
    wk = sub.add_parser("worker", help="run Prefect deployments (serve)")
    wk.add_argument("group", nargs="?", default="all", choices=["gpu", "cpu", "all"])
    d = sub.add_parser("discover", help="scan transcribe roots into the catalog")
    d.add_argument("--root", action="append")

    ra = sub.add_parser("run-action", help="run one pipeline action")
    ra.add_argument("action")
    ra.add_argument("--id", type=int, help="media id; default: claim a batch")
    ra.add_argument("--limit", type=int, default=5)

    pl = sub.add_parser("pipeline", help="run all stages once without Prefect")
    pl.add_argument("--batch", type=int)

    sub.add_parser("status", help="stage counts, backlog and ETA (was movie_eta.py)")
    tl = sub.add_parser("timeline", help="event history of one item")
    tl.add_argument("id", type=int)
    rp = sub.add_parser("reprobe", help="refresh ffprobe metadata without changing stages")
    rp.add_argument("--id", type=int)
    rp.add_argument("--all", action="store_true", help="also items that already have media_info")
    rp.add_argument("--limit", type=int)
    rf = sub.add_parser("retry-failed")
    rf.add_argument("--id", type=int)

    ls = sub.add_parser("library-scan", help="identify + suggest placement (was librarian.py)")
    ls.add_argument("--max-items", type=int)
    ls.add_argument("--no-resume", action="store_true")
    bs = sub.add_parser("book-scan", help="(was book_librarian.py)")
    bs.add_argument("--path", action="append")
    bs.add_argument("--max-items", type=int)
    bs.add_argument("--no-resume", action="store_true")

    sg = sub.add_parser("suggestions", help="list suggestions (no-ops hidden)")
    sg.add_argument("--domain", choices=["video", "book"])
    sg.add_argument("--status", default="open")
    sg.add_argument("--include-noop", action="store_true")
    sg.add_argument("--tsv", action="store_true", help="tab-separated table")

    dp = sub.add_parser("duplicates", help="read-only duplicates report (never deletes)")
    dp.add_argument("--exact-only", action="store_true")
    dp.add_argument("--tsv", action="store_true")
    dp.add_argument("--duration-tol", type=float, default=2.0)

    rj = sub.add_parser("reject", help="dismiss suggestions by id")
    rj.add_argument("--id", type=int, action="append", required=True)

    ap = sub.add_parser("apply", help="apply suggestions; dry-run unless --apply. "
                        "With --id only those ids are touched (filters ignored)")
    ap.add_argument("--id", type=int, action="append", help="repeatable")
    ap.add_argument("--domain", choices=["video", "book"])
    ap.add_argument("--kind", help="movie|tv|music|ebook|audiobook|podcast")
    ap.add_argument("--op", choices=["move", "rename"])
    ap.add_argument("--min-confidence", type=float, default=0.85)
    ap.add_argument("--apply", action="store_true")

    cs = sub.add_parser("cleanup-skipped", help="(was delete_skip_dst_exists.py)")
    cs.add_argument("--log", type=Path, help="legacy move_log JSONL; default: aib.file_op")
    cs.add_argument("--path-map", action="append", default=[], help="FROM=TO")
    cs.add_argument("--apply", action="store_true")

    ss = sub.add_parser("sync-subtitles", help="(was sync_srt_to_media_from_logs.py)")
    ss.add_argument("--log", type=Path, action="append")
    ss.add_argument("--apply", action="store_true")

    sc = sub.add_parser("srt-catalog", help="(was catalog_srt_files.py)")
    sc.add_argument("--root", type=Path)
    sc.add_argument("--format", choices=["json", "csv"], default="json")
    sc.add_argument("--unpaired-only", action="store_true")
    sc.add_argument("--output", type=Path)

    sr = sub.add_parser("srt-report", help="(was compare_rename_move_srt.py)")
    sr.add_argument("--log", type=Path, action="append")

    se = sub.add_parser("search")
    se.add_argument("query")
    se.add_argument("--mode", default="hybrid", choices=["hybrid", "semantic", "keyword"])
    se.add_argument("--windows", action="store_true", help="search movie_windows")
    se.add_argument("--limit", type=int, default=10)
    se.add_argument("--title", help="restrict to one movie (fuzzy, e.g. '22 Jump Street (2014)')")
    se.add_argument("--id", type=int, help="restrict to media id")

    cl = sub.add_parser("clip", help="(was movie_clipper.py)")
    cl.add_argument("phrase")
    cl.add_argument("--id", type=int)
    cl.add_argument("--title")
    cl.add_argument("--match-index", type=int, default=0)
    cl.add_argument("--pad", type=float)

    oi = sub.add_parser("openlibrary-import", help="(was import_openlibrary_pg.py)")
    oi.add_argument("dump", type=Path)
    oi.add_argument("--batch-lines", type=int, default=100_000)
    oi.add_argument("--truncate", action="store_true")
    ol = sub.add_parser("openlibrary-lookup")
    ol.add_argument("--title")
    ol.add_argument("--author")
    ol.add_argument("--isbn")

    gd = sub.add_parser("gen-docs", help="(was gen_docs.py)")
    gd.add_argument("paths", nargs="*", type=Path)
    gd.add_argument("--out", type=Path, default=Path("docs/reference"))
    gd.add_argument("--compose", type=Path, action="append", default=[])
    gd.add_argument("--force", action="store_true")

    sub.add_parser("prefect-limits", help="create tag concurrency limits in Prefect")
    return p


def main(argv: list[str] | None = None) -> int:  # noqa: C901 - flat dispatcher
    args = build_parser().parse_args(argv)
    setup_logging("aibrarian-cli", json_logs=not args.plain_logs)
    s = get_settings()
    cmd = args.cmd

    if cmd == "api":
        from .api import main as api_main

        api_main()
        return 0
    if cmd == "worker":
        from .flows import main as flows_main

        flows_main(["serve", args.group])
        return 0
    if cmd == "srt-catalog":
        from .sidecars import catalog_srt_files

        rows = [r for r in catalog_srt_files(args.root or s.media_root)
                if not (args.unpaired_only and r["paired"])]
        out = args.output.open("w", newline="") if args.output else sys.stdout
        if args.format == "csv":
            w = csv.DictWriter(out, fieldnames=list(rows[0]) if rows else ["srt"])
            w.writeheader()
            w.writerows(rows)
        else:
            json.dump(rows, out, indent=2)
        return 0
    if cmd == "gen-docs":
        from .gendocs import discover_compose, generate
        from .ollama import OllamaClient

        paths = args.paths or [Path("src")]
        compose = args.compose or discover_compose([Path(".")])
        _print(generate(paths, args.out, OllamaClient.from_settings(s), compose,
                        force=args.force))
        return 0
    if cmd == "prefect-limits":
        import subprocess

        from .flows import CONCURRENCY

        for tag, n in CONCURRENCY.items():
            subprocess.run(["prefect", "concurrency-limit", "create", tag, str(n)], check=False)
        return 0

    ctx = _ctx()
    from . import services

    if cmd == "migrate":
        _print({"applied": ctx.db.migrate()})
    elif cmd == "discover":
        from .actions import discover

        _print(discover(ctx, args.root))
    elif cmd == "run-action":
        from .actions import run_action
        from .status import ACTIONS

        if args.id:
            _print({"outcome": run_action(ctx, args.action, args.id)})
        else:
            rows = ctx.db.claim(ACTIONS[args.action].requires.value, None, args.limit, "cli")
            _print([{"id": r["id"], "outcome": _safe_run(ctx, args.action, r)} for r in rows])
    elif cmd == "pipeline":
        from .actions import discover
        from .status import ACTIONS

        out: dict[str, Any] = {"discover": discover(ctx)}
        n = args.batch or s.transcribe_batch_size
        for action, spec in ACTIONS.items():
            rows = ctx.db.claim(spec.requires.value, None, n, "cli")
            out[action] = [_safe_run(ctx, action, r) for r in rows]
        _print(out)
    elif cmd == "status":
        from .eta import fmt

        st = services.pipeline_status(ctx)
        st["eta_human"] = fmt(st["eta"].get("eta_s"))
        _print(st)
    elif cmd == "timeline":
        _print(ctx.db.timeline(args.id))
    elif cmd == "reprobe":
        from .actions import reprobe, reprobe_all

        _print(reprobe(ctx, args.id) if args.id else reprobe_all(ctx, not args.all, args.limit))
    elif cmd == "retry-failed":
        _print({"reset": ctx.db.retry_failed(args.id)})
    elif cmd == "library-scan":
        _print(services.library_scan(ctx, args.max_items, not args.no_resume))
    elif cmd == "book-scan":
        paths = [Path(p) for p in args.path] if args.path else None
        _print(services.book_scan(ctx, paths, not args.no_resume, args.max_items))
    elif cmd == "suggestions":
        rows = services.suggestion_rows(ctx, args.domain, args.status, args.include_noop)
        if args.tsv:
            cols = ["id", "confidence", "decision", "op", "source_path", "target_path"]
            print("\t".join(cols))
            for r in rows:
                print("\t".join("" if r[k] is None else str(r[k]) for k in cols))
        else:
            _print(rows)
    elif cmd == "duplicates":
        from .duplicates import to_tsv

        rep = services.duplicates_report(ctx, args.exact_only, args.duration_tol)
        if args.tsv:
            print(to_tsv(rep), end="")
        else:
            _print(rep)
    elif cmd == "reject":
        _print([services.reject_suggestion(ctx, i) for i in args.id])
    elif cmd == "apply":
        ids = args.id or [
            r["id"] for r in ctx.db.list_suggestions(args.domain)
            if r["confidence"] >= args.min_confidence and r["decision"] in ("suggest", "auto")
            and (not args.kind or r["media_kind"] == args.kind)
            and (not args.op or r["op"] == args.op)]
        _print([services.apply_suggestion(ctx, i, apply=args.apply) for i in ids])
    elif cmd == "cleanup-skipped":
        from . import fileops

        res = fileops.delete_skipped_duplicates(
            _records(args.log, ctx), apply=args.apply,
            path_maps=fileops.parse_path_maps(args.path_map),
            audit=lambda r: ctx.db.record_file_op(r.op, r.src, r.dst, r.result, r.error))
        _print([r.__dict__ for r in res])
    elif cmd == "sync-subtitles":
        from . import fileops

        recs = [r for lp in (args.log or [None]) for r in _records(lp, ctx)]
        _print([r.__dict__ for r in fileops.sync_orphan_subtitles(recs, apply=args.apply)])
    elif cmd == "srt-report":
        from . import fileops

        recs = [r for lp in (args.log or [None]) for r in _records(lp, ctx)]
        _print(fileops.subtitle_report(s.media_root, recs))
    elif cmd == "search":
        from .titles import TitleNotResolved

        if args.windows:
            _print(services.search_windows(ctx, args.query, args.limit))
        else:
            try:
                hits, res = services.search_titled(ctx, args.query, args.limit, args.mode,
                                                   args.id, args.title)
            except TitleNotResolved as exc:
                _print({"error": str(exc), "suggestions": exc.suggestions})
                return 1
            _print({"resolved": res, "hits": hits} if res else hits)
    elif cmd == "clip":
        _print(services.make_clip(ctx, args.phrase, args.id, args.title, None,
                                  args.match_index, args.pad))
    elif cmd == "openlibrary-import":
        from .openlibrary import import_dump

        ctx.db.migrate()
        import_dump(args.dump, s.database_url, args.batch_lines, args.truncate)
    elif cmd == "openlibrary-lookup":
        from .openlibrary import lookup

        _print(lookup(ctx.db, args.title, args.author, args.isbn))
    return 0


def _safe_run(ctx, action: str, row: dict) -> str:
    from .actions import RetryableError, run_action

    try:
        return run_action(ctx, action, row)
    except RetryableError as exc:
        return f"retry_later: {exc}"


if __name__ == "__main__":
    raise SystemExit(main())
