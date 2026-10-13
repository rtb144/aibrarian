"""``aibrarian`` command line. Every mutating command is dry-run unless --apply."""

from __future__ import annotations

import argparse
import contextlib
import csv
import json
import os
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


def _say(msg: str) -> None:
    """Progress line on stderr, flushed (stdout stays clean for the plan/JSON)."""
    print(msg, file=sys.stderr, flush=True)


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
    dps = dp.add_subparsers(dest="dup_cmd")
    dq = dps.add_parser("quarantine", help="move extra copies of IDENTICAL groups to the "
                        "quarantine dir (dry run unless --execute; never deletes)")
    dq.add_argument("--group", type=int, action="append",
                    help="identical group number from the report (repeatable)")
    dq.add_argument("--limit", type=int, help="max extra copies to handle")
    dq.add_argument("--execute", action="store_true", help="actually move the files")
    dq.add_argument("--json", action="store_true", help="full JSON instead of a table")
    dg = dps.add_parser("purge", help="permanently delete quarantined copies older than "
                        "QUARANTINE_RETENTION_DAYS (dry run unless --execute)")
    dg.add_argument("--older-than-days", type=int, help="override QUARANTINE_RETENTION_DAYS")
    dg.add_argument("--execute", action="store_true", help="really delete")
    dg.add_argument("--json", action="store_true")
    de = dps.add_parser("cleanup-empty", help="remove original folders of quarantined copies "
                        "that hold no media (junk moved to quarantine; dry run unless --execute)")
    de.add_argument("--execute", action="store_true")
    dps.add_parser("quarantine-status", help="what is in quarantine, age, purge dates")
    dr = dps.add_parser("restore", help="move quarantined files back using a manifest")
    dr.add_argument("--manifest", type=Path, required=True,
                    help="<quarantine>/<date>/manifest.jsonl")
    dr.add_argument("--path", action="append", help="only these (original or quarantine) paths")
    dr.add_argument("--execute", action="store_true")

    bk = sub.add_parser("books", help="automatic book librarian (see docs/operations.md)")
    bks = bk.add_subparsers(dest="books_cmd", required=True)
    ba = bks.add_parser("autoapply", help="apply open book suggestions >= "
                        "BOOK_AUTO_MIN_CONFIDENCE (dry run unless --execute)")
    ba.add_argument("--execute", action="store_true")
    ba.add_argument("--limit", type=int, help="max suggestions to consider")
    ba.add_argument("--json", action="store_true")
    bks.add_parser("validate", help="read-only Calibre check (integrity, logged ingests, "
                   "missing/orphan files); result logged as a validate action")
    bo = bks.add_parser("adopt-orphans", help="ingest loose files in the Calibre library "
                        "(orphans from validate) via calibredb, then quarantine the originals "
                        "(dry run unless --execute)")
    bo.add_argument("--execute", action="store_true")
    bo.add_argument("--limit", type=int, help="max folder groups to process")
    bo.add_argument("--json", action="store_true")
    bu = bks.add_parser("undo", help="reverse book actions by id or run (dry run unless --execute)")
    bu.add_argument("--id", type=int, action="append", help="library_action id (repeatable)")
    bu.add_argument("--run", help="run id, e.g. books-20261012T095000Z or prefect:<id>")
    bu.add_argument("--execute", action="store_true")

    br = bks.add_parser("rescan", help="re-catalog error / low-confidence book suggestions in "
                        "place (dry run unless --execute; holds the book-scan lock)")
    br.add_argument("--status", action="append", help="error | needs_review | review | reject "
                    "(review/reject = open with that decision) | open | failed ...; repeatable. "
                    "Default: error, needs_review, review, reject")
    br.add_argument("--id", type=int, action="append", help="only these suggestion ids "
                    "(repeatable; any non-applied status unless --status is given)")
    br.add_argument("--execute", action="store_true")
    br.add_argument("--limit", type=int)
    br.add_argument("--json", action="store_true")

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

    sp = sub.add_parser("srt-pair", help="pair orphan .srt files with the single video in "
                        "their movie dir (Jellyfin names; dry run unless --execute; others "
                        "to QUARANTINE_DIR/<date>, undo with duplicates restore)")
    sp.add_argument("--root", type=Path, help="default MEDIA_ROOT/01-movies")
    sp.add_argument("--execute", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--langs", help="kept languages, comma list (default env AIB_SUB_LANGS "
                    "or eng)")
    sp.add_argument("--limit", type=int, help="only the first N dirs")
    sp.add_argument("--include-no-video", action="store_true",
                    help="also quarantine srts of dirs that have no video")

    dl = sub.add_parser("downloads", help="downloads folder tools (read-only)")
    dls = dl.add_subparsers(dest="dl_cmd", required=True)
    dt = dls.add_parser("triage", help="READ-ONLY report: classify each downloads entry "
                        "(in library / moved / pending / junk ...) with a suggested action")
    dt.add_argument("--root", type=Path, help="default MEDIA_ROOT/downloads")
    dt.add_argument("--json", action="store_true", help="print the full report as JSON")
    dt.add_argument("--out", type=Path, help="default REPORT_DIR/downloads-triage.json "
                    "(CSV written next to it)")
    dt.add_argument("--depth", type=int, default=1,
                    help="levels to descend into numbered category dirs like 2-books")
    dt.add_argument("--max-hash", type=int, default=500,
                    help="cap on head/tail sample hashes (only on exact size matches)")
    dt.add_argument("--no-db", action="store_true", help="skip the catalog (junk/type only)")

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

    olx = sub.add_parser("openlibrary", help="local Open Library index from the Delta Lake")
    olxs = olx.add_subparsers(dest="ol_cmd", required=True)
    obi = olxs.add_parser("build-index", help="stream OL_DELTA_DIR into OL_INDEX_PATH (hours)")
    obi.add_argument("--rebuild", action="store_true", help="ignore an up-to-date/partial index")
    obi.add_argument("--batch-size", type=int, default=50_000)
    olxs.add_parser("stats", help="index row counts, last build, build progress")
    olxs.add_parser("add-title-index", help="add the title-only (edition-insensitive) lookup "
                    "table to an existing index (no rebuild; minutes)")
    olk = olxs.add_parser("lookup", help="query the index")
    olk.add_argument("--title")
    olk.add_argument("--author")
    olk.add_argument("--isbn", action="append", default=[])

    ee = sub.add_parser("embed-eval", help="A/B test an embedding model (separate collections)")
    ees = ee.add_subparsers(dest="ee_cmd", required=True)
    eb = ees.add_parser("build", help="embed N movies with baseline + candidate (resumable)")
    eb.add_argument("--model", required=True, help="candidate Ollama model, e.g. embeddinggemma-2")
    eb.add_argument("--movies", type=int, default=20)
    eb.add_argument("--batch", type=int, default=16)
    eb.add_argument("--pause", type=float, default=0.2, help="seconds between batches")
    eb.add_argument("--keep-alive", default="2m")
    eb.add_argument("--reselect", action="store_true", help="pick the movies again")
    eb.add_argument("--no-baseline", action="store_true")
    er = ees.add_parser("run", help="generate queries, score all built models, write the report")
    er.add_argument("--model", action="append", help="limit to these models")
    er.add_argument("--queries", type=Path, help="extra user queries (JSON list / JSONL)")
    er.add_argument("--per-type", type=int, default=20)
    er.add_argument("--regen", action="store_true", help="regenerate the query set")
    er.add_argument("--tolerance", type=float, default=30.0)
    er.add_argument("--json", action="store_true")
    eq = ees.add_parser("query", help="top 5 for one query from every built model")
    eq.add_argument("text")
    ees.add_parser("cleanup", help="drop all eval_* Qdrant collections")

    gd = sub.add_parser("gen-docs", help="(was gen_docs.py)")
    gd.add_argument("paths", nargs="*", type=Path)
    gd.add_argument("--out", type=Path, default=Path("docs/reference"))
    gd.add_argument("--compose", type=Path, action="append", default=[])
    gd.add_argument("--force", action="store_true")

    sub.add_parser("prefect-limits", help="create tag concurrency limits in Prefect")
    tr = sub.add_parser("transcripts", help="transcript checks (langcheck is read-only)")
    trs = tr.add_subparsers(dest="tr_cmd", required=True)
    tlc = trs.add_parser("langcheck", help="detect non-English / garbage transcripts; writes "
                         "langcheck.json + .csv to REPORT_DIR (never touches media/transcripts)")
    tlc.add_argument("--limit", type=int, help="max transcripts to check")
    tlc.add_argument("--out", type=Path, help="report dir, or a .json/.csv file path")
    tlc.add_argument("--no-store", action="store_true", help="do not save into aib.transcript_lang")
    tlc.add_argument("--no-db", action="store_true", help="sidecars only (skip DB-only transcripts)")
    tlc.add_argument("--json", action="store_true", help="print the summary as JSON")
    trq = trs.add_parser("requeue-lang", help="re-transcribe non-English transcripts whose file "
                         "has an English audio track (dry run unless --execute; old transcripts "
                         "are moved to QUARANTINE_DIR/<date>, never deleted)")
    trq.add_argument("--execute", action="store_true", help="move old transcripts + reset stage")
    trq.add_argument("--include-untagged", action="store_true",
                     help="also re-queue files with several audio tracks but no English tag")
    trq.add_argument("--json", action="store_true", help="print plan/results as JSON")
    cy = sub.add_parser("consistency", help="catalog <-> filesystem consistency (check is "
                        "read-only; fix only does safe relinks/repoints, dry run by default)")
    cys = cy.add_subparsers(dest="cy_cmd", required=True)
    cyc = cys.add_parser("check", help="classify unlinked transcripts, stale rows, missing "
                         "paths, videos without rows, inode duplicates, stuck changes; writes "
                         "REPORT_DIR/consistency.json")
    cyc.add_argument("--json", action="store_true", help="print the full report as JSON")
    cyc.add_argument("--fix-plan", action="store_true", help="also print the (dry-run) fix plan")
    cyc.add_argument("--pending-minutes", type=int, default=30,
                     help="flag change_log rows pending longer than this")
    cyc.add_argument("--sample", type=int, default=5, help="example items per class")
    cyc.add_argument("--no-write", action="store_true", help="do not write consistency.json")
    cyf = cys.add_parser("fix", help="apply SAFE fixes only (relink transcript to the video in "
                         "the same dir; repoint a stale row to its file); dry run unless "
                         "--execute; every change goes to aib.change_log; never deletes")
    cyf.add_argument("--execute", action="store_true")
    cyf.add_argument("--only", action="append", choices=["stem_mismatch", "stale_row_path"],
                     help="restrict to one class (repeatable)")
    cyf.add_argument("--limit", type=int)
    cyf.add_argument("--json", action="store_true")
    ch = sub.add_parser("changes", help="aib.change_log (moves/renames/quarantines/repoints)")
    ch.add_argument("--day", help="YYYY-MM-DD")
    ch.add_argument("--pipeline", help="actor substring, e.g. prefect:book-autoapply or cli")
    ch.add_argument("--op")
    ch.add_argument("--path", help="substring of old/new path")
    ch.add_argument("--status", choices=["pending", "committed", "failed", "rolled_back"])
    ch.add_argument("--limit", type=int, default=50)
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

    if cmd == "embed-eval":
        from . import embedeval
        from .db import Database
        from .ollama import OllamaClient

        h = embedeval.Harness(s, Database(s.database_url), OllamaClient.from_settings(s),
                              say=lambda m: print(m, file=sys.stderr, flush=True))
        if args.ee_cmd == "build":
            with contextlib.suppress(OSError):
                os.nice(10)  # low CPU priority next to the pipeline
            h.build(args.model, args.movies, args.batch, args.pause, args.keep_alive,
                    args.reselect, not args.no_baseline)
        elif args.ee_cmd == "run":
            res = h.run(args.model, args.queries, args.per_type, args.regen, args.tolerance)
            print(json.dumps(res, indent=1, default=str) if args.json
                  else embedeval.format_table(res))
            print(f"report: {h.out / 'latest.md'}", file=sys.stderr)
        elif args.ee_cmd == "query":
            _print({m: {ix: [(x["payload"].get("title"), round(x["payload"].get("start", 0)),
                              round(x["score"], 3)) for x in r[ix][:5]]
                        for ix in ("segments", "windows")}
                    for m in h.state()["models"] for r in [h.search(m, args.text, 5)]})
        else:
            _print({"dropped": h.cleanup()})
        return 0

    if cmd == "openlibrary":
        from . import olindex

        if args.ol_cmd == "build-index":
            olindex.build_index(s.ol_delta_dir, s.ol_index_path, args.batch_size, args.rebuild,
                                log=lambda m: print(m, file=sys.stderr, flush=True))
        elif args.ol_cmd == "add-title-index":
            _print(olindex.add_title_index(s.ol_index_path,
                                           log=lambda m: print(m, file=sys.stderr, flush=True)))
        elif args.ol_cmd == "stats":
            _print(olindex.stats(s.ol_index_path) | {"delta_dir": str(s.ol_delta_dir)})
        else:
            idx = olindex.open_index(s)
            if idx is None:
                print("no index at " + str(s.ol_index_path), file=sys.stderr)
                return 1
            _print(idx.lookup(args.title, args.author, args.isbn))
        return 0

    if cmd == "downloads":
        from . import downloads
        from .booklib import make_calibre
        from .db import Database

        db = None if args.no_db else Database.from_settings(s)
        cat = downloads.load_catalog(db, make_calibre(s))
        root = args.root or Path(s.media_root) / "downloads"
        rep = downloads.triage(root, cat, calibre_dir=Path(s.calibre_dir),
                               music_dir=Path(s.media_root) / "03-music", depth=args.depth,
                               max_hash=args.max_hash)
        out = args.out or Path(s.report_dir) / "downloads-triage.json"
        jp, cp = downloads.write_reports(rep, out)
        if args.json:
            _print(rep)
        else:
            print(downloads.format_summary(rep) + f"\nreports: {jp} {cp}")
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
    elif cmd == "duplicates" and args.dup_cmd == "quarantine":
        from .quarantine import format_plan

        if args.execute:
            res = services.quarantine_execute(ctx, args.group, args.limit, progress=_say)
        else:
            res = services.quarantine_plan(ctx, args.group, args.limit, progress=_say)
        print(json.dumps(res, indent=2, default=str) if args.json else format_plan(res))
    elif cmd == "duplicates" and args.dup_cmd == "purge":
        res = services.quarantine_purge(ctx, args.older_than_days, args.execute, progress=_say)
        if args.json:
            _print(res)
        else:
            verb = "deleted" if args.execute else "would delete (dry run, add --execute)"
            n = res["deleted"] if args.execute else res["would_delete"]
            print(f"{verb}: {n} files older than {res['older_than_days']} days in {res['root']}; "
                  f"kept {sum(i['result'] == 'kept' for i in res['items'])}; "
                  f"freed {res['freed_gb']} GB; removed dirs {res['removed_batch_dirs']}")
    elif cmd == "books" and args.books_cmd == "autoapply":
        from .booklib import format_result

        res = services.book_autoapply(ctx, args.execute, args.limit, progress=_say)
        print(json.dumps(res, indent=2, default=str) if args.json else format_result(res), end="")
    elif cmd == "books" and args.books_cmd == "rescan":
        if "applied" in (args.status or []):
            print("applied suggestions are never rescanned", file=sys.stderr)
            return 2
        res = services.book_rescan(ctx, args.status, args.execute, args.limit, progress=_say,
                                   ids=args.id)
        if args.json:
            print(json.dumps(res, indent=2, default=str))
        elif res.get("locked"):
            print(res["message"], file=sys.stderr)
        else:
            print(f"{'rescan' if args.execute else 'would rescan (dry run, add --execute)'}: "
                  f"{res['count']} book suggestions {res['by_status']} statuses={res['statuses']}")
            for it in res["sample"]:
                print(f"  #{it['id']} {it['status']}/{it['decision']} {it['confidence']} "
                      f"{it['source_path']}")
            if res.get("results"):
                print(json.dumps(res["results"], default=str))
        if res.get("locked"):
            return 3
        return 1 if res.get("results", {}).get("errors") else 0
    elif cmd == "books" and args.books_cmd == "validate":
        res = services.books_validate(ctx, progress=_say)
        _print(res)
        return 0 if res["ok"] else 1
    elif cmd == "books" and args.books_cmd == "adopt-orphans":
        res = services.books_adopt_orphans(ctx, args.execute, args.limit, progress=_say)
        if args.json:
            print(json.dumps(res, indent=2, default=str))
        if res["run_id"]:
            print(f"undo: aibrarian books undo --run {res['run_id']}  (dry run; add --execute)")
        return 1 if res["counts"].get("error") else 0
    elif cmd == "books" and args.books_cmd == "undo":
        if not args.id and not args.run:
            print("give --id N (repeatable) or --run RUN", file=sys.stderr)
            return 2
        _print(services.books_undo(ctx, args.id, args.run, args.execute, progress=_say))
    elif cmd == "duplicates" and args.dup_cmd == "cleanup-empty":
        _print(services.cleanup_empty_folders(ctx, args.execute, progress=_say))
    elif cmd == "duplicates" and args.dup_cmd == "quarantine-status":
        _print(services.quarantine_status(ctx))
    elif cmd == "duplicates" and args.dup_cmd == "restore":
        from .quarantine import restore

        _print(restore(args.manifest, args.execute, ctx.db, args.path))
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
    elif cmd == "srt-pair":
        from . import srtpair

        root = args.root or Path(s.media_root) / "01-movies"
        plan = srtpair.plan(root, srtpair.kept_langs(args.langs), args.limit,
                            args.include_no_video)
        res = srtpair.execute(plan, s, ctx.db, say=_say) if args.execute else None
        if args.json:
            _print({"plan": plan, "result": res})
        else:
            print(srtpair.format_plan(plan, res), end="")
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
    elif cmd == "transcripts" and args.tr_cmd == "langcheck":
        from . import langcheck

        res = langcheck.run(ctx, args.limit, args.out, store=not args.no_store,
                            include_db=not args.no_db, progress=_say)
        if args.json:
            _print(res["summary"])
        else:
            print(langcheck.format_summary(res), end="")
    elif cmd == "transcripts" and args.tr_cmd == "requeue-lang":
        from . import requeue_lang

        plan = requeue_lang.plan(ctx, include_untagged=args.include_untagged)
        res = requeue_lang.execute(ctx, plan) if args.execute else None
        if args.json:
            _print({"plan": plan, "result": res})
        else:
            print(requeue_lang.format_plan(plan, res), end="")
    elif cmd == "consistency":
        from . import consistency

        if args.cy_cmd == "check":
            rep = consistency.run(ctx, args.pending_minutes, write=not args.no_write)
            if args.json:
                _print(rep)
            else:
                print(consistency.format_summary(rep, plan=args.fix_plan, sample=args.sample),
                      end="")
        else:
            rep = consistency.run(ctx, write=False)
            res = consistency.execute_fixes(ctx, rep, args.execute,
                                            args.only or consistency.SAFE_FIXES, args.limit,
                                            say=None if args.json else print)
            if args.json:
                _print(res)
            else:
                print(f"{'done' if args.execute else 'dry run'}: {res['done']} changed, "
                      f"{res['planned']} planned" + ("" if args.execute else
                                                     " (use --execute)"))
    elif cmd == "changes":
        rows, total = ctx.db.changes(args.day, args.pipeline, args.op, args.path, args.status,
                                     args.limit)
        for r in reversed(rows):
            print(f"{r['id']:>7} {r['ts']:%Y-%m-%d %H:%M:%S} {r['status']:<11} "
                  f"{(r['actor'] or '-'):<26} {r['op']:<11} {r['entity']:<10} "
                  f"{r['old_path'] or '-'} -> {r['new_path'] or '-'}"
                  + (f"  ERROR {r['error']}" if r.get("error") else ""))
        print(f"({len(rows)} of {total})")
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
