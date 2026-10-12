"""Prefect 3 orchestration.

Every pipeline action is its own Prefect task (tagged with its resource so
tag-based concurrency limits keep exactly one whisper job on GPU0 and bound
Ollama load on GPU1). Flows only compose tasks; all state lives in Postgres,
so any flow can be killed and re-run safely.

Run ``python -m aibrarian.flows serve`` in the worker container: it registers
the deployments with their schedules and executes runs in-process.
"""

from __future__ import annotations

import logging
import os
import socket
from functools import lru_cache
from pathlib import Path

from prefect import flow, serve, task
from prefect.client.schemas.objects import ConcurrencyLimitConfig, ConcurrencyLimitStrategy
from prefect.runtime import flow_run, task_run

from aibrarian.quarantine import PURGE_CRON

from . import services
from .actions import Context, discover, run_action
from .config import get_settings
from .logging_setup import setup_logging
from .status import ACTIONS, MAX_ATTEMPTS

log = logging.getLogger("aibrarian.flows")
WORKER = f"{socket.gethostname()}:{os.getpid()}"

# default concurrency per resource tag (created by `aibrarian prefect-limits`)
CONCURRENCY = {"gpu-whisper": 1, "gpu-ollama": 2, "cpu": 4, "io": 8}


def _run_ids() -> tuple[str | None, str | None]:
    try:
        return (str(flow_run.id) if flow_run.id else None,
                str(task_run.id) if task_run.id else None)
    except Exception:
        return None, None


@lru_cache
def ctx() -> Context:
    c = Context.from_settings(get_settings())
    c.run_ids = _run_ids
    return c


def _make_action_task(name: str):
    spec = ACTIONS[name]

    @task(name=f"action:{name}", tags=[spec.resource, f"action:{name}"],
          retries=MAX_ATTEMPTS - 1, retry_delay_seconds=[30, 120, 600][:MAX_ATTEMPTS - 1])
    def _t(media_id: int) -> str:
        # RetryableError propagates -> Prefect retries; the final attempt moves the
        # item to 'failed' and returns normally (see run_action).
        return run_action(ctx(), name, media_id)
    return _t


ACTION_TASKS = {n: _make_action_task(n) for n in ACTIONS}


@task(name="discover", tags=["io"], retries=1)
def discover_task(roots: list[str] | None = None) -> dict:
    return discover(ctx(), roots)


@flow(name="stage", log_prints=True)
def stage_flow(action: str, limit: int = 20, roots: list[str] | None = None) -> dict:
    """Run one action over up to ``limit`` items waiting in its input stage."""
    spec = ACTIONS[action]
    rows = ctx().db.claim(spec.requires.value, roots, limit, WORKER)
    futures = [ACTION_TASKS[action].submit(r["id"]) for r in rows]
    results = [f.result(raise_on_failure=False) for f in futures]
    summary: dict[str, int] = {}
    for r in results:
        key = r if isinstance(r, str) else "error"
        summary[key] = summary.get(key, 0) + 1
    return {"action": action, "claimed": len(rows), **summary}


@flow(name="discover", log_prints=True)
def discover_flow(roots: list[str] | None = None) -> dict:
    """Filesystem walk on its own schedule, so processing never waits for it."""
    return discover_task(roots)


@flow(name="media-pipeline", log_prints=True)
def pipeline_flow(batch_size: int | None = None, run_discover: bool = False) -> dict:
    """probe -> extract_audio -> transcribe -> embed -> catalog (+ optional discover).

    Each stage processes what is waiting, so a batch advances as far as it can
    and leftovers are picked up next run. Whisper (GPU0) is the bottleneck, so
    its batch is ``transcribe_batch_size``; cheaper stages take more. Discovery
    runs as the separate ``discover`` deployment by default; even when
    ``run_discover`` is set, a discover failure does not block the stages.
    """
    s = get_settings()
    n = batch_size or s.transcribe_batch_size
    out: dict = {}
    if run_discover:
        fut = discover_task.submit()
        out["discover"] = fut.result(raise_on_failure=False)
    limits = {"probe": n * 10, "extract_audio": n * 2, "transcribe": n,
              "embed": n * 4, "catalog": n * 4}
    for action in ACTIONS:
        out[action] = stage_flow(action, limits[action])
    return out


