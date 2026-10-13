"""Consistency check / fix-plan / safe fixer + change_log helper (no Postgres needed)."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aibrarian import changelog, consistency
from aibrarian.api import create_app


def touch(p: Path, data: bytes = b"x") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def row(i, path, **kw):
    return {"id": i, "path": str(path), "root": "01-movies", "kind": "video",
            "size_bytes": kw.pop("size", 1), "fingerprint": kw.pop("fp", f"fp{i}"),
            "missing": kw.pop("missing", False), "stage": kw.pop("stage", "cataloged"),
            "title": None, "locked_by": None, "has_transcript": kw.pop("ht", True), **kw}


@pytest.fixture
def lib(tmp_path):
    m = tmp_path / "media" / "01-movies"
    T = ".transcript.json"
    f = {}
    # linked
    f["iron"] = touch(m / "Iron Man 2 (2010)" / "Iron Man 2 (2010).mkv")
    touch(m / "Iron Man 2 (2010)" / f"Iron Man 2 (2010){T}")
    # moved outside the catalog: discover made a new row, transcript on the old missing row
    f["mad"] = touch(m / "Madagascar (2005)" / "Madagascar (2005).mkv")
    touch(m / "Madagascar (2005)" / f"Madagascar (2005){T}")
    # transcript kept the scene-release stem after the video was renamed
    f["sof"] = touch(m / "Sound of Freedom (2023)" / "Sound of Freedom (2023).mkv")
    f["sof_t"] = touch(m / "Sound of Freedom (2023)" / f"Sound.of.Freedom.2023{T}")
    touch(m / "Sound of Freedom (2023)" / "Sound.of.Freedom.2023.srt")
    # multi-part
    f["u1"] = touch(m / "Underground (1995)" / "Underground (1995) cd1.avi")
    touch(m / "Underground (1995)" / "Underground (1995) cd2.avi")
    touch(m / "Underground (1995)" / f"Underground_E.Kusturica(1995).cd1{T}")
    # orphan: video moved away, transcript left in a nested release folder
    odir = m / "One Battle After Another (2025)" / "Dark.Waters.2019.MULTi.1080p"
    f["dw_t"] = touch(odir / f"Dark Waters (2019){T}")
    f["dw_new"] = touch(m / "Dark Waters (2019)" / "Dark Waters (2019).mkv")
    # orphan with no trace
    touch(m / "Lost (1999)" / f"Lost (1999){T}")
    # on disk, no row
    f["ice"] = touch(m / "Ice Age (2002)" / "Ice Age (2002).mkv")
    touch(m / "Ice Age (2002)" / f"Ice Age (2002){T}")
    # stale row path: file renamed on disk, row still at the old path
    f["pir"] = touch(m / "Pirates (2003)" / "Pirates (2003).mkv", b"12345")
    touch(m / "Pirates (2003)" / f"Pirates (2003){T}")
    old_pir = m / "old" / "Pirates (2003).mkv"
    snap = {
        "media": [row(1, f["iron"]),
                  row(2, f["mad"], ht=False, fp="F", stage="discovered"),
                  row(3, m / "dl" / "Madagascar.2005.mkv", missing=True, fp="F"),
                  row(4, f["sof"], ht=False), row(5, f["u1"]),
                  row(6, f["dw_new"]),
                  row(7, old_pir, size=5, missing=True),
                  row(8, m / "Gone (2001)" / "Gone (2001).mkv")],
        "transcript_lang": [
            {"id": 1, "path": str(f["sof_t"]), "media_id": None},
            {"id": 2, "path": str(f["dw_t"]), "media_id": None},
            {"id": 3, "path": str(m / "nowhere" / f"x{T}"), "media_id": None}],
        "suggestions": [{"id": 1, "domain": "video", "source_path": str(m / "gone.mkv"),
                         "status": "open"}],
        "file_ops": [{"id": 1, "op": "move", "src": str(odir / "Dark Waters (2019).mkv"),
                      "dst": str(f["dw_new"]), "result": "ok"}],
        "changes": []}
    return tmp_path, m, f, snap


def test_classifies_observed_patterns(lib):
    tmp, m, f, snap = lib
    rep = consistency.check(snap, [m], pending=[{"id": 9, "ts": "t", "op": "move"}])
    c = rep["counts"]
    assert rep["linked"] == 1 and rep["transcripts_on_disk"] == 8
    assert c["row_not_transcribed"] == 1 and c["stranded_transcript_twin"] == 1
    sm = {Path(i["transcript"]).name: i for i in rep["issues"]["stem_mismatch"]}
    assert sm["Sound.of.Freedom.2023.transcript.json"]["relink_to"].endswith(
        "Sound of Freedom (2023).transcript.json")
    assert sm["Underground_E.Kusturica(1995).cd1.transcript.json"]["video"].endswith("cd1.avi")
    assert [Path(i["transcript"]).name for i in rep["issues"]["orphan_video_moved"]] == [
        "Dark Waters (2019).transcript.json"]
    assert c["orphan"] == 1 and c["needs_scan"] == 1
    st = rep["issues"]["stale_row_path"][0]
    assert st["media_id"] == 7 and st["matched_by"] == "size+name"
    assert c["row_path_missing"] == 1  # row 8 (rows 3, 7 are flagged missing)
    assert {i["path"] for i in rep["issues"]["video_no_row"]} >= {str(f["ice"]), str(f["pir"])}
    assert c["transcript_lang_missing"] == 1 and c["suggestion_source_missing"] == 1
    assert c["pending_stale"] == 1
    assert rep["no_media_breakdown"] == {"stem_mismatch": 1, "orphan_video_moved": 1,
                                         "transcript_missing": 1}
    text = consistency.format_summary(rep, plan=True)
    assert "[safe]   relink_transcript" in text and "[manual] orphan" in text


def test_fix_plan_is_dry_run_and_fixer_only_safe(lib, ctx):
    tmp, m, f, snap = lib
    rep = consistency.check(snap, [m])
    before = sorted(p.name for p in m.rglob("*"))
    plan = consistency.fix_plan(rep)
    assert {p["action"] for p in plan if p["safe"]} == {"relink_transcript", "update_row_path"}
    assert sorted(p.name for p in m.rglob("*")) == before  # nothing touched

    calls = []

    class DB:
        def get_media(self, i):
            return next(({"id": r["id"], "path": r["path"]} for r in snap["media"]
                         if r["id"] == i), None)

        def get_media_by_path(self, p):
            return None

        def update_path(self, o, n):
            calls.append((o, n))
            return 7

    ctx.db = DB()
    dry = consistency.execute_fixes(ctx, rep)
    assert dry["done"] == 0 and sorted(p.name for p in m.rglob("*")) == before
    res = consistency.execute_fixes(ctx, rep, execute=True)
    assert res["done"] == 3
    d = m / "Sound of Freedom (2023)"
    assert (d / "Sound of Freedom (2023).transcript.json").is_file()
    assert (d / "Sound of Freedom (2023).srt").is_file() and not f["sof_t"].exists()
    assert (str(m / "old" / "Pirates (2003).mkv"), str(f["pir"])) in calls
    assert f["dw_t"].exists()  # orphans are never touched
    assert len(list(m.rglob("*"))) == len(before)  # renamed only, nothing deleted


def test_change_rolls_back_fs_on_error_and_logs(tmp_path):
    class DB:
        def __init__(self):
            self.rows, self.finished = [], []

        def change_begin(self, r):
            self.rows.append(r)
            return len(self.rows)

        def change_finish(self, cid, status, error=None, entity_id=None, repoint=(), **kw):
            if status == "committed" and repoint and repoint[0][1].endswith("boom"):
                raise RuntimeError("db down")
            self.finished.append((cid, status, error, list(repoint)))
            return entity_id

    db = DB()
    a, b = touch(tmp_path / "a.mkv"), tmp_path / "b.mkv"
    with changelog.change(db, "move", "media_file", old_path=a, new_path=b, actor="t") as ch:
        a.rename(b)
        ch.add_undo(lambda: b.rename(a))
        ch.repoint(a, b)
    assert db.rows[0]["status"] == "pending" and db.finished[-1][1] == "committed"
    assert db.finished[-1][3] == [(str(a), str(b))] and b.exists()
    # exception inside the block: FS undone, status rolled_back
    with pytest.raises(ValueError), changelog.change(db, "move", "file", old_path=b,
                                                     new_path=a, actor="t") as ch:
        b.rename(a)
        ch.add_undo(lambda: a.rename(b))
        raise ValueError("x")
    assert b.exists() and db.finished[-1][1] == "rolled_back"
    # DB commit fails after the FS op: FS undone too
    c = tmp_path / "boom"
    with pytest.raises(RuntimeError), changelog.change(db, "rename", "file", old_path=b,
                                                       new_path=c, actor="t") as ch:
        b.rename(c)
        ch.add_undo(lambda: c.rename(b))
        ch.repoint(b, c)
    assert b.exists() and not c.exists() and db.finished[-1][1] == "rolled_back"
    # refusal
    with changelog.change(db, "move", "file", old_path=b, new_path=a, actor="t") as ch:
        ch.fail("skip_dst_exists")
    assert db.finished[-1][1:3] == ("failed", "skip_dst_exists")


def test_change_busy_when_locked_or_leased(tmp_path):
    from contextlib import contextmanager
    from datetime import UTC, datetime

    class DB:
        held = True

        @contextmanager
        def path_locks(self, keys, mids):
            yield not self.held

        def get_media(self, i):
            return {"id": i, "locked_by": "w", "locked_at": datetime.now(UTC)}

    db = DB()
    with pytest.raises(changelog.Busy), changelog.change(db, "move", "file", old_path="/a"):
        pass
    db.held = False
    with pytest.raises(changelog.Busy), changelog.change(db, "move", "media_file",
                                                         old_path="/a", entity_id=3):
        pass


def test_consistency_endpoints_and_gauges(ctx):
    from aibrarian import metrics

    rep = consistency.check({"media": []}, [ctx.settings.media_root])
    rep["counts"]["orphan"] = 4
    consistency.write_report(rep, ctx.settings.report_dir)
    cl = TestClient(create_app(ctx, migrate=False))
    j = cl.get("/consistency").json()
    assert j["counts"]["orphan"] == 4 and "issues" not in j
    assert "Change log" in cl.get("/changes/ui").text
    metrics.refresh_consistency_gauges(rep)
    metrics.refresh_consistency_gauges(rep)  # idempotent
    body = cl.get("/metrics").text
    assert 'aib_consistency_issues{issue="orphan"} 4.0' in body
    json.dumps(rep)


def test_consistency_flow_registered():
    from aibrarian import flows

    deps = {d.name: d for d in flows.deployments("cpu")}
    assert deps["consistency"].schedules and deps["consistency"].concurrency_limit
