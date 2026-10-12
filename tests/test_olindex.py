import json
import sqlite3

import pyarrow as pa
import pytest
from deltalake import write_deltalake
from fastapi.testclient import TestClient

from aibrarian import booklib, bookmeta, olindex
from aibrarian.api import create_app
from aibrarian.cli import main as cli_main

KEY = pa.struct([("key", pa.string())])
AUTH_ROLE = pa.struct([("author", KEY), ("type", KEY)])


def _write(delta, name, rows, schema, mode="append"):
    write_deltalake(str(delta / name), pa.Table.from_pylist(rows, schema=schema), mode=mode)


def make_delta(delta):
    """Mixed types on purpose: array<string>, array<struct{key}>, array<struct{author{key}}>,
    JSON strings; duplicate record_keys with several revisions; two files per table."""
    a_schema = pa.schema([("record_key", pa.string()), ("revision", pa.int64()),
                          ("updated_at", pa.string()), ("payload_json", pa.string()),
                          ("name", pa.string()), ("personal_name", pa.string())])
    _write(delta, "authors", [
        {"record_key": "/authors/OL1A", "revision": 1, "name": "Frank Herbert Jr",
         "payload_json": "{}"},
        {"record_key": "/authors/OL2A", "revision": 3, "name": "Jo Doe"},
        {"record_key": "/authors/OL3A", "revision": 1, "name": None, "personal_name": "Ann Leckie"},
    ], a_schema)
    _write(delta, "authors", [{"record_key": "/authors/OL1A", "revision": 2,
                               "name": "Frank Herbert"}], a_schema)
    w_schema = pa.schema([("record_key", pa.string()), ("revision", pa.int64()),
                          ("title", pa.string()), ("subtitle", pa.string()),
                          ("authors", pa.list_(AUTH_ROLE)), ("payload_json", pa.string())])
    role = lambda k: {"author": {"key": k}, "type": {"key": "/type/author_role"}}  # noqa: E731
    _write(delta, "works", [
        {"record_key": "/works/OL10W", "revision": 5, "title": "Dune", "authors": [role("/authors/OL1A")]},
        {"record_key": "/works/OL11W", "revision": 1, "title": "Amnesia",
         "subtitle": "A Josephine Chronicles Mystery", "authors": [role("/authors/OL2A")]},
        {"record_key": "/works/OL12W", "revision": 1, "title": "Ancillary Justice",
         "authors": [role("/authors/OL3A")]},
    ], w_schema)
    _write(delta, "works", [{"record_key": "/works/OL10W", "revision": 4, "title": "Old Dune",
                             "authors": [role("/authors/OL1A")]}], w_schema)
    e_schema = pa.schema([("record_key", pa.string()), ("revision", pa.int64()),
                          ("title", pa.string()), ("subtitle", pa.string()),
                          ("isbn10", pa.string()), ("isbn13", pa.list_(pa.string())),
                          ("works", pa.string()), ("authors", pa.list_(KEY)),
                          ("payload_json", pa.string())])
    _write(delta, "editions", [
        {"record_key": "/books/OL100M", "revision": 2, "title": "Dune", "isbn10": None,
         "isbn13": ["978-0-441-01359-3"], "works": json.dumps([{"key": "/works/OL10W"}]),
         "authors": [{"key": "/authors/OL1A"}]},
        {"record_key": "/books/OL101M", "revision": 1, "title": "Amnesia",
         "isbn10": json.dumps(["1503071278"]), "isbn13": None,
         "works": '["/works/OL11W"]', "authors": None},
    ], e_schema)
    _write(delta, "editions", [
        {"record_key": "/books/OL100M", "revision": 1, "title": "Dune (old revision)",
         "isbn13": ["9780000000002"], "works": None, "authors": None}], e_schema)


@pytest.fixture
def built(settings, tmp_path):
    make_delta(settings.ol_delta_dir)
    lines = []
    st = olindex.build_index(settings.ol_delta_dir, settings.ol_index_path, log=lines.append)
    return settings, st, lines


def test_value_normalisation():
    assert olindex.as_keys('[{"author": {"key": "/authors/OL1A"}}]') == ["OL1A"]
    assert olindex.as_keys([{"key": "/works/OL9W"}]) == ["OL9W"]
    assert olindex.as_keys(["/authors/OL2A"]) == ["OL2A"]
    assert olindex.as_keys("/works/OL9W") == ["OL9W"]
    assert olindex.as_strs('["0441013597"]') == ["0441013597"]
    assert olindex.isbn13("0-441-01359-7") == "9780441013593"
    assert olindex.isbns_in_name("1503071278.epub") == ["9781503071278"]
    assert olindex.isbns_in_name("Book 2019 1234567890.epub") == []          # bad check digit
    assert olindex.surname("Herbert, Frank") == olindex.surname("Frank Herbert Jr") == "herbert"


