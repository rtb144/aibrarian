"""Hard per-item time limits for long scans.

``run_in_subprocess`` runs a picklable-result function in a forked child and kills it
after ``timeout`` seconds; it is the only reliable way to stop a C-level/pure-Python
parser stuck on a damaged file (pypdf on a broken xref can loop for hours). While
waiting it calls ``on_wait(elapsed)`` every ``poll`` seconds so the caller can emit
progress lines mid-file without threads.

``alarm`` is a SIGALRM-based wall-clock cap for the rest of the per-file work
(HTTP/LLM/lookups); it is a no-op outside the main thread.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import signal
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any


class ItemTimeout(Exception):
    pass


def env_seconds(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except ValueError:
        return default


def _child(conn, fn, args) -> None:
    try:
        conn.send(("ok", fn(*args)))
    except BaseException as exc:  # noqa: BLE001 - report anything to the parent
        conn.send(("err", f"{type(exc).__name__}: {exc}"))
    finally:
        conn.close()


def run_in_subprocess(fn: Callable[..., Any], args: tuple = (), timeout: float = 120.0,
                      poll: float = 5.0, on_wait: Callable[[float], None] | None = None) -> Any:
    """Return ``fn(*args)`` computed in a child process; raise ItemTimeout after
    ``timeout`` s (child killed) or RuntimeError if the child failed/died.
    ``timeout <= 0`` runs in-process without a limit."""
    if timeout <= 0:
        return fn(*args)
    ctx = mp.get_context("fork") if "fork" in mp.get_all_start_methods() else mp.get_context()
    recv, send = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_child, args=(send, fn, args), daemon=True)
    t0 = time.monotonic()
    proc.start()
    send.close()
    try:
        while True:
            elapsed = time.monotonic() - t0
            left = timeout - elapsed
            if left <= 0:
                raise ItemTimeout(f"timed out after {timeout:.0f}s")
            if recv.poll(min(poll, left)):
                try:
                    status, value = recv.recv()
                except EOFError:
                    raise RuntimeError(f"worker died (exit {proc.exitcode})") from None
                if status == "err":
                    raise RuntimeError(value)
                return value
            if not proc.is_alive() and not recv.poll(0):
                raise RuntimeError(f"worker died (exit {proc.exitcode})")
            if on_wait:
                on_wait(time.monotonic() - t0)
    finally:
        recv.close()
        if proc.is_alive():
            proc.terminate()
            proc.join(2)
            if proc.is_alive():
                proc.kill()
        proc.join(1)


@contextmanager
def alarm(seconds: float) -> Iterator[None]:
    """Raise ItemTimeout in the main thread after ``seconds`` (0 = off)."""
    if seconds <= 0 or threading.current_thread() is not threading.main_thread() \
            or not hasattr(signal, "SIGALRM"):
        yield
        return

    def _fire(signum, frame):
        raise ItemTimeout(f"timed out after {seconds:.0f}s")

    old = signal.signal(signal.SIGALRM, _fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)
