from fastapi.testclient import TestClient

from aibrarian import services
from aibrarian.api import create_app
from aibrarian.db import is_noop


class SugDB:
    """FakeDB + suggestion table."""

    def __init__(self, base):
        self.base, self.sug, self.events, self.paths = base, {}, [], []

    def __getattr__(self, name):
        return getattr(self.base, name)

    def get_suggestion(self, sid):
        return dict(self.sug[sid]) if sid in self.sug else None

    def set_suggestion_status(self, sid, status):
        self.sug[sid]["status"] = status

    def list_suggestions(self, domain, status="open", include_noop=False):
        return [dict(s) for s in self.sug.values()
                if (s["status"] == status or (include_noop and s["status"] == "noop"))
                and (include_noop or not is_noop(s["source_path"], s["target_path"], s["op"]))]

    def update_path(self, old, new):
        self.paths.append((old, new))
        for m in self.base.media.values():
            if m["path"] == old:
                m["path"] = new
                return m["id"]
        return None

    def record_event(self, mid, action, stage, outcome, started, details=None, **kw):
        self.events.append((mid, action, stage, outcome, details))


def setup(ctx, settings):
    root = settings.media_root
    src = root / "downloads" / "Alien.1979.mkv"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"v")
    src.with_suffix(".en.srt").write_text("1")
    dst = root / "01-movies" / "Alien (1979)" / "Alien (1979).mkv"
    db = SugDB(ctx.db)
    ctx.db = db
    m = db.base.add(path=str(src), stage="cataloged")
    mk = {"domain": "video", "op": "move", "confidence": 0.95, "decision": "suggest",
          "status": "open", "media_kind": "movie"}
    db.sug = {1: mk | {"id": 1, "source_path": str(src), "target_path": str(dst)},
              2: mk | {"id": 2, "source_path": "/m/x.mkv", "target_path": "/m/x.mkv"},
              3: mk | {"id": 3, "source_path": "/m/y.mkv", "target_path": None, "op": "none"}}
    refreshed = []
    ctx.jellyfin.refresh_library = lambda: refreshed.append(1) or True
    return src, dst, m, refreshed


def test_noop_detection():
    assert is_noop("/a", "/a", "move") and is_noop("/a", None, "move") and is_noop("/a", "/b", "none")
    assert not is_noop("/a", "/b", "rename")


def test_list_hides_noops(ctx, settings):
    setup(ctx, settings)
    c = TestClient(create_app(ctx, migrate=False))
    assert [r["id"] for r in c.get("/suggestions").json()] == [1]
    assert {r["id"] for r in c.get("/suggestions", params={"include_noop": True}).json()} == {1, 2, 3}
    rows = services.suggestion_rows(ctx)
    assert rows[0]["id"] == 1 and set(rows[0]) >= {"source_path", "target_path", "confidence",
                                                    "decision"}


def test_apply_dry_run_then_apply(ctx, settings):
    src, dst, m, refreshed = setup(ctx, settings)
    plan = services.apply_suggestion(ctx, 1)
    assert plan["status"] == "planned" and src.exists() and not dst.exists() and not refreshed
    out = services.apply_suggestion(ctx, 1, apply=True)
    assert out["status"] == "applied" and dst.exists() and not src.exists()
    assert dst.with_suffix(".en.srt").exists()  # sidecar carried
    assert ctx.db.base.media[m["id"]]["path"] == str(dst) and out["media_id"] == m["id"]
    assert ctx.db.events[-1][1] == "apply_suggestion" and ctx.db.events[-1][4]["sidecars"] == 1
    assert refreshed == [1] and ctx.db.sug[1]["status"] == "applied"
    assert services.apply_suggestion(ctx, 1, apply=True)["status"] == "not_open:applied"


def test_apply_existing_destination_quarantines_source(ctx, settings):
    src, dst, _, refreshed = setup(ctx, settings)
    dst.parent.mkdir(parents=True)
    dst.write_bytes(b"other")
    assert services.apply_suggestion(ctx, 1)["status"] == "would_quarantine:dst_exists"
    out = services.apply_suggestion(ctx, 1, apply=True)
    assert out["status"] == "quarantined_dst_exists" and not src.exists()
    assert dst.read_bytes() == b"other"
    assert ctx.db.sug[1]["status"] == "quarantined_dst_exists" and not refreshed
    assert not ctx.db.paths


def test_noop_apply_and_api_reject(ctx, settings):
    setup(ctx, settings)
    assert services.apply_suggestion(ctx, 2, apply=True)["status"] == "nothing_to_do"
    c = TestClient(create_app(ctx, migrate=False))
    assert c.post("/suggestions/1/apply").json()["status"] == "planned"  # dry-run default
    assert c.post("/suggestions/1/reject").json()["status"] == "dismissed"
    assert c.post("/suggestions/99/reject").status_code == 404
    assert c.post("/suggestions/1/apply", params={"dry_run": False}).json()["status"] == "not_open:dismissed"
