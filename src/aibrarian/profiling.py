"""Optional continuous profiling via Pyroscope (``pyroscope-io``).

``init_profiling`` is called once per process (API, CPU worker, GPU worker); ``stage``
tags samples with the pipeline stage and Prefect flow-run id so Grafana can show a
flame graph per stage. Profiling must never crash, block or spam the app: every
pyroscope call is guarded, failures warn once, and an unreachable server is fine
(the agent ships samples from a background thread and drops them on error).
"""

from __future__ import annotations

import contextlib
import functools
import logging
import socket
from collections.abc import Iterator

log = logging.getLogger("aibrarian.profiling")

_state: dict = {"initialized": False, "active": False, "warned": False}


def _warn_once(msg: str, exc: BaseException) -> None:
    if not _state["warned"]:
        _state["warned"] = True
        log.warning(msg, extra={"error": str(exc)})


def init_profiling(app_name: str | None = None) -> bool:
    """Configure pyroscope once; returns True when profiling is active. Never raises."""
    if _state["initialized"]:
        return _state["active"]
    _state["initialized"] = True
    try:
        from aibrarian.config import get_settings

        s = get_settings()
        if not s.pyroscope_enabled:
            return False
        import pyroscope

        service = app_name or "aibrarian"
        pyroscope.configure(
            application_name=s.pyroscope_app_name,
            server_address=s.pyroscope_url,
            sample_rate=100,
            enable_logging=False,  # the agent would log every failed upload
            tags={"host": socket.gethostname(), "service": service},
        )
        _state["active"] = True
        log.info("pyroscope profiling enabled",
                 extra={"url": s.pyroscope_url, "service": service})
    except Exception as exc:  # missing wheel, bad URL, anything
        _warn_once("pyroscope profiling disabled (init failed)", exc)
        _state["active"] = False
    return _state["active"]


def _flow_run_id() -> str | None:
    try:
        from prefect.runtime import flow_run

        return str(flow_run.id) if flow_run.id else None
    except Exception:
        return None


@contextlib.contextmanager
def stage(name: str, **tags) -> Iterator[None]:
    """Tag profiling samples with ``stage`` (+ Prefect flow-run id). No-op when inactive."""
    if not _state["active"]:
        yield
        return
    t = {"stage": name, **{k: str(v) for k, v in tags.items() if v is not None}}
    run = t.get("prefect_run") or _flow_run_id()
    if run:
        t["prefect_run"] = run
    try:
        import pyroscope

        cm = pyroscope.tag_wrapper(t)
        cm.__enter__()
    except Exception as exc:
        _warn_once("pyroscope tagging failed", exc)
        yield
        return
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            cm.__exit__(None, None, None)


def profiled(name: str):
    """Decorator form of :func:`stage` (keeps the signature, so it is safe under @flow)."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            with stage(name):
                return fn(*a, **kw)
        return wrapper
    return deco


def _reset_for_tests() -> None:
    _state.update(initialized=False, active=False, warned=False)


