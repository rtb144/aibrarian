"""PyMuPDF metadata, content scan (ISBN checksum, title page), weak-metadata detection,
books rescan dry run / lock."""

from __future__ import annotations

import logging
import re
import zipfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from aibrarian import books, contentscan, services
from aibrarian.contentscan import find_isbns, isbn10_ok, isbn13_ok, title_is_garbage, weak_reasons

fitz = pytest.importorskip("fitz")


def _broken_pdf(path: Path, title_meta: str = "Microsoft Word - draft.doc") -> Path:
    doc = fitz.open()
    p = doc.new_page()
    p.insert_text((72, 120), "The Silent Harbour", fontsize=28)
    p.insert_text((72, 170), "by Margaret Ellison", fontsize=14)
    p2 = doc.new_page()
    p2.insert_text((72, 100), "Copyright (c) 2019 Margaret Ellison. All rights reserved.",
                   fontsize=9)
    p2.insert_text((72, 115), "ISBN 978-0-306-40615-7 (hardcover)", fontsize=9)
    p2.insert_text((72, 130), "Phone 0306406152 ext 12", fontsize=9)
    for _ in range(3):
        doc.new_page().insert_text((72, 100), "Chapter text. " * 20, fontsize=10)
    doc.set_metadata({"title": title_meta, "author": ""})
    raw = doc.tobytes(garbage=0, deflate=False, no_new_id=True)
    doc.close()
    # break every xref offset and the startxref pointer -> needs a full rebuild
    raw = re.sub(rb"\d{10} 00000 n", b"0000000000 00000 n", raw)
    raw = re.sub(rb"startxref\s+\d+", b"startxref\n0", raw)
    path.write_bytes(raw)
    return path


def test_isbn_checksums():
    assert isbn13_ok("9780306406157") and not isbn13_ok("9780306406158")
    assert isbn10_ok("0306406152") and isbn10_ok("080442957X") and not isbn10_ok("0306406153")


def test_find_isbns_prefers_labelled_copyright_and_validates():
    pages = ["Call 0306406152 now 9780306406158",                     # bad checksum / unlabelled
             "Copyright 2001. All rights reserved.\nISBN-10: 0-306-40615-2\n"
             "ISBN 978-0-306-40615-7 (pbk)\nAudiobook ISBN 978-1-4028-9462-6",
             "random 9781861972712 on a chapter page"]
    got = [c["isbn"] for c in find_isbns(pages)]
    assert got[0] == "9780306406157"
    assert "0306406152" in got and "9780306406158" not in got
    assert got.index("9781402894626") > got.index("9781861972712")   # other-format last
    assert find_isbns(["order no. 0306406152"]) == []                # ISBN-10 needs label


@pytest.mark.parametrize("t,bad", [
    ("Microsoft Word - x.doc", True), ("untitled", True), ("Untitled-1", True), ("12345", True),
    ("Canon iR-ADV C5535", True), ("scan0001", True), ("report_final_v2.pdf", True),
    ("IMG_2041", True), ("", True), (None, True),
    ("Dune", False), ("Scandinavian Folklore", False), ("Canon of Medicine", False),
    ("Sharp Objects", False), ("The Brothers Karamazov", False), ("1984", True)])
def test_title_is_garbage(t, bad):
    assert title_is_garbage(t) is bad


def test_weak_reasons():
    assert weak_reasons({"title": "Dune", "author": "Frank Herbert", "isbn": "9780441013593"}) == []
    assert weak_reasons({"title": "Dune", "author": "Frank Herbert"}) == ["no_isbn"]
    assert weak_reasons({"title": "Microsoft Word - a.doc"}) == ["garbage_title", "no_author",
                                                                  "no_isbn"]


def test_broken_xref_pdf_pymupdf_metadata_and_content(tmp_path, caplog):
    pdf = _broken_pdf(tmp_path / "broken.pdf")
    meta = books.extract_metadata(pdf)
    assert meta["pdf_reader"] == "pymupdf" and meta["pages"] == "5"
    assert "garbage_title" in meta["weak_metadata"] and meta["content_scan"] is True
    assert meta["content_isbn"] == "9780306406157"
    assert meta["content_title"] == "The Silent Harbour"
    assert meta["content_author"] == "Margaret Ellison"
    assert "Silent Harbour" in meta["content_snippet"] and len(meta["content_snippet"]) <= 1500
    lk = books.lookup_fields(meta)
    assert lk == {"title": "The Silent Harbour", "author": "Margaret Ellison",
                  "isbn": "9780306406157", "title_source": "content"}