@flow(name="library-scan")
def library_scan_flow(max_items: int | None = None, resume: bool = True) -> dict:
    return services.library_scan(ctx(), max_items, resume, _run_ids()[0])


@flow(name="book-scan")
def book_scan_flow(paths: list[str] | None = None, resume: bool = True,
                   max_items: int | None = None) -> dict:
    return services.book_scan(ctx(), [Path(p) for p in paths] if paths else None, resume,
                              max_items, _run_ids()[0])


@flow(name="reprobe")
def reprobe_flow(only_missing: bool = True, limit: int | None = None) -> dict:
    """Refresh ffprobe metadata for items already past 'discovered' without
    resetting their stage (e.g. after the media_info upgrade)."""
    from .actions import reprobe_all

    return reprobe_all(ctx(), only_missing, limit)


@flow(name="duplicates")
def duplicates_flow(exact_only: bool = False) -> dict:
    """Read-only duplicates report (writes report_dir/duplicates.json)."""
    from .services import duplicates_report

    return duplicates_report(ctx(), exact_only)["summary"]


@flow(name="dedup-quarantine", log_prints=True)
def dedup_quarantine_flow(limit: int | None = None) -> dict:
    """Move extra copies of IDENTICAL groups to quarantine (never review groups, never
    deletes). Each copy's sha256 is re-verified right before the move. Off when
    DEDUP_AUTO_ENABLED=false; batch size DEDUP_BATCH_LIMIT (0 = unlimited)."""
    from aibrarian.services import quarantine_execute

    s = get_settings()
    if not s.dedup_auto_enabled:
        log.info("dedup-quarantine disabled (DEDUP_AUTO_ENABLED=false)")
        return {"enabled": False}
    n = limit if limit is not None else (s.dedup_batch_limit or None)
    res = quarantine_execute(ctx(), None, n, progress=log.info,
                             source=f"prefect:{_run_ids()[0]}")
    return {k: v for k, v in res.items() if k != "results"}


@flow(name="quarantine-purge", log_prints=True)
def quarantine_purge_flow(older_than_days: int | None = None, execute: bool = True) -> dict:
    """Permanently delete quarantined copies older than QUARANTINE_RETENTION_DAYS."""
    from aibrarian.services import quarantine_purge

    res = quarantine_purge(ctx(), older_than_days, execute, progress=log.info,
                           source=f"prefect:{_run_ids()[0]}")
    return {k: v for k, v in res.items() if k != "items"}


@flow(name="book-autoapply", log_prints=True)
def book_autoapply_flow(limit: int | None = None) -> dict:
    """Apply open book suggestions >= BOOK_AUTO_MIN_CONFIDENCE (rename/move with sidecars,
    identical copies quarantined, the rest needs_review). Off: BOOK_AUTO_APPLY_ENABLED=false."""
    from aibrarian.services import book_autoapply

    s = get_settings()
    if not s.book_auto_apply_enabled:
        log.info("book-autoapply disabled (BOOK_AUTO_APPLY_ENABLED=false)")
        return {"enabled": False}
    n = limit if limit is not None else (s.book_auto_batch_limit or None)
    res = book_autoapply(ctx(), True, n, progress=log.info, source=f"prefect:{_run_ids()[0]}")
    return {k: v for k, v in res.items() if k != "items"}


@flow(name="calibre-validate", log_prints=True)
def calibre_validate_flow() -> dict:
    """Read-only Calibre consistency check (never writes metadata.db)."""
    from aibrarian.services import books_validate

    res = books_validate(ctx(), progress=log.info, source=f"prefect:{_run_ids()[0]}")
    return {"ok": res["ok"], "books": res.get("books"), "issue_count": res.get("issue_count"),
            "integrity": res.get("integrity")}


