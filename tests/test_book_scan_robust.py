"""book-scan: per-file hard timeout, resume skip counting, single-run lock."""

from __future__ import annotations

import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from aibrarian import services
from aibrarian.progress import ProgressReporter
from aibrarian.timeouts import ItemTimeout, alarm, run_in_subprocess


class Cap:
    def __init__(self):
        self.lines, self.warnings = [], []

    def info(self, m):
        self.lines.append(m)

    def debug(self, m):
        pass

    def warning(self, m):
        self.warnings.append(m)


def _hang(_p):
    time.sleep(60)


def _ok(p):
    return {"title": str(p)}


def test_run_in_subprocess_kills_hang_and_ticks():
    waits = []
    t0 = time.monotonic()
    with pytest.raises(ItemTimeout):
        run_in_subprocess(_hang, ("x",), timeout=1.0, poll=0.2, on_wait=waits.append)
    assert time.monotonic() - t0 < 5 and waits          # progress callbacks mid-file
    assert run_in_subprocess(_ok, ("a",), timeout=5) == {"title": "a"}


def test_alarm_interrupts():
    with pytest.raises(ItemTimeout), alarm(0.3):
        time.sleep(5)


class DB:
    def __init__(self, keys, got=True):
        self.keys, self.got = keys, got
        self.saved, self.errors = [], []

    def start_run(self, *a):
        return 1

    def finish_run(self, *a):
        pass

    def book_scan_skip_keys(self):
        return self.keys

    def save_suggestion(self, s):
        self.saved.append(s)
        return len(self.saved)

    def save_scan_error(self, domain, path, err, run_id=None):
        self.errors.append((path, err))

    @contextmanager
    def try_lock(self, key):
        yield self.got


def _ctx(monkeypatch, tmp_path, files, db):
    monkeypatch.setattr(services.books, "iter_book_files", lambda dirs: iter(files))
    monkeypatch.setattr("aibrarian.olindex.make_ref_lookup", lambda s: None)
    monkeypatch.setattr(services, "make_ol_lookup", lambda db: None)
    st = SimpleNamespace(book_source_dirs=[tmp_path / "in"], calibre_dir=tmp_path / "calibre")
    return SimpleNamespace(settings=st, db=db, ollama=None)


def test_hanging_file_recorded_as_error_and_scan_continues(monkeypatch, tmp_path):
    monkeypatch.setenv("AIB_BOOK_EXTRACT_TIMEOUT_S", "1")
    files = [tmp_path / "in" / "bad.pdf", tmp_path / "in" / "good.epub"]

    def extract(p):
        if p.name == "bad.pdf":
            time.sleep(60)
        return {"title": "ok"}

    monkeypatch.setattr(services.books, "extract_metadata", extract)

    def catalog(p, *a, extract, **k):
        extract(p)
        return SimpleNamespace(decision="review")

    monkeypatch.setattr(services.books, "catalog_file", catalog)
    db = DB({"suggested": set(), "applied": set()})
    cap = Cap()
    t0 = time.monotonic()
    res = services.book_scan(_ctx(monkeypatch, tmp_path, files, db),
                             reporter=ProgressReporter("book-scan", interval=0, logger=cap))
    assert time.monotonic() - t0 < 10
    assert res["errors"] == 1 and res["processed"] == 1
    assert db.errors[0][0].endswith("bad.pdf") and "timed out" in db.errors[0][1]
    assert any("bad.pdf" in w for w in cap.warnings)


def test_resume_skip_counts(monkeypatch, tmp_path):
    inn, cal = tmp_path / "in", tmp_path / "calibre"
    files = [inn / "a.epub", inn / "b.pdf", inn / "c.epub", inn / "d.mkv",
             cal / "X" / "e.epub", inn / "f.epub"]
    # 'a' known under a non-normalised spelling, 'c' is where an applied one landed
    keys = {"suggested": {str(inn / "a.epub")}, "applied": {str(inn / "c.epub")}}
    monkeypatch.setattr(services.books, "catalog_file",
                        lambda p, *a, **k: SimpleNamespace(decision="review"))
    db = DB(keys)
    files[0] = inn / "sub" / ".." / "a.epub"
    cap = Cap()
    res = services.book_scan(_ctx(monkeypatch, tmp_path, files, db),
                             reporter=ProgressReporter("book-scan", interval=0, logger=cap))
    assert res["processed"] == 2 and res["skipped_resume"] == 4
    start = cap.lines[0]
    assert "total=2" in start and "files_found=6 skipped=4" in start
    assert "video=1 suggested=1 applied=1 in_calibre=1" in start


def test_lock_held_exits_quickly(monkeypatch, tmp_path):
    db = DB({"suggested": set(), "applied": set()}, got=False)
    monkeypatch.setattr(services.books, "iter_book_files",
                        lambda d: pytest.fail("must not scan"))
    cap = Cap()
    res = services.book_scan(SimpleNamespace(settings=None, db=db, ollama=None),
                             reporter=ProgressReporter("book-scan", logger=cap))
    assert res["locked"] == 1 and "already running" in cap.lines[0]


def test_book_scan_deployment_single_run():
    from prefect.client.schemas.objects import ConcurrencyLimitStrategy

    from aibrarian import flows

    d = {x.name: x for x in flows.deployments("cpu")}["book-scan"]
    assert d.concurrency_limit == 1 or d.concurrency_limit.limit == 1
    opts = getattr(d, "concurrency_options", None) or d.concurrency_limit
    assert opts.collision_strategy == ConcurrencyLimitStrategy.CANCEL_NEW
    assert d.schedules
