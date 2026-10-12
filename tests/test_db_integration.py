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
    assert d.migrate() == ["001_init", "002_media_info"]
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
