"""Prometheus metrics.

Action counters/histograms are incremented in-process by whatever runs the
action (Prefect worker exposes them on ``AIB_METRICS_PORT``); stage gauges are
computed from Postgres at scrape time by the API so they're always accurate
regardless of which process did the work.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

ACTION_TOTAL = Counter(
    "aib_action_total", "Pipeline action executions", ["action", "outcome"])
ACTION_SECONDS = Histogram(
    "aib_action_duration_seconds", "Pipeline action duration", ["action"],
    buckets=(0.1, 0.5, 1, 5, 15, 60, 300, 900, 1800, 3600, 7200))
MEDIA_STAGE = Gauge("aib_media_items", "Media items per pipeline stage", ["stage"])
BACKLOG = Gauge("aib_backlog_items", "Items not yet cataloged (excl. failed/skipped/review)")
SUGGESTIONS_OPEN = Gauge("aib_suggestions_open", "Open librarian suggestions", ["domain"])


def refresh_stage_gauges(counts: dict[str, int]) -> None:
    from .status import Stage, backlog_stages

    for st in Stage:
        MEDIA_STAGE.labels(stage=st.value).set(counts.get(st.value, 0))
    BACKLOG.set(sum(counts.get(s.value, 0) for s in backlog_stages()))
