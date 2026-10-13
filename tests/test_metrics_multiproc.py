"""Counters incremented in child processes must reach the serving registry."""
import os
import subprocess
import sys

CHILD = """
from aibrarian.metrics import ACTION_TOTAL, ACTION_SECONDS
ACTION_TOTAL.labels(action="probe", outcome="ok").inc()
ACTION_SECONDS.labels(action="probe").observe(0.2)
"""

PARENT = f"""
import subprocess, sys
from aibrarian.metrics import reset_multiprocess_dir
reset_multiprocess_dir()
for _ in range(3):
    subprocess.run([sys.executable, "-c", {CHILD!r}], check=True)
from prometheus_client import CollectorRegistry, generate_latest, multiprocess
reg = CollectorRegistry()
multiprocess.MultiProcessCollector(reg)
print(generate_latest(reg).decode())
"""


def test_multiprocess_aggregation(tmp_path):
    env = dict(os.environ, PROMETHEUS_MULTIPROC_DIR=str(tmp_path / "mp"))
    out = subprocess.run([sys.executable, "-c", PARENT], env=env, check=True,
                         capture_output=True, text=True).stdout
    assert 'aib_action_total{action="probe",outcome="ok"} 3.0' in out
    assert 'aib_action_duration_seconds_count{action="probe"} 3.0' in out
    # missing dir is created on import (CLI exec path)
    assert (tmp_path / "mp").is_dir()
