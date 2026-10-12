import errno
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aibrarian import booklib, bookmeta
from aibrarian.api import create_app
from aibrarian.calibre import Calibre

REAL_CALIBREDB = shutil.which("calibredb") or (
    "/tmp/calibre/calibredb" if Path("/tmp/calibre/calibredb").exists() else None)


# -- metadata + score -------------------------------------------------------------

@pytest.mark.parametrize("raw,exp", [
    ("AMNESIA - The Josephine Chronicles : Book 1 - FBI Series",
     ("Amnesia", "The Josephine Chronicles", 1, "FBI Series")),
    ("Dark Fortune: A Cozy Mystery (Fortunes, Love Book 6)",
     ("Dark Fortune", "Fortunes, Love", 6, "A Cozy Mystery")),
    ("The Way of Kings (The Stormlight Archive #1)", ("The Way of Kings", "The Stormlight Archive", 1, None)),
    ("Star Wars: Thrawn", ("Star Wars: Thrawn", None, None, None)),
    ("Dune", ("Dune", None, None, None)),
])
def test_clean_title(raw, exp):
    c = bookmeta.clean_title(raw)
    assert (c["title"], c["series"], c["series_index"], c["subtitle"]) == exp


def test_score_is_evidence_based():
    emb = {"title": "Dune", "author": "Frank Herbert", "isbn": "9780441013593"}
    assert bookmeta.score(emb, {"title": "Dune", "match_type": "isbn"}, "Dune", "Frank Herbert")[0] == 0.97
    assert bookmeta.score(emb, {"title": "Other", "match_type": "isbn"}, "Dune", "x")[0] == 0.85
    emb2 = {"title": "Dune", "author": "Frank Herbert"}
    assert bookmeta.score(emb2, {"title": "Dune", "author": "Frank Herbert"}, "Dune", "F")[0] == 0.95
    assert bookmeta.score(emb2, None, "Dune", "Frank Herbert")[0] == 0.70
    assert bookmeta.score({}, None, "Some Title", "A")[0] == 0.35
    assert bookmeta.score({"title": "1503071278"}, None, None, "A")[0] == 0.10
    assert bookmeta.score(emb2, {"title": "Dune", "author": "Frank Herbert"}, "Dune", None)[0] == 0.30


# -- fake Calibre ------------------------------------------------------------------

SCHEMA = """CREATE TABLE books(id INTEGER PRIMARY KEY, title TEXT, path TEXT);
CREATE TABLE authors(id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE books_authors_link(id INTEGER PRIMARY KEY, book INT, author INT);
CREATE TABLE identifiers(id INTEGER PRIMARY KEY, book INT, type TEXT, val TEXT);
CREATE TABLE data(id INTEGER PRIMARY KEY, book INT, format TEXT, uncompressed_size INT, name TEXT);"""


def fake_lib(lib: Path, books=()):
    lib.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(lib / "metadata.db") as c:
        c.executescript(SCHEMA)
    for b in books:
        fake_insert(lib, *b)


def fake_insert(lib, title, author, src: Path, isbn=None):
    with sqlite3.connect(lib / "metadata.db") as c:
        bid = (c.execute("SELECT max(id) FROM books").fetchone()[0] or 0) + 1
        rel = f"{author}/{title} ({bid})"
        name = f"{title} - {author}"
        (lib / rel).mkdir(parents=True)
        shutil.copy2(src, lib / rel / f"{name}{src.suffix.lower()}")
        c.execute("INSERT INTO books VALUES (?,?,?)", (bid, title, rel))
        c.execute("INSERT INTO authors(name) VALUES (?)", (author,))
        aid = c.execute("SELECT max(id) FROM authors").fetchone()[0]
        c.execute("INSERT INTO books_authors_link(book, author) VALUES (?,?)", (bid, aid))
        if isbn:
            c.execute("INSERT INTO identifiers(book,type,val) VALUES (?,?,?)", (bid, "isbn", isbn))
        c.execute("INSERT INTO data(book,format,uncompressed_size,name) VALUES (?,?,?,?)",
                  (bid, src.suffix.lstrip(".").upper(), src.stat().st_size, name))
    return bid


