import pytest
from fastapi.testclient import TestClient

from aibrarian.actions import RetryableError, discover, run_action
from aibrarian.api import create_app
from aibrarian.transcribe import RawSegment, TranscriptResult


class FakeProc:
    def __init__(self, stdout=b""):
        self.stdout = stdout


def ffprobe_runner(audio=True):
    import json

    def run(cmd, **kw):
        if cmd[0] == "ffprobe":
            streams = [{"codec_type": "video"}] + ([{"codec_type": "audio"}] if audio else [])
            return FakeProc(json.dumps({"format": {"duration": "120.5"},
                                        "streams": streams}).encode())
        if cmd[0] == "ffmpeg":
            __import__("pathlib").Path(cmd[-1]).write_bytes(b"RIFF")
            return FakeProc()
        raise AssertionError(cmd)
    return run


class FakeTranscriber:
    model_name = "large-v3"

    def __init__(self):
        self.calls = 0

    def transcribe(self, path):
        self.calls += 1
        return TranscriptResult([RawSegment(0, 5, "hello world", [(0, 1, "hello")]),
                                 RawSegment(5, 10, "second line")], "en", 10.0)


def test_full_pipeline_happy_path(ctx, tmp_path):
    video = ctx.settings.media_root / "01-movies" / "Alien (1979)" / "Alien (1979).mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"v")
    ctx.runner = ffprobe_runner()
    tr = FakeTranscriber()
    ctx.transcriber_factory = lambda: tr
    ctx.ollama.reply = {"kind": "movie", "title": "Alien", "year": 1979, "confidence": 0.95,
                        "notes": "clear"}
    ctx.jellyfin.items = [{"Name": "Alien", "ProductionYear": 1979, "Id": "jf1"}]
    row = ctx.db.add(path=str(video), title="Alien", fingerprint="fp1")
    for action in ["probe", "extract_audio", "transcribe", "embed", "catalog"]:
        assert run_action(ctx, action, row["id"]) == "ok", action
    m = ctx.db.media[row["id"]]
    assert m["stage"] == "cataloged" and m["duration_s"] == 120.5 and m["jellyfin_id"] == "jf1"
    assert [e["action"] for e in ctx.db.events] == ["probe", "extract_audio", "transcribe",
                                                    "embed", "catalog"]
    assert video.with_suffix(".srt").exists()  # sidecar written
    assert not (ctx.settings.work_dir / "audio" / f"{row['id']}.wav").exists()  # temp cleaned
    assert len(ctx.vectors.points) == 1 and len(ctx.windows.points) == 1
    assert ctx.db.suggestions[0].decision == "suggest"
    # idempotent: wrong stage -> noop, no new events
    assert run_action(ctx, "probe", row["id"]) == "noop"
    assert len(ctx.db.events) == 5


def test_sidecar_reuse_skips_whisper(ctx):
    video = ctx.settings.media_root / "01-movies" / "m.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"v")
    ctx.runner = ffprobe_runner()
    tr = FakeTranscriber()
    ctx.transcriber_factory = lambda: tr
    row = ctx.db.add(path=str(video), fingerprint="fp")
    for a in ["probe", "extract_audio", "transcribe"]:
        run_action(ctx, a, row["id"])
    assert tr.calls == 1
    ctx.db.media[row["id"]].update(stage="probed", audio_path=None)
    run_action(ctx, "extract_audio", row["id"])
    assert ctx.db.events[-1]["details"] == {"reused_sidecar": True}
    run_action(ctx, "transcribe", row["id"])
    assert tr.calls == 1 and ctx.db.events[-1]["details"]["reused_sidecar"] is True


def test_no_audio_is_skipped(ctx):
    ctx.runner = ffprobe_runner(audio=False)
    row = ctx.db.add(path="/x.mkv")
    assert run_action(ctx, "probe", row["id"]) == "skipped"
    assert ctx.db.media[row["id"]]["stage"] == "skipped"


def test_retries_then_failed(ctx):
    def boom(cmd, **kw):
        raise OSError("disk on fire")
    ctx.runner = boom
    row = ctx.db.add(path="/x.mkv")
    for _ in range(2):
        with pytest.raises(RetryableError):
            run_action(ctx, "probe", row["id"])
    assert run_action(ctx, "probe", row["id"]) == "failed"
    m = ctx.db.media[row["id"]]
    assert m["stage"] == "failed" and m["failed_stage"] == "discovered"
    assert [e.get("attempt") for e in ctx.db.events] == [1, 2, 3]


