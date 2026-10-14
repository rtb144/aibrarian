import logging
import sys
import types

import pytest

from aibrarian import profiling
from aibrarian.config import get_settings


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    profiling._reset_for_tests()
    get_settings.cache_clear()
    yield
    profiling._reset_for_tests()
    get_settings.cache_clear()


def test_disabled_is_noop(monkeypatch):
    monkeypatch.setenv("AIB_PYROSCOPE_ENABLED", "false")
    called = []
    fake = types.SimpleNamespace(configure=lambda **k: called.append(k),
                                 tag_wrapper=lambda t: called.append(t))
    monkeypatch.setitem(sys.modules, "pyroscope", fake)
    assert profiling.init_profiling("test") is False
    with profiling.stage("probe", media_id=1):
        pass
    assert called == []


def test_init_survives_bad_url_and_warns_once(monkeypatch, caplog):
    monkeypatch.setenv("AIB_PYROSCOPE_ENABLED", "true")
    monkeypatch.setenv("AIB_PYROSCOPE_URL", "not a url://")

    def boom(**k):
        raise RuntimeError("bad url")
    monkeypatch.setitem(sys.modules, "pyroscope", types.SimpleNamespace(configure=boom))
    with caplog.at_level(logging.WARNING, logger="aibrarian.profiling"):
        assert profiling.init_profiling("test") is False
        assert profiling.init_profiling("test") is False  # idempotent
        with profiling.stage("embed"):
            pass
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_unreachable_server_real_agent(monkeypatch):
    pytest.importorskip("pyroscope")
    monkeypatch.setenv("AIB_PYROSCOPE_ENABLED", "true")
    monkeypatch.setenv("AIB_PYROSCOPE_URL", "http://127.0.0.1:9")
    profiling.init_profiling("test")
    with profiling.stage("transcribe", prefect_run="abc"):
        sum(range(1000))