class FakeRunner:
    """Pretends to be calibredb against the fake sqlite library."""

    def __init__(self, lib, fail=False, locked_times=0):
        self.lib, self.fail, self.locked, self.calls = lib, fail, locked_times, []

    def __call__(self, cmd):
        self.calls.append(cmd)
        if self.locked:
            self.locked -= 1
            return subprocess.CompletedProcess(cmd, 1, "", "apsw.BusyError: database is locked")
        if self.fail:
            return subprocess.CompletedProcess(cmd, 1, "", "boom: unsupported format")
        op = cmd[3]
        if op == "add":
            a = cmd[4:]
            meta = dict(zip(a[1:-1:2], a[2:-1:2], strict=False))
            bid = fake_insert(self.lib, meta["--title"], meta["--authors"], Path(a[-1]),
                              meta.get("--isbn"))
            return subprocess.CompletedProcess(cmd, 0, f"Added book ids: {bid}\n", "warn\n")
        if op == "add_format":
            bid, src = int(cmd[-2]), Path(cmd[-1])
            with sqlite3.connect(self.lib / "metadata.db") as c:
                rel = c.execute("SELECT path FROM books WHERE id=?", (bid,)).fetchone()[0]
                name = Path(next((self.lib / rel).iterdir())).stem
                shutil.copy2(src, self.lib / rel / f"{name}{src.suffix}")
                c.execute("INSERT INTO data(book,format,uncompressed_size,name) VALUES (?,?,?,?)",
                          (bid, src.suffix.lstrip(".").upper(), src.stat().st_size, name))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if op in ("remove", "remove_format"):
            bid = int(cmd[4])
            with sqlite3.connect(self.lib / "metadata.db") as c:
                c.execute("DELETE FROM data WHERE book=?", (bid,))
                if op == "remove":
                    c.execute("DELETE FROM books WHERE id=?", (bid,))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise AssertionError(cmd)


class BookDB:
    def __init__(self, sugs):
        self.sugs = {s["id"]: s for s in sugs}
        self.actions, self.dedup = [], []

    def set_suggestion_status(self, sid, status):
        self.sugs[sid]["status"] = status

    def list_suggestions(self, domain, status="open", include_noop=False):
        return [s for s in self.sugs.values() if s["status"] == status]

    def record_library_action(self, row):
        row = dict(row, id=len(self.actions) + 1, undone_by=None)
        self.actions.append(row)
        return row["id"]

    def get_library_actions(self, ids=None, run_id=None):
        return [a for a in reversed(self.actions) if a["id"] in (ids or []) or
                (run_id and a["run_id"] == run_id)]

    def mark_library_undone(self, aid, uid):
        self.actions[aid - 1]["undone_by"] = uid

    def record_dedup_action(self, row):
        self.dedup.append(row)

    def library_actions(self, action=None, domain=None, since=None, until=None, title=None,
                        author=None, series=None, run_id=None, limit=50, offset=0):
        rows = [a for a in reversed(self.actions)
                if (not action or a["action"] == action)
                and (not author or author.lower() in (a.get("author") or "").lower())]
        return rows[offset:offset + limit], len(rows)

    def library_action_counts(self, since=None):
        out = {}
        for a in self.actions:
            out[a["action"]] = out.get(a["action"], 0) + 1
        return out

    def library_facet(self, field, q="", limit=20):
        return sorted({a.get(field) for a in self.actions if a.get(field)})


