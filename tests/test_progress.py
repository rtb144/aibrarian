import logging

from aibrarian.progress import ProgressReporter, progress_interval, run_logger


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class Cap:
    def __init__(self):
        self.lines = []

    def info(self, m):
        self.lines.append(m)


def test_interval_env(monkeypatch):
    monkeypatch.delenv("AIB_PROGRESS_LOG_SECONDS", raising=False)
    assert progress_interval() == 900
    monkeypatch.setenv("AIB_PROGRESS_LOG_SECONDS", "60")
    assert progress_interval() == 60
    monkeypatch.setenv("AIB_PROGRESS_LOG_SECONDS", "junk")
    assert progress_interval() == 900


def test_ticks_only_after_interval():
    c, cap = Clock(), Cap()
    r = ProgressReporter("book-scan", total=100, interval=900, logger=cap, clock=c)
    r.start()
    c.t = 10
    assert not r.tick(current="a.epub", processed=1)
    c.t = 1800
    assert r.tick(current="b.epub", processed=50, skipped_resume=3, errors=0)
    c.t = 1900
    assert not r.tick(processed=51)
    r.finish(processed=100)
    assert len(cap.lines) == 3
    line = cap.lines[1]
    for part in ("elapsed=30m00s", "processed=50/100", "skipped_resume=3", "rate=100/h",
                 "remaining=50", "eta=30m00s", "current=b.epub"):
        assert part in line, line
    assert "book-scan done" in cap.lines[2]


def test_unknown_total_has_no_eta():
    c, cap = Clock(), Cap()
    r = ProgressReporter("x", interval=0, logger=cap, clock=c)
    c.t = 3600
    r.tick(processed=5)
    assert "processed=5 " in cap.lines[0] and "eta" not in cap.lines[0]


def test_falls_back_outside_prefect():
    assert isinstance(run_logger(), logging.Logger)


def test_book_scan_ticks(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from aibrarian import services

    files = [tmp_path / f"b{i}.epub" for i in range(3)]
    monkeypatch.setattr(services.books, "iter_book_files", lambda dirs: iter(files))
    monkeypatch.setattr(services.books, "catalog_file",
                        lambda p, *a, **k: SimpleNamespace(decision="review"))
    monkeypatch.setattr("aibrarian.olindex.make_ref_lookup", lambda s: None)
    monkeypatch.setattr(services, "make_ol_lookup", lambda db: None)
    db = SimpleNamespace(start_run=lambda *a: "r1", suggestion_sources=lambda k: {str(files[0])},
                         save_suggestion=lambda s: 1, finish_run=lambda *a: None)
    ctx = SimpleNamespace(settings=SimpleNamespace(book_source_dirs=[tmp_path]), db=db,
                          ollama=None)
    cap = Cap()
    rep = ProgressReporter("book-scan", interval=0, logger=cap)
    res = services.book_scan(ctx, reporter=rep)
    assert res["processed"] == 2 and rep.total == 2
    assert "total=2" in cap.lines[0] and "processed=2/2" in cap.lines[-1]
    assert any("current=b2.epub" in x for x in cap.lines)