def test_pypdf_fallback_when_pymupdf_raises(tmp_path, monkeypatch):
    pdf = _broken_pdf(tmp_path / "b.pdf", title_meta="Real Title")
    def boom(p):
        raise RuntimeError("mupdf exploded")
    monkeypatch.setattr(books, "_pymupdf_metadata", boom)
    pytest.importorskip("pypdf")
    out = books.pdf_metadata(pdf)
    assert out["pdf_reader"] == "pypdf" and "mupdf exploded" in out["pymupdf_error"]
    assert logging.getLogger("pypdf._reader").level == logging.ERROR


def test_strong_metadata_skips_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(books, "pdf_metadata", lambda p: {"title": "Dune", "author": "F H",
                                                          "isbn": "9780441013593"})
    called = []
    monkeypatch.setattr(contentscan, "scan", lambda *a: called.append(a) or {})
    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF")
    assert "content_scan" not in books.extract_metadata(p) and not called


def test_epub_content_scan(tmp_path):
    p = tmp_path / "b.epub"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="OEBPS/c.opf"/>'
                   "</rootfiles></container>")
        z.writestr("OEBPS/c.opf", '<package xmlns:dc="http://purl.org/dc/elements/1.1/"><metadata>'
                   "<dc:title>untitled</dc:title></metadata><manifest>"
                   '<item id="t" href="t.xhtml"/><item id="c" href="c.xhtml"/></manifest>'
                   '<spine><itemref idref="t"/><itemref idref="c"/></spine></package>')
        z.writestr("OEBPS/t.xhtml", "<html><body><h1>Winter Roads</h1><p>by Jonas Pell</p></body></html>")
        z.writestr("OEBPS/c.xhtml", "<html><body><p>&copy; 2020. ISBN: 978-1-86197-271-2</p></body></html>")
    meta = books.extract_metadata(p)
    assert meta["content_isbn"] == "9781861972712"
    assert meta["content_title"] == "Winter Roads" and meta["content_author"] == "Jonas Pell"


def test_catalog_uses_content_isbn_and_snippet(tmp_path):
    from aibrarian.config import Settings

    seen, prompts = [], []
    meta = {"filename": "x.pdf", "media_type": "ebook", "title": "untitled",
            "content_isbn": "9780306406157", "content_title": "The Silent Harbour",
            "content_snippet": "THE SILENT HARBOUR copyright page"}
    ollama = SimpleNamespace(generate_json=lambda pr, sc: prompts.append(pr) or
                             {"kind": "ebook", "title": None, "author": None, "series": None,
                              "year": None, "confidence": 0.5, "notes": ""})
    books.catalog_file(tmp_path / "x.pdf", Settings(), ollama,
                       ol_lookup=lambda t, a, i: seen.append((t, a, i)) or None,
                       gb_lookup=lambda *a: None, extract=lambda p: dict(meta))
    assert seen == [("The Silent Harbour", None, "9780306406157")]
    assert "THE SILENT HARBOUR copyright page" in prompts[0]


class RescanDB:
    def __init__(self, got=True):
        self.got, self.calls, self.updated = got, [], []

    @contextmanager
    def try_lock(self, key):
        self.lock_key = key
        yield self.got

    def book_rescan_candidates(self, statuses, review_open, limit):
        self.calls.append((statuses, review_open, limit))
        return [{"id": i, "source_path": f"/b/{i}.pdf", "status": st, "decision": "reject",
                 "confidence": 0.0, "media_kind": None, "rationale": None}
                for i, st in enumerate(["error", "needs_review", "open"], 1)][:limit]

    def rescan_suggestion(self, *a):
        self.updated.append(a)


def test_rescan_dry_run_lists_and_changes_nothing():
    db = RescanDB()
    res = services.book_rescan(SimpleNamespace(db=db), limit=2)
    assert db.lock_key == services.BOOK_SCAN_LOCK == 724413
    assert db.calls == [(["error", "needs_review"], ["review", "reject"], 2)]
    assert res["count"] == 2 and res["by_status"] == {"error": 1, "needs_review": 1}
    assert [s["id"] for s in res["sample"]] == [1, 2] and not db.updated and "results" not in res


def test_rescan_lock_held_and_applied_refused():
    db = RescanDB(got=False)
    res = services.book_rescan(SimpleNamespace(db=db), ["error"], execute=True)
    assert res["locked"] == 1 and "book-scan" in res["message"] and not db.calls
    with pytest.raises(ValueError):
        services.book_rescan(SimpleNamespace(db=RescanDB()), ["applied"])