def put(p: Path, data=b"book"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def sug(i, src, kind="ebook", title="Dune", author="Frank Herbert", isbn=None, ref=True,
        target="x"):
    emb = {"title": title, "author": author} | ({"isbn": isbn} if isbn else {})
    return {"id": i, "domain": "book", "source_path": str(src), "target_path": target,
            "confidence": 0.95, "op": "move", "status": "open", "media_kind": kind,
            "metadata": {"embedded": emb,
                         "reference": {"title": title, "author": author} if ref else None,
                         "classification": {"kind": kind, "title": title, "author": author}}}


@pytest.fixture
def env(settings):
    lib = settings.calibre_dir
    drop = settings.media_root / "book_drop"
    settings.audiobooks_dir.mkdir(parents=True)
    old = put(drop / "x" / "old.epub", b"already")
    fake_lib(lib, [("Known", "Ann", old, "111")])
    old.unlink()
    shutil.rmtree(drop / "x")
    runner = FakeRunner(lib)
    cal = Calibre(lib, settings.report_dir / ".calibre.lock", "calibredb", runner, backoff_s=0)
    return settings, lib, drop, runner, cal


def test_dry_run_shows_scores_clean_metadata_and_command(env):
    s, lib, drop, runner, cal = env
    a = put(drop / "f" / "1503071278.epub", b"amnesia")
    b = put(drop / "low.epub", b"low")
    sugs = [sug(1, a, title="AMNESIA - The Josephine Chronicles : Book 1 - FBI Series",
                author="Jo Doe"),
            sug(2, b, title="Low", author="X", ref=False)]
    lines = []
    res = booklib.autoapply(sugs, s, BookDB(sugs), progress=lines.append, calibre=cal)
    i1, i2 = res["items"]
    assert (i1["title"], i1["series"], i1["series_index"], i1["subtitle"]) == (
        "Amnesia", "The Josephine Chronicles", 1, "FBI Series")
    assert i1["decision"] == "calibre_add" and "--series-index 1" in i1["calibredb_cmd"]
    assert "--title Amnesia" in i1["calibredb_cmd"] and "--automerge=ignore" in i1["calibredb_cmd"]
    assert i2["confidence"] == 0.70 and i2["decision"] == "needs_review" and not i2["passes"]
    text = "\n".join(lines)
    assert "0.70 below 0.90 -> needs_review" in text and "calibredb --library-path" in text
    assert not runner.calls and a.exists()                       # dry run: nothing ran
    out = booklib.format_result(res)
    assert "DRY RUN" in out and "The Josephine Chronicles\t1\tFBI Series" in out


def test_execute_calibre_add_quarantines_source_and_logs_output(env):
    s, lib, drop, runner, cal = env
    runner.locked = 2                                            # 'database is locked' twice
    a = put(drop / "Dune dir" / "1503071278.epub", b"dune-bytes")
    put(drop / "Dune dir" / "cover.jpg", b"img")
    sugs = [sug(1, a, isbn="9780441013593")]
    db = BookDB(sugs)
    res = booklib.autoapply(sugs, s, db, execute=True, run_id="r1", calibre=cal)
    assert res["items"][0]["result"] == "calibre_add"
    add = next(x for x in db.actions if x["action"] == "calibre_add")
    cal_log = add["details"]["calibre"]
    assert cal_log["exit_code"] == 0 and cal_log["attempts"] == 3 and "Added book ids" in cal_log["stdout"]
    assert add["details"]["calibre_id"] == 2 and Path(add["new_path"]).exists()
    assert add["details"]["source_quarantine"]["result"] == "moved"
    assert not a.exists() and not a.parent.exists()              # source + junk quarantined
    assert any(x["action"] == "quarantine" for x in db.actions)
    assert db.sugs[1]["status"] == "applied"
    line = json.loads((s.report_dir / booklib.LOG_FILE).read_text().splitlines()[-1])
    assert line["action"] == "calibre_add" and line["details"]["calibre"]["book_ids"] == [2]

    # undo the run: source back, calibredb remove called
    u = booklib.undo(db.get_library_actions(run_id="r1"), s, db, execute=True, calibre=cal)
    assert a.exists() and any(c[3] == "remove" for c in runner.calls) and u["undone"] >= 1
    assert db.sugs[1]["status"] == "undone"


def test_calibre_failure_leaves_source(env):
    s, lib, drop, runner, cal = env
    runner.fail = True
    a = put(drop / "a.epub", b"aaa")
    sugs = [sug(1, a)]
    db = BookDB(sugs)
    booklib.autoapply(sugs, s, db, execute=True, calibre=cal)
    assert a.exists() and db.sugs[1]["status"] == "needs_review"
    err = next(x for x in db.actions if x["action"] == "error")
    assert err["details"]["calibre"]["stderr"].startswith("boom") and "untouched" in err["reason"]


def test_duplicates_against_calibre(env):
    s, lib, drop, runner, cal = env
    exact = put(drop / "same.epub", b"already")                   # same bytes as book 1
    other = put(drop / "known.epub", b"different")               # same isbn, has EPUB
    newfmt = put(drop / "known.pdf", b"pdf-bytes")                # same isbn, lacks PDF
    sugs = [sug(1, exact, title="Whatever", author="Z"),
            sug(2, other, title="Known", author="Ann", isbn="111"),
            sug(3, newfmt, title="Known", author="Ann", isbn="111")]
    db = BookDB(sugs)
    res = booklib.autoapply(sugs, s, db, execute=True, calibre=cal)
    r = {i["suggestion_id"]: (i["decision"], i["result"]) for i in res["items"]}
    assert r == {1: ("quarantine", "quarantine"), 2: ("needs_review", "needs_review"),
                 3: ("add_format", "add_format")}
    assert not exact.exists() and other.exists() and not newfmt.exists()
    assert {f["format"] for f in cal.books()[0]["formats"]} == {"EPUB", "PDF"}


def test_audiobook_move_and_cross_mount_copy(env, monkeypatch):
    s, lib, drop, runner, cal = env
    a = put(drop / "ab" / "part.m4b", b"audio")
    b = put(drop / "ab2" / "other.m4b", b"audio2")
    sugs = [sug(1, a, kind="audiobook", title="Dark Fortune: A Cozy Mystery (Fortunes Book 6)",
                author="Kim"),
            sug(2, b, kind="audiobook", title="Solo", author="Lee")]
    db = BookDB(sugs)
    real = os.rename

    def no_cross(src, dst):
        if "other.m4b" in str(src) and str(dst).startswith(str(s.audiobooks_dir)) \
                and str(src).startswith(str(drop)):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real(src, dst)
    monkeypatch.setattr(booklib.os, "rename", no_cross)
    res = booklib.autoapply(sugs, s, db, execute=True, run_id="r2", calibre=cal)
    t1 = s.audiobooks_dir / "Kim" / "Fortunes" / "6 - Dark Fortune" / "part.m4b"
    t2 = s.audiobooks_dir / "Lee" / "Solo" / "other.m4b"
    assert [i["result"] for i in res["items"]] == ["move", "copy"], [i["reason"] for i in res["items"]]
    assert t1.exists() and not a.exists() and t2.read_bytes() == b"audio2" and not b.exists()
    cp = next(x for x in db.actions if x["action"] == "copy")
    assert cp["details"]["method"] == "copy" and cp["details"]["source_quarantine"]["result"] == "moved"
    monkeypatch.setattr(booklib.os, "rename", real)
    u = booklib.undo(db.get_library_actions(run_id="r2"), s, db, execute=True, calibre=cal)
    assert a.exists() and b.exists() and not t1.exists() and not t2.exists()
    assert u["undone"] >= 2


def test_validate_reports_issues_read_only(env):
    s, lib, drop, runner, cal = env
    src = put(drop / "v.epub", b"vvv")
    bid = fake_insert(lib, "Gone", "Bo", src)
    gone = next(Path(f["path"]) for b in cal.books() if b["id"] == bid for f in b["formats"])
    gone.unlink()
    put(lib / "Stray" / "orphan.epub", b"o")
    before = (lib / "metadata.db").read_bytes()
    db = BookDB([])
    db.record_library_action({"action": "calibre_add", "run_id": "x", "title": "Known",
                              "details": {"calibre_id": 1, "sha256": "0" * 64, "format": "EPUB"}})
    db.record_library_action({"action": "calibre_add", "run_id": "x", "title": "Lost",
                              "details": {"calibre_id": 99, "format": "EPUB"}})
    res = booklib.validate(s, db, calibre=cal)
    iss = res["issues"]
    assert not res["ok"] and res["integrity"] == "ok"
    assert len(iss["missing_files"]) == 1 and iss["orphan_files"][0].endswith("orphan.epub")
    assert iss["missing_logged_book"][0]["book_id"] == 99 and len(iss["sha_mismatch"]) == 1
    assert (lib / "metadata.db").read_bytes() == before            # never writes
    assert db.actions[-1]["action"] == "validate" and "missing_files: 1" in db.actions[-1]["reason"]


def test_library_log_api_ui_and_dashboard(ctx, env):
    s, lib, drop, runner, cal = env
    a = put(drop / "a.epub", b"aaa")
    db = BookDB([sug(1, a)])
    booklib.autoapply(list(db.sugs.values()), s, db, execute=True, run_id="r1", calibre=cal)
    booklib.validate(s, db, calibre=cal)
    ctx.db = db
    cl = TestClient(create_app(ctx, migrate=False))
    r = cl.get("/library-log", params={"action": "calibre_add"}).json()
    assert r["total"] == 1 and r["actions"][0]["details"]["calibre"]["exit_code"] == 0
    assert r["summary"]["validation"]["action"] == "validate"
    assert cl.get("/library-log/validation").json()["action"] == "validate"
    html = cl.get("/library-log/ui").text
    assert "<details>" in html and "Latest Calibre validation" in html
    assert "library-log/validation" in cl.get("/dashboard").text


@pytest.mark.skipif(not REAL_CALIBREDB, reason="calibredb not installed")
def test_real_calibredb_add_duplicate_validate_undo(settings, tmp_path):
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from test_librarians import make_epub

    settings.calibredb_bin = REAL_CALIBREDB
    lib = settings.calibre_dir
    seed = tmp_path / "seed.epub"
    make_epub(seed, title="Seed", author="Someone", isbn="9780000000002")
    subprocess.run([REAL_CALIBREDB, "--library-path", str(lib), "add", str(seed)],
                   capture_output=True, check=True, timeout=300)
    drop = settings.media_root / "book_drop"
    a = drop / "1503071278.epub"
    a.parent.mkdir(parents=True)
    make_epub(a)
    data = a.read_bytes()
    sugs = [sug(1, a, isbn="9780441013593")]
    db = BookDB(sugs)
    res = booklib.autoapply(sugs, settings, db, execute=True, run_id="real")
    assert res["items"][0]["result"] == "calibre_add", res["items"][0]
    add = next(x for x in db.actions if x["action"] == "calibre_add")
    bid = add["details"]["calibre_id"]
    cal = booklib.make_calibre(settings)
    book = next(b for b in cal.books() if b["id"] == bid)
    assert book["title"] == "Dune" and book["authors"] == ["Frank Herbert"]
    assert not a.exists()                                        # source quarantined
    # the same bytes again -> exact duplicate -> quarantined, not added
    a2 = drop / "again.epub"
    a2.write_bytes(data)
    r2 = booklib.autoapply([sug(2, a2)], settings, BookDB([sug(2, a2)]), execute=True)
    assert r2["items"][0]["decision"] == "quarantine" and len(cal.books()) == 2
    v = booklib.validate(settings, db)
    assert v["integrity"] == "ok" and not v["issues"]["missing_logged_file"]
    assert not v["issues"]["sha_mismatch"] and not v["issues"]["orphan_files"], v["issues"]
    assert not v["issues"]["missing_files"]
    u = booklib.undo(db.get_library_actions(run_id="real"), settings, db, execute=True)
    assert a.exists() and u["undone"] >= 1 and bid not in {b["id"] for b in cal.books()}


def test_book_duplicate_groups(settings):
    from aibrarian import duplicates as dmod
    from aibrarian import quarantine as q

    lib_copy = put(settings.calibre_dir / "A" / "T" / "t.epub", b"x" * 50)
    drop_copy = put(settings.media_root / "downloads" / "t.epub", b"x" * 50)
    put(settings.audiobooks_dir / "B" / "t2.m4b", b"x" * 50)
    rows = dmod.book_rows([settings.calibre_dir, settings.audiobooks_dir,
                           settings.media_root / "downloads"])
    groups = dmod.book_groups(rows, dmod.HashCache(), settings.media_root)
    assert len(groups) == 1 and groups[0]["count"] == 3
    p = q.plan({"identical": [], "identical_books": groups}, settings)
    assert [i["path"] for i in p["items"]] == [str(drop_copy)] and lib_copy.exists()
