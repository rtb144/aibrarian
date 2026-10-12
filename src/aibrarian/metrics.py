"""Prometheus metrics.

Action counters/histograms are incremented in-process by whatever runs the
action (Prefect worker exposes them on ``AIB_METRICS_PORT``); stage gauges are
computed from Postgres at scrape time by the API so they're always accurate
regardless of which process did the work.
"""

from __future__ import annotations

from prometheus_client import REGISTRY, Counter, Gauge, Histogram


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
MEDIA_STAGE = _get_or_create(Gauge, "aib_media_items", "Media items per pipeline stage", ["stage"])
BACKLOG = _get_or_create(Gauge, "aib_backlog_items", "Items not yet cataloged (excl. failed/skipped/review)")
SUGGESTIONS_OPEN = _get_or_create(Gauge, "aib_suggestions_open", "Open librarian suggestions", ["domain"])


def refresh_stage_gauges(counts: dict[str, int]) -> None:
    from .status import Stage, backlog_stages

    for st in Stage:
        MEDIA_STAGE.labels(stage=st.value).set(counts.get(st.value, 0))
    BACKLOG.set(sum(counts.get(s.value, 0) for s in backlog_stages()))