def test_build_index_latest_revision_and_lookups(built):
    s, st, lines = built
    assert (st["rows_author"], st["rows_work"], st["rows_edition"]) == (3, 3, 2)
    assert st["built_at"] and st["delta_versions"] == {"authors": 1, "works": 1, "editions": 1}
    assert any("schema" in m and "isbn13:list<" in m for m in lines)
    idx = olindex.OLIndex(s.ol_index_path)
    hit = idx.by_isbn("0441013597")                                  # ISBN-10 -> 13
    assert hit["title"] == "Dune" and hit["author"] == "Frank Herbert"   # latest revisions
    assert hit["work_key"] == "OL10W" and hit["edition_key"] == "OL100M"
    assert idx.by_isbn("9780000000002") is None                       # old revision's ISBN
    amn = idx.by_isbn("1503071278")
    assert amn["author"] == "Jo Doe"                                  # via the work's authors
    t = idx.by_title("Ancillary Justice (Imperial Radch #1)", "Leckie, Ann")
    assert t["work_key"] == "OL12W" and t["author"] == "Ann Leckie" and t["confidence"] == 1.0
    assert idx.by_title("Something Else Entirely", "Ann Leckie") is None
    # payload_json never read, read-only open
    with pytest.raises(sqlite3.OperationalError):
        idx.c.execute("DELETE FROM author")


def test_build_skips_when_unchanged_and_resumes(settings, monkeypatch):
    make_delta(settings.ol_delta_dir)
    real, calls = olindex._rows, {"n": 0}

    def boom(table, batch):
        calls["n"] += 1
        if calls["n"] == 4:
            raise RuntimeError("killed")
        yield from real(table, batch)
    monkeypatch.setattr(olindex, "_rows", boom)
    with pytest.raises(RuntimeError):
        olindex.build_index(settings.ol_delta_dir, settings.ol_index_path, log=lambda m: None)
    part = settings.ol_index_path.with_name("openlibrary.sqlite.building")
    assert part.exists() and not settings.ol_index_path.exists()
    st = olindex.stats(settings.ol_index_path)
    assert st["building"]["files_done"] == {"authors": 2, "works": 1}
    monkeypatch.setattr(olindex, "_rows", real)
    lines = []
    st = olindex.build_index(settings.ol_delta_dir, settings.ol_index_path, log=lines.append)
    assert any("resuming" in m for m in lines)
    assert not any("authors: file" in m for m in lines)              # done files skipped
    assert st["rows_edition"] == 2 and not part.exists()
    lines = []
    olindex.build_index(settings.ol_delta_dir, settings.ol_index_path, log=lines.append)
    assert "nothing to do" in lines[-1]


def test_scores_use_the_index(built, tmp_path):
    s, _, _ = built
    look = olindex.make_ref_lookup(s)
    src = tmp_path / "drop" / "1503071278.epub"
    junk = {"id": 1, "source_path": str(src), "media_kind": "ebook",
            "metadata": {"embedded": {"title": "1503071278"}, "reference": None,
                         "classification": {"kind": "ebook"}}}
    ev = bookmeta.evaluate(junk, look)
    assert (ev["confidence"], ev["title"], ev["author"]) == (0.97, "Amnesia", "Jo Doe")
    assert ev["isbn"] == "9781503071278" and ev["openlibrary"]["edition_key"] == "OL101M"
    emb = {"id": 2, "source_path": str(tmp_path / "x.epub"), "media_kind": "ebook",
           "metadata": {"embedded": {"title": "Ancillary Justice", "author": "Leckie, Ann"},
                        "reference": None, "classification": {}}}
    ev = bookmeta.evaluate(emb, look)
    assert ev["confidence"] == 0.95 and ev["author"] == "Ann Leckie"
    assert "Open Library OL12W" in ev["score_reason"]
    assert bookmeta.evaluate(emb)["confidence"] == 0.70                 # no index -> as before


def test_autoapply_dry_run_with_index(built, tmp_path):
    from aibrarian.calibre import Calibre
    from test_booklib import BookDB, FakeRunner, fake_lib, put, sug

    s, _, _ = built
    fake_lib(s.calibre_dir, [])
    cal = Calibre(s.calibre_dir, s.report_dir / ".calibre.lock", "calibredb",
                  FakeRunner(s.calibre_dir), backoff_s=0)
    a = put(s.media_root / "book_drop" / "Dune.epub", b"dune")
    sugs = [sug(1, a, title="Dune", author="Frank Herbert", isbn="9780441013593", ref=False)]
    res = booklib.autoapply(sugs, s, BookDB(sugs), calibre=cal)
    it = res["items"][0]
    assert it["confidence"] == 0.97 and it["passes"] and it["decision"] == "calibre_add"
    assert "--identifier openlibrary:OL100M" in it["calibredb_cmd"]


def test_cli_and_api_stats(built, ctx, monkeypatch, capsys):
    s, _, _ = built
    from aibrarian import cli
    monkeypatch.setattr(cli, "get_settings", lambda: s)
    assert cli_main(["--plain-logs", "openlibrary", "stats"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["rows_isbn"] == 2 and out["rows_tkey"] >= 3
    assert cli_main(["--plain-logs", "openlibrary", "lookup", "--isbn", "0441013597"]) == 0
    assert json.loads(capsys.readouterr().out)["work_key"] == "OL10W"
    ctx.settings = s
    r = TestClient(create_app(ctx)).get("/openlibrary/stats").json()
    assert r["index"]["rows_edition"] == 2 and r["index"]["built_at"]