@flow(name="retry-failed")
def retry_failed_flow() -> int:
    return ctx().db.retry_failed()


@flow(name="openlibrary-import")
def openlibrary_import_flow(dump_path: str, truncate: bool = False) -> dict:
    from .openlibrary import import_dump

    ins, skip = import_dump(Path(dump_path), get_settings().database_url, truncate=truncate,
                            log=log.info)
    return {"inserted": ins, "skipped": skip}


@flow(name="openlibrary-index")
def openlibrary_index_flow(rebuild: bool = False) -> dict:
    """Build/refresh the local Open Library index (no-op if the Delta versions are unchanged)."""
    from prefect import get_run_logger

    from aibrarian import olindex

    s = get_settings()
    rl = get_run_logger()
    st = olindex.build_index(Path(s.ol_delta_dir), Path(s.ol_index_path), rebuild=rebuild,
                             log=rl.info)
    return {k: v for k, v in st.items() if k.startswith("rows_") or k in ("built_at", "size_gb")}


# One run at a time per deployment; a scheduled run that would overlap a
# still-running one is cancelled instead of piling up.
OL_INDEX_CRON = "15 1 * * 6"  # weekly, Saturday 01:15 UTC; skips when the Delta is unchanged
SINGLE_RUN = ConcurrencyLimitConfig(limit=1, collision_strategy=ConcurrencyLimitStrategy.CANCEL_NEW)


def deployments(group: str) -> list:
    """``gpu``: the media pipeline (runs where whisper/GPU0 is available).
    ``cpu``: discover, librarian scans and maintenance. ``all``: both."""
    s = get_settings()
    one = {"concurrency_limit": SINGLE_RUN}
    gpu = [pipeline_flow.to_deployment("media-pipeline", interval=s.transcribe_interval_s, **one),
           stage_flow.to_deployment("stage")]
    cpu = [discover_flow.to_deployment("discover", interval=s.discover_interval_s, **one),
           library_scan_flow.to_deployment("library-scan", cron="0 3 * * *", **one),
           book_scan_flow.to_deployment("book-scan", cron="30 * * * *", **one),
           retry_failed_flow.to_deployment("retry-failed", **one),
           reprobe_flow.to_deployment("reprobe", **one),
           duplicates_flow.to_deployment("duplicates", cron="0 4 * * 0", **one),
           dedup_quarantine_flow.to_deployment("dedup-quarantine", cron="0 5 * * 0", **one),
           book_autoapply_flow.to_deployment("book-autoapply", cron="50 * * * *", **one),
           calibre_validate_flow.to_deployment("calibre-validate", cron="41 5 * * *", **one),
           quarantine_purge_flow.to_deployment("quarantine-purge", cron=PURGE_CRON, **one),
           openlibrary_import_flow.to_deployment("openlibrary-import", **one),
           openlibrary_index_flow.to_deployment("openlibrary-index", cron=OL_INDEX_CRON, **one)]
    return {"gpu": gpu, "cpu": cpu, "all": gpu + cpu}[group]


def main(argv: list[str] | None = None) -> None:
    import sys

    from prometheus_client import start_http_server

    argv = sys.argv[1:] if argv is None else argv
    setup_logging("aibrarian-worker")
    if argv and argv[0] == "serve":
        group = argv[1] if len(argv) > 1 else "all"
        ctx().db.migrate()
        try:
            services.dedup_backfill(ctx())
        except Exception as exc:
            log.warning("dedup backfill failed", extra={"error": str(exc)})
        start_http_server(int(os.getenv("AIB_METRICS_PORT", "9108")))
        serve(*deployments(group))
    else:
        print(pipeline_flow())


if __name__ == "__main__":
    main()
