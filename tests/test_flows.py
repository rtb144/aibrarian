import pytest

flows = pytest.importorskip("aibrarian.flows")


def test_scheduled_deployments_cannot_overlap():
    from prefect.client.schemas.objects import ConcurrencyLimitStrategy

    deps = {d.name: d for d in flows.deployments("all")}
    assert set(deps) == {"media-pipeline", "stage", "discover", "library-scan", "book-scan",
                         "retry-failed", "openlibrary-import", "reprobe", "duplicates",
                         "dedup-quarantine", "quarantine-purge", "book-autoapply",
                         "calibre-validate", "openlibrary-index",
                         "calibre-adopt-orphans"}
    for name in ("media-pipeline", "discover", "library-scan", "book-scan",
                 "dedup-quarantine", "quarantine-purge", "book-autoapply",
                 "calibre-validate", "openlibrary-index"):
        cl = deps[name].concurrency_limit
        limit = getattr(cl, "limit", cl)
        strategy = (getattr(cl, "collision_strategy", None)
                    or deps[name].concurrency_options.collision_strategy)
        assert limit == 1 and strategy == ConcurrencyLimitStrategy.CANCEL_NEW, name
    assert deps["discover"].schedules  # discover has its own schedule
    crons = {n: deps[n].schedules[0].schedule.cron for n in
             ("duplicates", "dedup-quarantine", "quarantine-purge", "book-autoapply",
              "calibre-validate", "openlibrary-index")}
    assert crons == {"duplicates": "0 4 * * 0", "dedup-quarantine": "0 5 * * 0",
                     "quarantine-purge": "37 4 * * *", "book-autoapply": "50 * * * *",
                     "calibre-validate": "41 5 * * *", "openlibrary-index": "15 1 * * 6"}


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


def test_book_autoapply_can_be_disabled(monkeypatch):
    from aibrarian.config import get_settings

    monkeypatch.setenv("AIB_BOOK_AUTO_APPLY_ENABLED", "false")
    get_settings.cache_clear()
    try:
        assert flows.book_autoapply_flow.fn() == {"enabled": False}
    finally:
        get_settings.cache_clear()


def test_flows_importable_as_script(tmp_path):
    """Prefect loads a deployment's entrypoint by file path (one flow per process);
    every flow must import that way (relative-import bug of 46a385f)."""
    import subprocess
    import sys
    from pathlib import Path

    import aibrarian

    f = Path(aibrarian.__file__).with_name("flows.py")
    for name, flow_name in (("book_autoapply_flow", "book-autoapply"),
                            ("dedup_quarantine_flow", "dedup-quarantine"),
                            ("quarantine_purge_flow", "quarantine-purge"),
                            ("book_scan_flow", "book-scan"),
                            ("calibre_validate_flow", "calibre-validate"),
                            ("calibre_adopt_orphans_flow", "calibre-adopt-orphans"),
                            ("openlibrary_index_flow", "openlibrary-index")):
        code = ("from prefect.flows import load_flow_from_entrypoint as l;"
                f"print(l('{f}:{name}').name)")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           cwd=tmp_path, timeout=120)
        assert r.returncode == 0 and flow_name in r.stdout, r.stderr[-2000:]


def test_adopt_orphans_deployment_is_manual():
    deps = {d.name: d for d in flows.deployments("all")}
    assert not deps["calibre-adopt-orphans"].schedules