def test_low_confidence_needs_review(ctx):
    ctx.ollama.reply = {"kind": "unknown", "confidence": 0.2, "notes": "?"}
    row = ctx.db.add(path=str(ctx.settings.media_root / "01-movies/x.mkv"), stage="embedded")
    assert run_action(ctx, "catalog", row["id"]) == "needs_review"


def test_discover_uses_db(ctx):
    d = ctx.settings.media_root / "05-instructional_videos"
    d.mkdir(parents=True)
    (d / "lesson.mp4").write_bytes(b"1")
    calls = {}

    def upsert(files):
        calls["files"] = files
        return [{"id": 1, "path": str(f.path), "created": True, "reset": False} for f in files]
    ctx.db.upsert_media = upsert
    ctx.db.mark_missing = lambda root, paths: 0
    out = discover(ctx)
    assert out["new"] == 1 and calls["files"][0].root == "05-instructional_videos"


def test_discover_rescan_skips_unchanged_without_hashing(ctx, monkeypatch):
    import aibrarian.media as media

    d = ctx.settings.media_root / "01-movies"
    d.mkdir(parents=True)
    a, b = d / "a.mkv", d / "b.mkv"
    a.write_bytes(b"1")
    b.write_bytes(b"2")
    st = a.stat()
    known = {str(a): (st.st_size, st.st_mtime, "fp-a")}
    ctx.db.known_files = lambda root: known if root == "01-movies" else {}
    hashed = []
    real = media.fingerprint
    monkeypatch.setattr(media, "fingerprint", lambda p, *x: hashed.append(p.name) or real(p, *x))
    upserted = []
    ctx.db.upsert_media = lambda files: upserted.extend(files) or [
        {"id": 1, "path": str(f.path), "created": True, "reset": False} for f in files]
    ctx.db.mark_missing = lambda root, paths: 0
    out = discover(ctx)
    assert hashed == ["b.mkv"]                       # a.mkv not re-read
    assert [f.path.name for f in upserted] == ["b.mkv"]  # only new/changed written
    assert out["unchanged"] == 1 and out["new"] == 1 and out["seen"] == 2


def test_discover_skips_when_locked(ctx):
    from contextlib import contextmanager

    @contextmanager
    def busy(key):
        yield False
    ctx.db.try_lock = busy
    assert discover(ctx)["locked"] == 1


class ApiDB:
    def __init__(self):
        self.row = {"id": 1, "path": "/media/01-movies/A/A.mkv", "title": "A", "stage": "failed"}

    def stage_counts(self):
        return {"cataloged": 3, "discovered": 2, "transcribed": 1, "failed": 1}

    def completion_times(self, action):
        return []

    def get_media(self, mid):
        return dict(self.row) if mid == 1 else None

    def timeline(self, mid):
        return [{"action": "probe", "outcome": "ok"}]

    def list_by_stage(self, stage, limit, offset):
        return [self.row] if stage == "failed" else []

    def list_suggestions(self, domain, status="open"):
        return []

    def retry_failed(self, mid=None):
        return 1


@pytest.fixture
def client(ctx, settings):
    settings.media_root = __import__("pathlib").Path("/media")
    ctx.db = ApiDB()
    return TestClient(create_app(ctx, migrate=False))


def test_status_endpoints(client):
    st = client.get("/status").json()
    assert st["backlog"] == 3 and st["stages"]["cataloged"] == 3
    assert client.get("/status/stages/failed").json()[0]["id"] == 1
    assert client.get("/status/stages/bogus").status_code == 422
    tl = client.get("/media/1/timeline").json()
    assert tl["media"]["rel_path"] == "01-movies/A/A.mkv" and tl["events"][0]["action"] == "probe"
    assert client.get("/media/2/timeline").status_code == 404
    assert client.post("/media/1/retry").json() == {"reset": 1}


def test_metrics_and_dashboard(client):
    body = client.get("/metrics").text
    assert 'aib_media_items{stage="cataloged"} 3.0' in body and "aib_backlog_items 3.0" in body
    assert "aibrarian" in client.get("/dashboard").text


def test_legacy_location_route(client):
    loc = client.get("/movies/1/location").json()
    assert loc["srt_path"] == "/media/01-movies/A/A.srt"
