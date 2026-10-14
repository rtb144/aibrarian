"""'Title - Author.mobi' downloads: filename split, MOBI/EXTH metadata, accent/initial
tolerant author matching, >= 0.90 with an Open Library hit, books explain."""

from __future__ import annotations

import sqlite3
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from aibrarian import books, olindex, services
from aibrarian.bookmeta import _author_ratio
from aibrarian.config import Settings

D = "/media/downloads/2-books/A Philosophy Collection (Kindle Books)/"
CASES = [
    ("50 Big Ideas You Really Need to Know - Ben Dupre.mobi",
     "50 Big Ideas You Really Need to Know", "Ben Dupre"),
    ("A Little History of Philosophy - Warburton, Nigel.mobi",
     "A Little History of Philosophy", "Nigel Warburton"),
    ("A Universe From Nothing - Krauss, Lawrence M_.mobi", "A Universe From Nothing",
     "Lawrence M. Krauss"),
    ("Anarchism_ A Very Short Introduction - Ward, Colin.mobi",
     "Anarchism: A Very Short Introduction", "Colin Ward"),
    ("Against Intellectual Monopoly - Michele Boldrin;David K. Levine.mobi",
     "Against Intellectual Monopoly", "Michele Boldrin; David K. Levine"),
]


@pytest.mark.parametrize("name,title,author", CASES + [
    ("OpenGL SuperBible 5th Edition.pdf", "OpenGL SuperBible 5th Edition", None),
    ("Star Wars - The Last Jedi.epub", "Star Wars - The Last Jedi", None),
    ("Warburton, Nigel - A Little History of Philosophy.epub",
     "A Little History of Philosophy", "Nigel Warburton")])
def test_split_filename(name, title, author):
    assert books.split_filename(name) == (title, author)


@pytest.mark.parametrize("a,b", [("Ben Dupre", "Ben Dupré"),
                                 ("Lawrence M. Krauss", "Lawrence Maxwell Krauss"),
                                 ("Warburton, Nigel", "Nigel Warburton"),
                                 ("Michele Boldrin; David K. Levine", "David K. Levine"),
                                 ("BEN DUPRÉ", "ben dupre")])
def test_author_ratio_tolerant(a, b):
    assert _author_ratio(a, b) == 1.0


def test_author_ratio_different_person():
    assert _author_ratio("Ben Dupre", "Anne Dupre") < 1.0
    assert _author_ratio("Ben Dupre", "Nigel Warburton") < 0.6


@pytest.fixture
def index(tmp_path):
    p = tmp_path / "ol.sqlite"
    c = sqlite3.connect(p)
    c.executescript(olindex.SCHEMA)
    c.executemany("INSERT INTO author VALUES (?,?,?)", [
        ("OL1A", 1, "Ben Dupré"), ("OL2A", 1, "Nigel Warburton"),
        ("OL3A", 1, "Lawrence Maxwell Krauss"), ("OL4A", 1, "Colin Ward"),
        ("OL5A", 1, "Michele Boldrin"), ("OL6A", 1, "David K. Levine")])
    c.executemany("INSERT INTO work VALUES (?,?,?,?,?)", [
        ("OL1W", 1, "50 Big Ideas You Really Need To Know", None, '["OL1A"]'),
        ("OL2W", 1, "A Little History of Philosophy", None, '["OL2A"]'),
        ("OL3W", 1, "A Universe from Nothing", "Why There Is Something Rather Than Nothing",
         '["OL3A"]'),
        ("OL4W", 1, "Anarchism", "A Very Short Introduction", '["OL4A"]'),
        ("OL5W", 1, "Against Intellectual Monopoly", None, '["OL5A", "OL6A"]')])
    c.commit()
    olindex._derive(c)
    c.close()
    return p


def _llm(kind="ebook"):
    return SimpleNamespace(generate_json=lambda pr, sc: {
        "kind": kind, "title": None, "author": None, "series": None, "year": None,
        "confidence": 0.5, "notes": ""})


@pytest.mark.parametrize("name,title,author", CASES)
def test_mobi_names_score_high_with_openlibrary(name, title, author, index, tmp_path):
    idx = olindex.OLIndex(index)
    ref = lambda t, a, i=None: idx.lookup(t, a, i)  # noqa: E731
    sug = books.catalog_file(Path(D + name), Settings(calibre_dir=tmp_path / "cal"), _llm(),
                             ol_lookup=lambda t, a, i: ref(t, a, [i] if i else []),
                             ref_lookup=ref, extract=books.extract_metadata)
    assert sug.confidence >= 0.90, sug.metadata
    assert sug.decision in ("suggest", "auto")


