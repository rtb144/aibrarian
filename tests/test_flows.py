import pytest

flows = pytest.importorskip("aibrarian.flows")


def test_scheduled_deployments_cannot_overlap():
    from prefect.client.schemas.objects import ConcurrencyLimitStrategy

    deps = {d.name: d for d in flows.deployments("all")}
    assert set(deps) == {"media-pipeline", "stage", "discover", "library-scan", "book-scan",
                         "retry-failed", "openlibrary-import", "reprobe"}
    for name in ("media-pipeline", "discover", "library-scan", "book-scan"):
        cl = deps[name].concurrency_limit
        limit = getattr(cl, "limit", cl)
        strategy = (getattr(cl, "collision_strategy", None)
                    or deps[name].concurrency_options.collision_strategy)
        assert limit == 1 and strategy == ConcurrencyLimitStrategy.CANCEL_NEW, name
    assert deps["discover"].schedules  # discover has its own schedule


def test_pipeline_does_not_discover_by_default():
    import inspect

    sig = inspect.signature(flows.pipeline_flow.fn)
    assert sig.parameters["run_discover"].default is False
