"""Prometheus metrics.

Action counters/histograms are incremented in-process by whatever runs the
action (Prefect worker exposes them on ``AIB_METRICS_PORT``); stage gauges are
computed from Postgres at scrape time by the API so they're always accurate
regardless of which process did the work.
"""

from __future__ import annotations

import contextlib
import os
import shutil

# Prefect runs each flow run in a subprocess, so the worker containers set
# PROMETHEUS_MULTIPROC_DIR and the serving process aggregates per-PID files.
# The dir must exist before any metric is touched (also for `docker compose
# exec ... aibrarian ...` CLI calls), so create it before importing.
_MP_DIR = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
if _MP_DIR:
    try:
        os.makedirs(_MP_DIR, exist_ok=True)
    except OSError:  # unwritable: fall back to plain single-process metrics
        os.environ.pop("PROMETHEUS_MULTIPROC_DIR", None)
        _MP_DIR = None

from prometheus_client import REGISTRY, Counter, Gauge, Histogram  # noqa: E402


def _get_or_create(cls, name, *args, **kwargs):
    """Return an already-registered collector instead of crashing when this
    module is imported twice (Prefect loads flows.py as a script)."""
    existing = getattr(REGISTRY, "_names_to_collectors", {})
    for key in (name, f"{name}_total"):
        if key in existing:
            return existing[key]
    return cls(name, *args, **kwargs)

ACTION_TOTAL = _get_or_create(Counter,
    "aib_action_total", "Pipeline action executions", ["action", "outcome"])
ACTION_SECONDS = _get_or_create(Histogram,
    "aib_action_duration_seconds", "Pipeline action duration", ["action"],
    buckets=(0.1, 0.5, 1, 5, 15, 60, 300, 900, 1800, 3600, 7200))
# DB-derived gauges are only meaningful in the API (computed at scrape time).
# Worker processes run in multiprocess mode and must not expose them, or
# dashboards see duplicate/zero series from every worker.
if _MP_DIR:
    MEDIA_STAGE = BACKLOG = SUGGESTIONS_OPEN = None
    CONSISTENCY_ISSUES = CONSISTENCY_LAST_RUN = CHANGES_PENDING = None
else:
    MEDIA_STAGE = _get_or_create(Gauge, "aib_media_items", "Media items per pipeline stage", ["stage"])
    BACKLOG = _get_or_create(Gauge, "aib_backlog_items",
                             "Items not yet cataloged (excl. failed/skipped/review)")
    SUGGESTIONS_OPEN = _get_or_create(Gauge, "aib_suggestions_open", "Open librarian suggestions", ["domain"])
    # from REPORT_DIR/consistency.json (written by the `consistency` flow) at scrape time
    CONSISTENCY_ISSUES = _get_or_create(Gauge, "aib_consistency_issues",
                                        "Consistency check: items per issue class", ["issue"])
    CONSISTENCY_LAST_RUN = _get_or_create(Gauge, "aib_consistency_last_run_timestamp_seconds",
                                          "Unix time of the last consistency report")
    CHANGES_PENDING = _get_or_create(Gauge, "aib_change_log_pending",
                                     "aib.change_log rows still pending (live, from the DB)",
                                     ["age"])


def refresh_consistency_gauges(report: dict | None) -> None:
    """Idempotent: every known class is (re)set, absent ones to 0."""
    if CONSISTENCY_ISSUES is None or not report:
        return
    from datetime import datetime

    from .consistency import ALL_CLASSES

    counts = report.get("counts") or {}
    for k in ALL_CLASSES:
        CONSISTENCY_ISSUES.labels(issue=k).set(counts.get(k, 0))
    for k, v in (report.get("no_media_breakdown") or {}).items():
        CONSISTENCY_ISSUES.labels(issue=f"no_media:{k}").set(v)
    with contextlib.suppress(KeyError, ValueError):
        CONSISTENCY_LAST_RUN.set(datetime.fromisoformat(report["generated_at"]).timestamp())


def refresh_pending_gauge(db, stale_minutes: int = 30) -> None:
    if CHANGES_PENDING is None:
        return
    with db.conn() as c:
        r = c.execute("SELECT count(*) AS n, count(*) FILTER (WHERE ts < now() - "
                      "make_interval(mins => %s)) AS stale FROM aib.change_log "
                      "WHERE status='pending'", (stale_minutes,)).fetchone()
    CHANGES_PENDING.labels(age="all").set(r["n"])
    CHANGES_PENDING.labels(age="stale").set(r["stale"])


def reset_multiprocess_dir() -> None:
    """Wipe stale per-PID files. Call once at worker startup, before any flow
    run subprocess starts."""
    d = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if not d:
        return
    os.makedirs(d, exist_ok=True)
    for name in os.listdir(d):
        path = os.path.join(d, name)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            with contextlib.suppress(FileNotFoundError):
                os.remove(path)


def start_metrics_server(port: int):
    """Serve metrics; aggregates all processes when multiprocess mode is on."""
    from prometheus_client import CollectorRegistry, start_http_server

    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess

        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        return start_http_server(port, registry=registry)
    return start_http_server(port)


def refresh_stage_gauges(counts: dict[str, int]) -> None:
    from .status import Stage, backlog_stages

    if MEDIA_STAGE is None or BACKLOG is None:
        return

    for st in Stage:
        MEDIA_STAGE.labels(stage=st.value).set(counts.get(st.value, 0))
    BACKLOG.set(sum(counts.get(s.value, 0) for s in backlog_stages()))