def test_canonical_author_from_openlibrary(index, tmp_path):
    idx = olindex.OLIndex(index)
    ref = lambda t, a, i=None: idx.lookup(t, a, i)  # noqa: E731
    sug = books.catalog_file(Path(D + CASES[0][0]), Settings(calibre_dir=tmp_path / "cal"),
                             _llm(), ol_lookup=lambda t, a, i: ref(t, a, []), ref_lookup=ref,
                             extract=books.extract_metadata)
    assert "/Ben Dupré/" in sug.target_path


def make_mobi(path: Path, title="Full Name Title", exth=None, utf8=True) -> Path:
    exth = exth or [(503, "A Little History of Philosophy"), (100, "Warburton, Nigel"),
                    (104, "978-0-300-18779-7"), (101, "Yale University Press"),
                    (106, "2011-09-27")]
    recs = b"".join(struct.pack(">II", t, 8 + len(v.encode())) + v.encode() for t, v in exth)
    exth_b = b"EXTH" + struct.pack(">II", 12 + len(recs), len(exth)) + recs
    hlen = 232
    mobi = bytearray(hlen)
    mobi[0:4] = b"MOBI"
    struct.pack_into(">I", mobi, 4, hlen)
    struct.pack_into(">I", mobi, 12, 65001 if utf8 else 1252)
    name = title.encode()
    name_off = 16 + hlen + len(exth_b)
    struct.pack_into(">II", mobi, 0x44, name_off, len(name))
    struct.pack_into(">I", mobi, 0x70, 0x40)
    rec0 = b"\0" * 16 + bytes(mobi) + exth_b + name + b"\0\0"
    head = bytearray(78)
    head[0:len(b"test")] = b"test"
    head[60:68] = b"BOOKMOBI"
    struct.pack_into(">H", head, 76, 2)
    r0 = 78 + 16 + 2
    offs = struct.pack(">II", r0, 0) + struct.pack(">II", r0 + len(rec0), 1)
    path.write_bytes(bytes(head) + offs + b"\0\0" + rec0 + b"text")
    return path


def test_mobi_exth_metadata(tmp_path):
    p = make_mobi(tmp_path / "x.mobi")
    m = books.mobi_metadata(p)
    assert m["title"] == "A Little History of Philosophy"  # EXTH 503 beats full name
    assert m["author"] == "Warburton, Nigel" and m["isbn"] == "9780300187797"
    assert m["publisher"] == "Yale University Press" and m["year"] == 2011
    meta = books.extract_metadata(p)
    assert meta["title"] == "A Little History of Philosophy" and "extract_error" not in meta


def test_mobi_full_name_fallback_and_garbage(tmp_path):
    p = make_mobi(tmp_path / "y.azw3", title="Only Full Name", exth=[(100, "Jo Doe")])
    assert books.mobi_metadata(p) == {"title": "Only Full Name", "author": "Jo Doe"}
    bad = tmp_path / "bad.mobi"
    bad.write_bytes(b"not a mobi" * 20)
    meta = books.extract_metadata(bad)
    assert "extract_error" in meta and meta["name_title"] == "bad"


def test_books_explain(index, tmp_path):
    p = make_mobi(tmp_path / "A Universe From Nothing - Krauss, Lawrence M_.mobi",
                  exth=[(100, "Krauss, Lawrence M.")], title="A Universe From Nothing")
    s = Settings(calibre_dir=tmp_path / "cal", ol_index_path=index)
    stored = {"id": 9, "source_path": str(p), "status": "open", "decision": "review",
              "confidence": 0.65, "rationale": "x", "target_path": None, "media_kind": "ebook",
              "metadata": {"classification": {"kind": "ebook"}}}
    ctx = SimpleNamespace(settings=s, db=SimpleNamespace(get_suggestion=lambda i: stored))
    out = services.book_explain(ctx, 9)
    assert out["stored"]["confidence"] == 0.65 and out["ol_index"]["title_table"]
    assert out["lookup_fields"]["author"] == "Krauss, Lawrence M."
    kinds = [t["lookup"] for t in out["ol_lookups"]]
    assert kinds == ["title+author", "title_only"]
    assert out["ol_lookups"][0]["hit"]["work_key"] == "OL3W"
    assert out["score"]["confidence"] >= 0.90 and "vs reference" in out["score"]["score_reason"]
