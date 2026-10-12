import json
from pathlib import Path

from fastapi.testclient import TestClient

from aibrarian import booklib
from aibrarian.api import create_app


class BookDB:
    """Suggestions + library_action + dedup_action in memory."""

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


def sug(i, src, dst, conf=0.95, **meta):
    return {"id": i, "domain": "book", "source_path": str(src),
            "target_path": str(dst) if dst else None, "confidence": conf,
            "op": "move" if dst else "none", "status": "open",
            "metadata": {"classification": {"title": meta.get("title", "Dune"),
                                            "author": meta.get("author", "Frank Herbert"),
                                            "series": meta.get("series")},
                         "embedded": {"series_index": meta.get("idx")}}}


def setup(settings):
    m = settings.media_root
    drop = m / "book_drop"
    lib = settings.calibre_dir
    lib.mkdir(parents=True)
    a = put(drop / "Dune Folder" / "dune.epub", b"dune-bytes")
    put(drop / "Dune Folder" / "dune.opf", b"<opf/>")
    put(drop / "Dune Folder" / "cover.jpg", b"img")
    b = put(drop / "low.epub", b"low")
    c = put(drop / "dup" / "copy.epub", b"same")
    put(lib / "Ann" / "Same" / "copy.epub", b"same")
    d = put(drop / "clash.epub", b"new")
    put(lib / "Bob" / "Clash" / "clash.epub", b"old")
    sugs = [sug(1, a, lib / "Frank Herbert" / "Dune" / "dune.epub", 0.95, series="Dune", idx="1"),
            sug(2, b, lib / "X" / "Low" / "low.epub", 0.89),
            sug(3, c, lib / "Ann" / "Same" / "copy.epub", 0.97, title="Same", author="Ann"),
            sug(4, d, lib / "Bob" / "Clash" / "clash.epub", 0.99, title="Clash", author="Bob"),
            sug(5, drop / "gone.epub", lib / "g.epub", 0.99),
            sug(6, b, Path("/etc/evil.epub"), 0.99)]
    return a, b, c, d, sugs


def test_dry_run_shows_confidence_and_changes_nothing(settings):
    a, b, c, d, sugs = setup(settings)
    db = BookDB(sugs)
    lines = []
    res = booklib.autoapply(sugs, settings, db, progress=lines.append)
    dec = {i["suggestion_id"]: i["decision"] for i in res["items"]}
    assert dec == {1: "apply", 2: "needs_review", 3: "quarantine", 4: "needs_review",
                   5: "skip", 6: "needs_review"}
    assert res["threshold"] == 0.90 and a.exists() and not db.actions
    assert all(s["status"] == "open" for s in db.sugs.values())
    assert "0.89 below 0.90 -> needs_review" in "\n".join(lines)
    out = booklib.format_result(res)
    assert "DRY RUN" in out and "\t0.95\tyes\tapply\tDune" in out and "\t0.89\tno\t" in out
    assert booklib.autoapply(sugs, settings, db, limit=2)["considered"] == 2


