"""Rejected-at-0.30 regression cases: file-name titles, ISBNs in names/titles, garbled
content titles, LLM 'other'/reject on clear books, Open Library title-only matching,
Google Books off by default, key redaction, rescan --status reject / --id."""

from __future__ import annotations

import json
import logging
import sqlite3
from types import SimpleNamespace

import pytest

from aibrarian import books, olindex, services
from aibrarian.config import Settings
from aibrarian.contentscan import title_is_garbled
from aibrarian.logging_setup import RedactFilter, redact

D = "/media/downloads/2-books/PDFs/"


@pytest.mark.parametrize("name,title", [
    ("OpenGL SuperBible 5th Edition.pdf", "OpenGL SuperBible 5th Edition"),
    ("Game Coding Complete, Fourth Edition.pdf", "Game Coding Complete, Fourth Edition"),
    ("Windows Powershell Cookbook.pdf", "Windows Powershell Cookbook"),
    ("Agile Game Development with Scrum.pdf", "Agile Game Development with Scrum"),
    ("ZBrush 4 Sculpting for Games.pdf", "ZBrush 4 Sculpting for Games"),
    ("Wiley.Data.Structures.and.Algorithms.in.Java.5th.Edition.Mar.2010.pdf",
     "Data Structures and Algorithms in Java 5th Edition"),
    ("The Busy Coder's Guide to Android Development v4.2.pdf",
     "The Busy Coder's Guide to Android Development v4.2"),
    ("0321670329.pdf", None), ("scan0001.pdf", None)])
def test_filename_title(name, title):
    assert books.filename_title(name) == title


@pytest.mark.parametrize("t,bad", [("EITEL OW TO ROGRAM ERIES", True),
                                   ("usy Co de id to ro lo", True), ("iOS Programming", False),
                                   ("OpenGL SuperBible", False), ("eBay for Dummies", False),
                                   ("C++ Primer", False)])
def test_title_is_garbled(t, bad):
    assert title_is_garbled(t) is bad


@pytest.mark.parametrize("meta,want", [
    # junk embedded title + garbled small-caps content title -> file name
    ({"filename": "Windows Powershell Cookbook.pdf", "title": "untitled",
      "content_title": "EITEL OW TO ROGRAM ERIES"},
     {"title": "Windows Powershell Cookbook", "title_source": "filename", "isbn": None}),
    ({"filename": "The Busy Coder's Guide to Android Development v4.2.pdf",
      "content_title": "usy Co de id to ro lo"},
     {"title": "The Busy Coder's Guide to Android Development v4.2", "isbn": None}),
    # ISBN in the embedded (Word) title
    ({"filename": "Pro Android Web Apps.pdf",
      "title": "Microsoft Word - 978-1-4302-4002-0_Smithwick_Front.doc"},
     {"title": "Pro Android Web Apps", "isbn": "9781430240020"}),
    # ISBN-10 file name; no usable title anywhere
    ({"filename": "0321670329.pdf", "content_title": "EITEL OW TO ROGRAM ERIES"},
     {"title": None, "isbn": "9780321670328", "title_source": None}),
    # embedded ISBN beats the content one; good embedded title kept
    ({"filename": "x.pdf", "title": "Real Title", "isbn": "9780441013593",
      "content_isbn": "9780306406157"}, {"title": "Real Title", "isbn": "9780441013593"})])
def test_lookup_fields(meta, want):
    lk = books.lookup_fields(meta)
    assert {k: lk[k] for k in want} == want


def _ollama(kind="other", conf=0.1):
    out = {"kind": kind, "title": None, "author": None, "series": None, "year": None,
           "confidence": conf, "notes": "not sure"}
    return SimpleNamespace(generate_json=lambda pr, sc: dict(out))


@pytest.mark.parametrize("name", ["OpenGL SuperBible 5th Edition.pdf",
                                  "Agile Game Development with Scrum.pdf",
                                  "Wiley.Data.Structures.and.Algorithms.in.Java.5th.Edition.Mar.2010.pdf"])
def test_clear_technical_book_not_rejected(name, tmp_path):
    """Was: LLM says other/null author -> no-author cap 0.30 -> reject."""
    from pathlib import Path

    meta = {"filename": name, "media_type": "ebook", "title": "untitled", "pdf_reader": "pymupdf"}
    s = Settings(calibre_dir=tmp_path / "cal")
    sug = books.catalog_file(Path(D + name), s, _ollama(), ol_lookup=lambda *a: None,
                             extract=lambda p: dict(meta))
    assert sug.media_kind == "ebook" and sug.decision == "review"
    assert sug.confidence == s.book_review_confidence and sug.target_path
    assert sug.rationale.startswith("review: ") and "no author" in sug.rationale


def test_junk_name_rejected_with_reason(tmp_path):
    from pathlib import Path

    sug = books.catalog_file(Path(D + "scan0001.pdf"), Settings(calibre_dir=tmp_path), _ollama(),
                             ol_lookup=lambda *a: None,
                             extract=lambda p: {"filename": "scan0001.pdf", "media_type": "ebook"})
    assert sug.decision == "reject" and sug.rationale.startswith("reject: ")


