"""Real-Postgres tests for SQL (migrations, state machine, queues, search).

Skipped unless AIB_TEST_DATABASE_URL points at a disposable database, e.g.
``postgresql://postgres@localhost:5432/aibtest`` (CI starts a postgres service).
"""

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aibrarian.db import Database
from aibrarian.models import MediaFile, Segment, Suggestion

DSN = os.getenv("AIB_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="AIB_TEST_DATABASE_URL not set")


@pytest.fixture
def db():
    d = Database(DSN)
    with d.conn() as c:
        c.execute("DROP SCHEMA IF EXISTS aib CASCADE")
        c.commit()
    from aibrarian.db import list_migrations

    assert d.migrate() == [v for v, _ in list_migrations()]
    assert d.migrate() == []  # idempotent
    return d


def mf(path, fp="a", title="Alien", root="01-movies"):
    return MediaFile(path=Path(path), root=root, kind="video", size_bytes=1, mtime=1.0,
                     fingerprint=fp, title=title, year=1979)


def test_upsert_events_and_reset(db):
    r = db.upsert_media([mf("/m/01-movies/a.mkv")])
    assert r[0]["created"] and not r[0]["reset"]
    mid = r[0]["id"]
    assert db.upsert_media([mf("/m/01-movies/a.mkv")])[0] == {
        "id": mid, "path": "/m/01-movies/a.mkv", "created": False, "reset": False}
    now = datetime.now(UTC)
    assert db.transition(mid, "probe", "discovered", "probed", "ok", now)
    assert not db.transition(mid, "probe", "discovered", "probed", "ok", now)  # guarded
    r = db.upsert_media([mf("/m/01-movies/a.mkv", fp="b")])
    assert r[0]["reset"] and db.get_media(mid)["stage"] == "discovered"
    tl = db.timeline(mid)
    assert [(e["action"], e["outcome"]) for e in tl] == [
        ("discover", "ok"), ("probe", "ok"), ("discover", "reset")]
    assert tl[1]["duration_ms"] >= 0


def test_claim_skip_locked_and_failure_retry(db):
    ids = [r["id"] for r in db.upsert_media([mf(f"/m/01-movies/{i}.mkv") for i in range(3)])]
    a = db.claim("discovered", ["01-movies"], 2, "w1")
    b = db.claim("discovered", ["01-movies"], 5, "w2")
    assert len(a) == 2 and len(b) == 1 and not {x["id"] for x in a} & {x["id"] for x in b}
    assert db.claim("discovered", ["02-tvshows"], 5, "w3") == []
    now = datetime.now(UTC)
    db.record_attempt_failure(ids[0], "probe", "discovered", now, "boom", 1, "flow-1", "task-1")
    assert db.get_media(ids[0])["stage_attempts"] == 1
    db.transition(ids[0], "probe", "discovered", "failed", "failed", now, attempt=3, error="boom")
    m = db.get_media(ids[0])
    assert m["failed_stage"] == "discovered" and m["last_error"] == "boom"
    assert db.stage_counts() == {"discovered": 2, "failed": 1}
    assert db.list_by_stage("failed")[0]["id"] == ids[0]
    assert db.retry_failed() == 1 and db.get_media(ids[0])["stage"] == "discovered"
    ev = db.recent_events(10, "failed")
    assert ev[0]["path"].endswith("0.mkv") and ev[-1]["flow_run_id"] == "flow-1"
    assert "probe" in {r["action"] for r in db.action_durations(1)}


def test_segments_search_and_suggestions(db):
    mid = db.upsert_media([mf("/m/01-movies/a.mkv")])[0]["id"]
    ids = db.save_segments(mid, [Segment(seq=0, start=0, end=5, text="in space no one can hear",
                                         words=[(0.0, 1.0, "in")]),
                                 Segment(seq=1, start=5, end=9, text="you scream")],
                           "a", "en", "large-v3", 9.0)
    assert len(ids) == 2
    hits = db.keyword_search("hear you scream", 5)
    assert hits and hits[0]["media_id"] == mid
    assert db.keyword_search("space", 5, media_id=mid + 99) == []
    assert db.segments_for_media(mid)[0]["words"] == [[0.0, 1.0, "in"]]
    assert db.find_media_by_title("alien")[0]["id"] == mid
    run = db.start_run("video", {"x": 1})
    sid = db.save_suggestion(Suggestion(domain="video", source_path="/m/a.mkv",
                                        target_path="/m/b.mkv", confidence=0.9,
                                        decision="suggest", run_id=run, media_kind="movie"))
    assert db.save_suggestion(Suggestion(domain="video", source_path="/m/a.mkv", target_path=None,
                                         confidence=0.5, decision="review")) == sid
    db.finish_run(run, {"processed": 1})
    assert db.latest_run("video")["summary"] == {"processed": 1}
    db.set_suggestion_status(sid, "applied")
    assert db.suggestion_sources("video") == {"/m/a.mkv"}
    db.record_file_op("move", "/a", "/b", "ok", None, sid)
    assert db.file_ops("ok")[0]["dst"] == "/b"
    db.update_media_fields(mid, probe={"duration": 9.0}, duration_s=9.0)
    assert db.get_media(mid)["probe"] == {"duration": 9.0}
    assert db.list_media(kind="video")[0]["id"] == mid


def test_openlibrary_import_and_lookup(db, tmp_path):
    import json

    from aibrarian.openlibrary import import_dump, lookup

    recs = [{"title": "Dune", "isbn_13": ["9780441013593"], "by_statement": "Frank Herbert",
             "publish_date": "1990"},
            {"title": "Dune Messiah", "publish_date": "1969"}]
    dump = tmp_path / "ed.txt"
    dump.write_text("\n".join("\t".join(["/type/edition", f"/books/OL{i}M", "1", "x",
                                         json.dumps(r)]) for i, r in enumerate(recs))
                    + "\n/type/work\tk\t1\tx\t{}\n")
    assert import_dump(dump, DSN, batch_lines=1, log=lambda *_: None) == (2, 1)
    assert import_dump(dump, DSN, log=lambda *_: None) == (2, 1)  # upsert, no dupes
    assert lookup(db, isbn="978-0441013593")[0]["confidence"] == 1.0
    hits = lookup(db, title="dune", author="Herbert")
    assert hits[0]["key"] == "/books/OL0M"


def test_known_files_and_try_lock(db):
    db.upsert_media([mf("/m/01-movies/k.mkv", fp="zz")])
    assert db.known_files("01-movies") == {"/m/01-movies/k.mkv": (1, 1.0, "zz")}
    with db.try_lock(99) as a:
        assert a
        with db.try_lock(99) as b:
            assert not b
    with db.try_lock(99) as c:
        assert c


def test_media_info_columns_and_reprobe_queries(db):
    mid = db.upsert_media([mf("/m/01-movies/r.mkv")])[0]["id"]
    db.transition(mid, "probe", "discovered", "probed", "ok", datetime.now(UTC))
    assert [r["id"] for r in db.reprobe_candidates(10)] == [mid]
    db.update_media_fields(mid, media_info={"audio": []}, audio_layout="5.1",
                           audio_stream_index=2, audio_language="en", probed_at=datetime.now(UTC))
    assert db.reprobe_candidates(10) == []
    assert [r["id"] for r in db.reprobe_candidates(10, only_missing=False)] == [mid]
    db.record_event(mid, "reprobe", "probed", "info", datetime.now(UTC), {"x": 1})
    assert db.timeline(mid)[-1]["outcome"] == "info"
    assert db.get_media(mid)["stage"] == "probed"
    assert db.audio_layout_counts() == {"5.1": 1}
    assert db.list_media(audio_layout="5.1")[0]["audio_language"] == "en"


def test_searchable_titles_only_with_segments(db):
    rows = db.upsert_media([mf("/m/01-movies/s1.mkv"), mf("/m/01-movies/s2.mkv")])
    db.save_segments(rows[0]["id"], [Segment(seq=0, start=0, end=1, text="hi")],
                     "fp", "en", "large-v3", 1.0)
    got = db.searchable_titles()
    assert [r["media_id"] for r in got] == [rows[0]["id"]] and got[0]["title"]


def test_noop_suggestions_hidden(db):
    def sg(src, dst, op="move"):
        return Suggestion(domain="video", source_path=src, target_path=dst, confidence=0.9,
                          decision="suggest", op=op)
    a = db.save_suggestion(sg("/m/a.mkv", "/m/A/a.mkv"))
    b = db.save_suggestion(sg("/m/b.mkv", "/m/b.mkv"))
    assert [r["id"] for r in db.list_suggestions("video")] == [a]
    assert {r["id"] for r in db.list_suggestions(None, include_noop=True)} == {a, b}
    assert db.get_suggestion(b)["status"] == "noop" and "/m/b.mkv" in db.suggestion_sources("video")
    mid = db.upsert_media([mf("/m/a.mkv")])[0]["id"]
    assert db.update_path("/m/a.mkv", "/m/A/a.mkv") == mid and db.update_path("/nope", "/x") is None


def test_file_hash_cache_and_candidates(db):
    db.upsert_media([mf("/m/01-movies/d.mkv")])
    assert db.duplicate_candidates()[0]["path"] == "/m/01-movies/d.mkv"
    assert db.get_file_hash("/p") is None
    db.put_file_hash("/p", 10, 1.5, "s", None)
    db.put_file_hash("/p", 10, 1.5, "s", "h")
    assert db.get_file_hash("/p")["sha256"] == "h"


def test_quarantine_purge_and_dedup_action_log(db):
    mid = db.upsert_media([mf("/m/01-movies/dup.mkv")])[0]["id"]
    now = datetime.now(UTC)
    db.quarantine_media(mid, "/m/01-movies/dup.mkv", "/m/_quarantine/d/01-movies/dup.mkv", now,
                        {"keeper": "/m/01-movies/k.mkv", "group": 1})
    row = db.get_media(mid)
    assert row["stage"] == "skipped" and row["missing"] and row["path"].startswith("/m/_q")
    db.restore_media(mid, row["path"], "/m/01-movies/dup.mkv", "discovered", now)
    assert db.get_media(mid)["path"] == "/m/01-movies/dup.mkv" and not db.get_media(mid)["missing"]
    db.quarantine_media(mid, "/m/01-movies/dup.mkv", "/m/_q/dup.mkv", now, {"keeper": "k"})
    db.purge_media(mid, "/m/_q/dup.mkv", now, {"age_days": 31})
    assert db.get_media(mid)["purged_at"] is not None and db.get_media(mid)["stage"] == "skipped"
    assert [e["action"] for e in db.timeline(mid)][-4:] == [
        "quarantine", "restore", "quarantine", "purged"]

    base = {"file_path": "/m/01-movies/dup.mkv", "size_bytes": 10, "source": "cli"}
    db.record_dedup_action(base | {"action": "quarantine", "ts": "2026-10-12T08:00:00+00:00",
                                   "sidecars_copied": ["/m/k.srt"], "sha256": "ab"})
    db.record_dedup_action(base | {"action": "quarantine", "ts": "2026-10-12T08:00:00+00:00"})
    db.record_dedup_action(base | {"action": "purge", "ts": None})
    rows, total = db.dedup_actions()
    assert total == 2 and rows[0]["action"] == "purge" and rows[1]["sidecars_copied"] == ["/m/k.srt"]
    assert db.dedup_actions(action="quarantine", q="DUP")[1] == 1
    assert db.dedup_actions(since="2030-01-01")[1] == 0
    assert db.dedup_totals()["purge"] == {"files": 1, "bytes": 10}


def test_library_action_log_and_statuses(db):
    from aibrarian.models import Suggestion

    sid = db.save_suggestion(Suggestion(domain="book", source_path="/m/drop/a.epub",
                                        target_path="/books/A/a.epub", confidence=0.95,
                                        decision="suggest"))
    for st in ("needs_review", "quarantined", "undone"):
        db.set_suggestion_status(sid, st)
    assert "/m/drop/a.epub" in db.suggestion_sources("book", include_open=False)
    base = {"run_id": "r1", "domain": "book", "suggestion_id": sid, "title": "Dune",
            "author": "Frank Herbert", "series": "Dune", "series_index": "1", "confidence": 0.95,
            "source": "cli"}
    a = db.record_library_action(base | {"action": "move", "old_path": "/m/drop/a.epub",
                                         "new_path": "/books/A/a.epub",
                                         "details": {"moves": [{"from": "x", "to": "y"}]}})
    db.record_library_action(base | {"action": "needs_review", "title": "Other",
                                     "author": "Ann", "series": None, "run_id": "r2"})
    u = db.record_library_action(base | {"action": "undo", "run_id": "u1"})
    db.mark_library_undone(a, u)
    got = db.get_library_actions([a])
    assert got[0]["undone_by"] == u and got[0]["details"]["moves"][0]["to"] == "y"
    assert [r["action"] for r in db.get_library_actions(run_id="r1")] == ["move"]
    rows, total = db.library_actions(author="herbert")
    assert total == 2 and rows[0]["action"] == "undo"
    assert db.library_actions(series="dune", action="move")[1] == 1
    assert db.library_actions(title="other", domain="book")[1] == 1
    assert db.library_actions(since="2030-01-01")[1] == 0
    assert db.library_action_counts() == {"move": 1, "needs_review": 1, "undo": 1}
    assert db.library_facet("author", "her") == ["Frank Herbert"]
    for act in ("calibre_add", "add_format", "copy", "validate"):
        db.record_library_action({"action": act, "details": {"calibre": {"exit_code": 0}}})
    assert db.library_actions(action="validate")[0][0]["details"]["calibre"]["exit_code"] == 0


def test_change_log_cascade_locks_and_claim(db, tmp_path):
    from aibrarian import changelog

    old, new = "/m/01-movies/dl/Alien.mkv", "/m/01-movies/Alien (1979)/Alien (1979).mkv"
    mid = db.upsert_media([mf(old)])[0]["id"]
    db.save_langcheck([{"path": "/m/01-movies/dl/Alien.transcript.json", "media_id": mid,
                        "flags": [], "source": "sidecar"}])
    db.put_file_hash(old, 1, 1.0, "s", None)
    db.save_suggestion(Suggestion(domain="video", source_path=old, target_path="/x/y.mkv",
                                  confidence=0.5, decision="suggest"))
    with changelog.change(db, "move", "media_file", old_path=old, new_path=new,
                          entity_id=mid, actor="test") as ch:
        with db.path_locks([old]) as got:  # held by the change -> busy for others
            assert got is False
        ch.repoint(old, new)
    assert ch.status == "committed" and ch.entity_id == mid
    assert db.get_media(mid)["path"] == new
    with db.conn() as c:
        assert c.execute("SELECT path FROM aib.transcript_lang").fetchone()["path"] == \
            "/m/01-movies/Alien (1979)/Alien (1979).transcript.json"
        assert c.execute("SELECT path FROM aib.file_hash").fetchone()["path"] == new
        assert c.execute("SELECT source_path FROM aib.suggestion").fetchone()["source_path"] == new
    rows, total = db.changes(op="move", path="Alien (1979)")
    assert total == 1 and rows[0]["status"] == "committed" and rows[0]["actor"] == "test"

    # update_path cascades back, and the per-path lock blocks a stage claim
    assert db.update_path(new, old) == mid
    with db.conn() as c:
        c.execute("UPDATE aib.media_file SET stage='probed' WHERE id=%s", (mid,))
        c.commit()
    with db.path_locks([old], [mid]) as got:
        assert got
        assert db.claim("probed", None, 10, "w1") == []
        with pytest.raises(changelog.Busy), changelog.change(
                db, "move", "media_file", old_path=old, new_path=new, entity_id=mid):
            pass
    assert [r["id"] for r in db.claim("probed", None, 10, "w1")] == [mid]
    # fresh lease -> moves refuse
    with pytest.raises(changelog.Busy), changelog.change(
            db, "move", "media_file", old_path=old, new_path=new, entity_id=mid):
        pass

    # failure inside -> failed; stale pending detection
    with pytest.raises(RuntimeError), changelog.change(db, "rename", "file", old_path="/a",
                                                       new_path="/b", check_lease=False):
        raise RuntimeError("boom")
    assert db.changes(status="failed")[0][0]["error"] == "boom"
    cid = db.change_begin({"txn_id": "t", "op": "move", "entity": "file", "old_path": "/p"})
    with db.conn() as c:
        c.execute("UPDATE aib.change_log SET ts = now() - interval '2 hours' WHERE id=%s",
                  (cid,))
        c.commit()
    assert [r["id"] for r in db.stale_pending_changes(30)] == [cid]
    snap = db.consistency_snapshot()
    assert snap["media"][0]["id"] == mid and snap["changes"]


def test_quarantine_media_cascades_and_commits_change(db):
    mid = db.upsert_media([mf("/m/01-movies/a.mkv")])[0]["id"]
    db.save_langcheck([{"path": "/m/01-movies/a.transcript.json", "media_id": mid,
                        "flags": [], "source": "sidecar"}])
    cid = db.change_begin({"txn_id": "t", "op": "quarantine", "entity": "media_file"})
    db.quarantine_media(mid, "/m/01-movies/a.mkv", "/q/a.mkv", datetime.now(UTC),
                        {"keeper": "/k.mkv"}, change_id=cid)
    assert db.changes(status="committed")[0][0]["id"] == cid
    with db.conn() as c:
        assert c.execute("SELECT path FROM aib.transcript_lang").fetchone()["path"] == \
            "/q/a.transcript.json"
    db.restore_media(mid, "/q/a.mkv", "/m/01-movies/a.mkv", "cataloged", datetime.now(UTC))
    m = db.get_media(mid)
    assert m["path"] == "/m/01-movies/a.mkv" and not m["missing"]
    with db.conn() as c:
        assert c.execute("SELECT path FROM aib.transcript_lang").fetchone()["path"] == \
            "/m/01-movies/a.transcript.json"


def test_book_scan_skip_keys_and_error_status(db):
    from aibrarian.models import Suggestion

    a = db.save_suggestion(Suggestion(domain="book", source_path="/m/drop/a.epub",
                                      target_path="/books/A/a.epub", confidence=0.9,
                                      decision="suggest"))
    db.save_suggestion(Suggestion(domain="book", source_path="/m/drop/b.epub",
                                  target_path="/books/B/b.epub", confidence=0.9,
                                  decision="suggest"))
    db.set_suggestion_status(a, "applied")
    db.save_scan_error("book", "/m/drop/bad.pdf", "timed out after 120s", None)
    db.save_scan_error("book", "/m/drop/bad.pdf", "timed out after 120s", None)  # upsert
    k = db.book_scan_skip_keys()
    assert {"/m/drop/a.epub", "/m/drop/b.epub", "/m/drop/bad.pdf"} <= k["suggested"]
    assert k["applied"] == {"/books/A/a.epub"}
    with db.try_lock(724413) as g1, db.try_lock(724413) as g2:
        assert g1 and not g2