def test_execute_logs_and_undo_run(settings):
    a, b, c, d, sugs = setup(settings)
    db = BookDB(sugs)
    res = booklib.autoapply(sugs, settings, db, execute=True, run_id="r1")
    lib = settings.calibre_dir
    new = lib / "Frank Herbert" / "Dune" / "dune.epub"
    assert new.exists() and (new.parent / "dune.opf").exists() and (new.parent / "cover.jpg").exists()
    assert not a.parent.exists()                                     # emptied folder removed
    assert b.exists() and db.sugs[2]["status"] == "needs_review"
    assert not c.exists() and db.sugs[3]["status"] == "quarantined"   # identical -> quarantine
    assert (lib / "Ann" / "Same" / "copy.epub").exists()
    assert d.exists() and (lib / "Bob/Clash/clash.epub").read_bytes() == b"old"  # never overwrite
    assert db.sugs[4]["status"] == "needs_review" and db.sugs[1]["status"] == "applied"
    acts = [x["action"] for x in db.actions]
    assert acts.count("move") == 1 and "quarantine" in acts and acts.count("needs_review") == 3
    assert "folder_removed" in acts and "skip" in acts
    mv = next(x for x in db.actions if x["action"] == "move")
    assert (mv["title"], mv["author"], mv["series"], mv["series_index"]) == (
        "Dune", "Frank Herbert", "Dune", "1")
    logf = settings.report_dir / booklib.LOG_FILE
    assert len(logf.read_text().splitlines()) == len(db.actions)
    assert res["counts"]["move"] == 1

    dry = booklib.undo(db.get_library_actions(run_id="r1"), settings, db)
    assert dry["dry_run"] and new.exists()
    u = booklib.undo(db.get_library_actions(run_id="r1"), settings, db, execute=True)
    assert u["undone"] == 2 and a.exists() and c.exists() and not new.exists()
    assert (a.parent / "cover.jpg").exists() and (a.parent / "dune.opf").exists()
    assert db.sugs[1]["status"] == "undone" and db.sugs[3]["status"] == "undone"
    again = booklib.undo(db.get_library_actions(run_id="r1"), settings, db, execute=True)
    assert again["undone"] == 0                                      # idempotent
    assert any(x["action"] == "undo" for x in db.actions)
    assert json.loads(logf.read_text().splitlines()[-1])["action"] == "undo"


def test_cross_filesystem_is_needs_review(settings, monkeypatch):
    a, b, c, d, sugs = setup(settings)
    real = Path.stat

    def fake(self, *args, **kw):
        st = real(self, *args, **kw)
        if str(self).startswith(str(settings.calibre_dir)):
            class S:
                pass
            s2 = S()
            for k in ("st_size", "st_mtime", "st_mode", "st_ino"):
                setattr(s2, k, getattr(st, k))
            s2.st_dev = st.st_dev + 1
            return s2
        return st
    monkeypatch.setattr(Path, "stat", fake)
    it = booklib.assess(sugs[0], settings)
    assert it["decision"] == "needs_review" and "filesystem" in it["reason"]


def test_library_log_api_and_ui(ctx, settings):
    a, b, c, d, sugs = setup(settings)
    db = BookDB(sugs)
    booklib.autoapply(sugs, settings, db, execute=True, run_id="r1")
    ctx.db = db
    cl = TestClient(create_app(ctx, migrate=False))
    r = cl.get("/library-log", params={"action": "move"}).json()
    assert r["total"] == 1 and r["summary"]["today"]["move"] == 1
    assert r["summary"]["threshold"] == 0.9
    assert cl.get("/library-log", params={"author": "ann"}).json()["total"] >= 1
    assert "Herbert" in " ".join(cl.get("/library-log/facets", params={"field": "author"}).json())
    html = cl.get("/library-log/ui").text
    assert "Library log (read-only)" in html and "books undo --id" in html
    assert "library-log/ui" in cl.get("/dashboard").text
    assert "library-log/ui" in cl.get("/dedup/ui").text


def test_book_duplicate_groups(settings):
    from aibrarian import duplicates as dmod
    from aibrarian import quarantine as q

    lib_copy = put(settings.calibre_dir / "A" / "T" / "t.epub", b"x" * 50)
    drop_copy = put(settings.media_root / "downloads" / "t.epub", b"x" * 50)
    put(settings.audiobooks_dir / "B" / "t2.m4b", b"x" * 50)        # same bytes, both in libs
    rows = dmod.book_rows([settings.calibre_dir, settings.audiobooks_dir,
                           settings.media_root / "downloads"])
    groups = dmod.book_groups(rows, dmod.HashCache(), settings.media_root)
    assert len(groups) == 1 and groups[0]["count"] == 3
    assert not Path(groups[0]["suggested_keep"]).is_relative_to(settings.media_root)
    p = q.plan({"identical": [], "identical_books": groups}, settings)
    assert [i["path"] for i in p["items"]] == [str(drop_copy)]       # only media_root copies move
    assert p["items"][0]["group"] == q.BOOK_GROUP_BASE and lib_copy.exists()