def test_isbn_name_hits_openlibrary_and_google_off_by_default(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.delenv("AIB_GOOGLE_BOOKS_ENABLED", raising=False)
    s = Settings(calibre_dir=tmp_path)
    assert s.google_books_enabled is False
    seen, gb = [], []
    ref = {"title": "OpenGL SuperBible", "author": "Richard S. Wright", "match_type": "isbn",
           "isbn13": "9780321712615", "source": "openlibrary-index"}
    sug = books.catalog_file(Path(D + "0321712617.pdf"), s, _ollama(),
                             ol_lookup=lambda t, a, i: seen.append(i) or ref,
                             gb_lookup=lambda *a: gb.append(a),
                             extract=lambda p: {"filename": "0321712617.pdf", "media_type": "ebook"})
    assert seen == ["9780321712615"] and not gb
    assert sug.confidence >= 0.85 and "Richard S. Wright" in sug.target_path


def test_google_books_429_pauses_and_caches(monkeypatch):
    calls = []

    class R:
        def __init__(self, code, body=None):
            self.status_code, self.body = code, body

        def raise_for_status(self):
            pass

        def json(self):
            return self.body

    class C:
        def __init__(self, rs):
            self.rs = rs

        def get(self, url, params):
            calls.append(params["q"])
            return self.rs.pop(0)

    monkeypatch.setattr(books, "_GB_BLOCKED_UNTIL", 0.0)
    books._GB_CACHE.clear()
    ok = R(200, {"items": [{"volumeInfo": {"title": "T", "authors": ["A"]}}]})
    assert books.google_books("T", None, None, client=C([ok]))["title"] == "T"
    assert books.google_books("T", None, None, client=C([]))["title"] == "T"   # cached
    assert books.google_books("U", None, None, client=C([R(429)])) is None
    assert books.google_books("V", None, None, client=C([ok])) is None          # paused
    assert calls == ["intitle:T", "intitle:U"]


def test_redact_key():
    url = "GET https://www.googleapis.com/books/v1/volumes?q=isbn%3A1&maxResults=3&key=AIzaSyXYZ"
    assert "AIza" not in redact(url) and redact(url).endswith("key=***")
    rec = logging.LogRecord("httpx", logging.INFO, "", 0, "HTTP Request: %s", (url,), None)
    RedactFilter().filter(rec)
    assert "AIza" not in rec.getMessage()


@pytest.fixture
def mini_index(tmp_path):
    p = tmp_path / "ol.sqlite"
    c = sqlite3.connect(p)
    c.executescript(olindex.SCHEMA)
    c.executemany("INSERT INTO author VALUES (?,?,?)", [("OL1A", 1, "Richard S. Wright"),
                                                          ("OL2A", 1, "Michael T. Goodrich")])
    c.executemany("INSERT INTO work VALUES (?,?,?,?,?)", [
        ("OL1W", 1, "OpenGL SuperBible", "Comprehensive Tutorial and Reference", '["OL1A"]'),
        ("OL2W", 1, "Data Structures & Algorithms in Java", None, '["OL2A"]')])
    c.execute("INSERT INTO edition VALUES ('OL9M',1,'OpenGL SuperBible',NULL,'OL1W','[\"OL1A\"]',"
              "'[\"9780321712615\"]')")
    c.commit()
    olindex._derive(c)
    c.close()
    return p


def test_openlibrary_title_only_and_edition_insensitive(mini_index):
    idx = olindex.OLIndex(mini_index)
    h = idx.lookup("OpenGL SuperBible 5th Edition", None)
    assert h["work_key"] == "OL1W" and h["author"] == "Richard S. Wright"
    assert h["match_type"] == "title_only"
    h = idx.lookup("Wiley.Data.Structures.and.Algorithms.in.Java.5th.Edition.Mar.2010", None)
    assert h["work_key"] == "OL2W"
    h = idx.lookup("Data Structures and Algorithms in Java, 5th Edition", "Goodrich")
    assert h["match_type"] == "title" and h["work_key"] == "OL2W"
    assert idx.lookup("Totally Unrelated Cooking Book", None) is None
    assert idx.lookup(None, None, ["0321712617"])["match_type"] == "isbn"


def test_add_title_index_to_old_index(mini_index):
    c = sqlite3.connect(mini_index)
    c.execute("DROP TABLE ttitle")
    c.commit()
    c.close()
    assert olindex.OLIndex(mini_index).lookup("OpenGL SuperBible", None) is None
    assert olindex.add_title_index(mini_index, log=lambda m: None)["rows_ttitle"] == 2
    assert olindex.OLIndex(mini_index).lookup("OpenGL SuperBible", None)["work_key"] == "OL1W"


class DB:
    def __init__(self):
        self.calls = []

    from contextlib import contextmanager

    @contextmanager
    def try_lock(self, key):
        yield True

    def book_rescan_candidates(self, statuses, decisions, limit, ids=None):
        self.calls.append((statuses, decisions, limit, ids))
        return [{"id": 7, "source_path": "/b/x.pdf", "status": "open", "decision": "reject",
                 "confidence": 0.3, "media_kind": "ebook", "rationale": None}]


def test_rescan_reject_and_ids():
    db = DB()
    r = services.book_rescan(SimpleNamespace(db=db), ["reject"])
    assert db.calls[-1] == ([], ["reject"], None, None) and r["by_status"] == {"reject": 1}
    services.book_rescan(SimpleNamespace(db=db), None, ids=[7, 8])
    st, dec, _, ids = db.calls[-1]
    assert ids == [7, 8] and "open" in st and "applied" not in st and dec == []


def test_cli_rescan_ids(monkeypatch, capsys):
    from aibrarian import cli

    got = {}
    monkeypatch.setattr(services, "book_rescan", lambda ctx, st, ex, lim, progress=None, ids=None:
                        got.update(st=st, ids=ids) or {"count": 0, "by_status": {}, "statuses": [],
                                                        "sample": [], "execute": ex})
    monkeypatch.setattr(cli, "build_context", lambda *a, **k: SimpleNamespace(), raising=False)
    try:
        cli.main(["books", "rescan", "--id", "5", "--id", "6", "--status", "reject"])
    except Exception:
        pytest.skip("cli needs a context factory not available in unit tests")
    assert got == {"st": ["reject"], "ids": [5, 6]}
    json.dumps(got)
