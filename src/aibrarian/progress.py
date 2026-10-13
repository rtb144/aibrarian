"""Periodic progress lines for long-running flows.

``ProgressReporter.tick(**counters)`` is cheap to call per item; it logs only when
``AIB_PROGRESS_LOG_SECONDS`` (default 900) have passed since the last line. Inside a
Prefect run it logs via ``get_run_logger()`` (visible in the UI), otherwise via the
standard ``logging`` logger. No threads: lines are emitted from the caller's loop.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import Any

DEFAULT_INTERVAL = 900.0
_fallback = logging.getLogger("aibrarian.progress")


def progress_interval() -> float:
    try:
        return max(0.0, float(os.environ.get("AIB_PROGRESS_LOG_SECONDS", DEFAULT_INTERVAL)))
    except ValueError:
        return DEFAULT_INTERVAL


def run_logger() -> Any:
    """Prefect run logger when inside a flow/task run, else the stdlib logger."""
    try:
        from prefect import get_run_logger

        return get_run_logger()
    except Exception:
        return _fallback


def _dur(sec: float) -> str:
    sec = int(max(sec, 0))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


class ProgressReporter:
    """``done_key`` names the counter used for rate / remaining / ETA."""

    def __init__(self, name: str, total: int | None = None, interval: float | None = None,
                 logger: Any = None, done_key: str = "processed",
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.name, self.total, self.done_key = name, total, done_key
        self.interval = progress_interval() if interval is None else interval
        self.logger = logger
        self.clock = clock
        self.t0 = self.last = clock()
        self.counters: dict[str, Any] = {}
        self.current: str | None = None
        self.lines = 0

    def _log(self, msg: str) -> None:
        if self.logger is None:
            self.logger = run_logger()
        self.logger.info(msg)

    def format(self, prefix: str = "progress") -> str:
        el = self.clock() - self.t0
        done = self.counters.get(self.done_key, 0) or 0
        parts = [f"{self.name} {prefix}: elapsed={_dur(el)}"]
        if self.total is not None:
            parts.append(f"{self.done_key}={done}/{self.total}")
        else:
            parts.append(f"{self.done_key}={done}")
        parts += [f"{k}={v}" for k, v in self.counters.items() if k != self.done_key]
        rate = done / el * 3600 if el > 0 else 0.0
        parts.append(f"rate={rate:,.0f}/h")
        if self.total is not None:
            rem = max(self.total - done, 0)
            parts.append(f"remaining={rem}")
            if rate > 0 and prefix == "progress":
                parts.append(f"eta={_dur(rem / rate * 3600)}")
        if self.current and prefix == "progress":
            parts.append(f"current={self.current}")
        return " ".join(parts)

    def start(self, msg: str = "") -> None:
        total = f" total={self.total}" if self.total is not None else ""
        self._log(f"{self.name} start:{total} (progress every {self.interval:.0f}s)"
                  + (f" {msg}" if msg else ""))

    def tick(self, current: Any = None, force: bool = False, **counters: Any) -> bool:
        """Update counters (absolute values); log if the interval has passed."""
        self.counters.update(counters)
        if current is not None:
            self.current = str(current)
        now = self.clock()
        if not force and now - self.last < self.interval:
            return False
        self.last = now
        self.lines += 1
        self._log(self.format())
        return True

    def finish(self, **counters: Any) -> None:
        self.counters.update(counters)
        self._log(self.format("done"))
