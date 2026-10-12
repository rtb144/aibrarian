import pytest

flows = pytest.importorskip("aibrarian.flows")


def test_scheduled_deployments_cannot_overlap():
    from prefect.client.schemas.objects import ConcurrencyLimitStrategy

    deps = {d.name: d for d in flows.deployments("all")}
    assert set(deps) == {"media-pipeline", "stage", "discover", "library-scan", "book-scan",
                         "retry-failed", "openlibrary-import", "reprobe", "duplicates",
                         "dedup-quarantine", "quarantine-purge"}
    for name in ("media-pipeline", "discover", "library-scan", "book-scan",
                 "dedup-quarantine", "quarantine-purge"):
        cl = deps[name].concurrency_limit
        limit = getattr(cl, "limit", cl)
        strategy = (getattr(cl, "collision_strategy", None)
                    or deps[name].concurrency_options.collision_strategy)
        assert limit == 1 and strategy == ConcurrencyLimitStrategy.CANCEL_NEW, name
    assert deps["discover"].schedules  # discover has its own schedule
    crons = {n: deps[n].schedules[0].schedule.cron for n in
             ("duplicates", "dedup-quarantine", "quarantine-purge")}
    assert crons == {"duplicates": "0 4 * * 0", "dedup-quarantine": "0 5 * * 0",
                     "quarantine-purge": "37 4 * * *"}


def test_dedup_quarantine_can_be_disabled(monkeypatch):
    from aibrarian.config import get_settings

    monkeypatch.setenv("AIB_DEDUP_AUTO_ENABLED", "false")
    get_settings.cache_clear()
    try:
        assert flows.dedup_quarantine_flow.fn() == {"enabled": False}
    finally:
        get_settings.cache_clear()


def test_pipeline_does_not_discover_by_default():
    import inspect

    sig = inspect.signature(flows.pipeline_flow.fn)
    assert sig.parameters["run_discover"].default is False
