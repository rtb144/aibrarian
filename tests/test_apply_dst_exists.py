import json

from aibrarian import fileops, quarantine, services
from test_suggestions_apply import setup


def _prep(ctx, settings, dst_bytes=b"v"):
    src, dst, m, refreshed = setup(ctx, settings)
    dst.parent.mkdir(parents=True)
    dst.write_bytes(dst_bytes)
    actions = []
    ctx.db.record_dedup_action = actions.append
    ctx.db.get_media_by_path = lambda p: next(
        (dict(x) for x in ctx.db.base.media.values() if x["path"] == p), None)
    q = []
    ctx.db.quarantine_media = lambda *a: q.append(a)
    restored = []
    ctx.db.restore_media = lambda *a: restored.append(a)
    return src, dst, m, actions, q, restored


def test_dry_run_would_quarantine_identical(ctx, settings):
    src, dst, _, actions, _, _ = _prep(ctx, settings)
    out = services.apply_suggestion(ctx, 1)
    assert out["status"] == "would_quarantine:dst_exists"
    qi = out["quarantine"]
    assert qi["result"] == "would_quarantine:dst_exists" and qi["comparison"] == "identical"
    assert qi["src_sha256"] == qi["dst_sha256"] and qi["src_size"] == qi["dst_size"] == 1
    assert qi["sidecars"] and src.exists() and dst.read_bytes() == b"v" and not actions
    assert ctx.db.sug[1]["status"] == "open"


def test_dry_run_size_only_for_big_equal(ctx, settings, monkeypatch):
    _prep(ctx, settings)
    monkeypatch.setattr(quarantine, "DRY_RUN_HASH_LIMIT", 0)
    qi = services.apply_suggestion(ctx, 1)["quarantine"]
    assert qi["comparison"] == "same_size_unhashed" and qi["src_sha256"] is None and qi["note"]


def test_apply_quarantines_source_and_restore_undoes(ctx, settings):
    src, dst, m, actions, q, restored = _prep(ctx, settings, b"bigger")
    out = services.apply_suggestion(ctx, 1, apply=True)
    assert out["status"] == "quarantined_dst_exists"
    assert ctx.db.sug[1]["status"] == "quarantined_dst_exists"
    qi = out["quarantine"]
    assert qi["comparison"] == "different" and qi["larger"] == "dst"
    assert qi["src_sha256"] and qi["dst_sha256"]
    assert not src.exists() and not src.with_suffix(".en.srt").exists()
    assert dst.read_bytes() == b"bigger"  # destination untouched
    qp = quarantine.quarantine_root(settings)
    man = next(qp.glob("*/manifest.jsonl"))
    rec = json.loads(man.read_text())
    assert rec["reason"] == "dst_exists" and rec["dst_size"] == 6 and rec["suggestion_id"] == 1
    assert len(rec["sidecars_moved"]) == 1 and q and q[0][0] == m["id"]
    assert actions[0]["action"] == "quarantine" and actions[0]["reason"].startswith(
        "dst_exists:different")
    assert (qp / "dedup-actions.log").exists()
    r = quarantine.restore(man, execute=True, db=ctx.db)
    assert r["restored"] == 1 and src.exists() and src.with_suffix(".en.srt").exists()
    assert ctx.db.sug[1]["status"] == "open" and restored


def test_cleanup_skipped_still_works(tmp_path):
    a, b = tmp_path / "a.mkv", tmp_path / "b.mkv"
    a.write_bytes(b"x")
    b.write_bytes(b"x")
    recs = [{"result": "skip_dst_exists", "src": str(a), "dst": str(b)},
            {"result": "quarantined_dst_exists", "src": str(a), "dst": str(b)}]
    res = fileops.delete_skipped_duplicates(recs, apply=False)
    assert [r.result for r in res] == ["dry_run"] and a.exists()
