import json
from pathlib import Path

from aibrarian import requeue_lang
from aibrarian.probe import extract_audio_cmd, media_info, select_audio_stream


def _a(index, lang=None, title=None, ch=2, default=0, **disp):
    tags = {k: v for k, v in (("language", lang), ("title", title)) if v}
    return {"index": index, "codec_type": "audio", "codec_name": "ac3", "channels": ch,
            "tags": tags, "disposition": {"default": default, **disp}}


def _probe(*audio):
    return {"format": {"duration": "100"},
            "streams": [{"index": 0, "codec_type": "video", "codec_name": "h264"}, *audio]}


def sel(*audio):
    return select_audio_stream(media_info(_probe(*audio)))


def test_prefers_english_over_dub_default():
    s = sel(_a(1, "cze", ch=6, default=1), _a(2, "eng", ch=6))
    assert s["index"] == 2 and s["audio_ordinal"] == 1 and s["english"]


def test_english_by_title_and_variants():
    assert sel(_a(1, "hin", default=1), _a(2, None, "English 5.1"))["index"] == 2
    assert sel(_a(1, "spa", default=1), _a(2, "en"))["index"] == 2
    assert sel(_a(1, "fre", default=1), _a(2, "english"))["index"] == 2
    # a tagged non-English track whose title mentions English is not English
    assert not sel(_a(1, "fre", "French (from English)", default=1))["english"]


def test_english_tiebreak_default_then_channels_skip_commentary():
    s = sel(_a(1, "eng", "Commentary", ch=8, default=1), _a(2, "eng", ch=2),
            _a(3, "eng", ch=6), _a(4, "eng", "Audio Description", ch=8),
            _a(5, "eng", "AD", ch=8), _a(6, "eng", ch=8, comment=1))
    assert s["index"] == 3 and s["reason"] == "english"
    s = sel(_a(1, "eng", ch=2, default=1), _a(2, "eng", ch=6))
    assert s["index"] == 1 and s["reason"] == "english_default"
    # only commentary in English: still English (better than a dub)
    assert sel(_a(1, "ger", default=1), _a(2, "eng", "Director's commentary"))["index"] == 2


def test_no_english_falls_back_to_default():
    s = sel(_a(1, "ger"), _a(2, "fre", default=1))
    assert s["index"] == 2 and not s["english"] and s["reason"] == "default"
    assert sel(_a(1), _a(2))["reason"] == "first"


def test_map_uses_audio_ordinal():
    cmd = extract_audio_cmd(Path("/a.mkv"), Path("/o.wav"), audio_ordinal=1)
    assert cmd[cmd.index("-map") + 1] == "0:a:1"


class _DB:
    def __init__(self, cands, media):
        self.cands, self.media, self.events, self.logged = cands, media, [], []

    def langcheck_requeue_candidates(self):
        return self.cands

    def get_media(self, mid):
        return dict(self.media[mid])

    def transition(self, mid, action, frm, to, outcome, started, **kw):
        if self.media[mid]["stage"] != frm:
            return False
        self.media[mid]["stage"] = to
        self.events.append((mid, action, frm, to, kw.get("details")))
        return True

    def update_media_fields(self, mid, **f):
        self.media[mid].update(f)

    def record_dedup_action(self, row):
        self.logged.append(row)


def _setup(ctx, tmp_path):
    root = ctx.settings.transcribe_paths()[0]
    root.mkdir(parents=True)
    vids, cands, media = {}, [], {}
    specs = {  # name: (audio, detected, sidecar)
        "Ant-Man": ([_a(1, "cze", ch=6, default=1), _a(2, "eng", ch=6)], "cs", "legacy"),
        "Allied": ([_a(1, "eng", ch=6, default=1)], "fr", "new_same"),
        "Amelie": ([_a(1, "fre", ch=6, default=1)], "fr", "legacy"),
        "Untagged": ([_a(1, default=1), _a(2)], "es", "legacy"),
    }
    for i, (name, (audio, lang, kind)) in enumerate(specs.items(), 1):
        v = root / f"{name}.mkv"
        v.write_bytes(b"x")
        data = {"segments": [{"start": 0, "end": 1, "text": "hola"}], "language": lang}
        if kind == "new_same":
            data |= {"fingerprint": "fp", "model": "m", "audio_stream_index": 1}
        v.with_suffix(".transcript.json").write_text(json.dumps(data))
        v.with_suffix(".srt").write_text("1\n")
        info = media_info(_probe(*audio))
        media[i] = {"id": i, "path": str(v), "stage": "cataloged", "title": name}
        cands.append({"lang_id": i, "media_id": i, "path": str(v), "title": name,
                      "transcript_path": str(v.with_suffix(".transcript.json")),
                      "detected_lang": lang, "stored_lang": lang, "stage": "cataloged",
                      "media_info": info, "audio_stream_index": 1})
        vids[name] = v
    ctx.db = _DB(cands, media)
    return vids


def test_requeue_dry_run_and_execute(ctx, tmp_path):
    vids = _setup(ctx, tmp_path)
    p = requeue_lang.plan(ctx)
    dec = {i["title"]: i["decision"] for i in p["items"]}
    assert dec == {"Ant-Man": "requeue", "Allied": "same_track", "Amelie": "keep",
                   "Untagged": "untagged"}
    out = requeue_lang.format_plan(p)
    assert "WOULD RE-QUEUE" in out and "KEEP AS-IS" in out and "Amelie" in out
    assert all(v.with_suffix(".transcript.json").exists() for v in vids.values())  # dry run
    assert ctx.db.media[1]["stage"] == "cataloged"

    res = requeue_lang.execute(ctx, p)
    assert res["requeued"] == 1
    am = vids["Ant-Man"]
    assert not am.with_suffix(".transcript.json").exists() and not am.with_suffix(".srt").exists()
    assert ctx.db.media[1]["stage"] == "probed" and ctx.db.media[2]["stage"] == "cataloged"
    assert ctx.db.events[0][1] == "reset" and ctx.db.events[0][4]["new_stream"] == 2
    manifest = Path(res["manifest"])
    recs = [json.loads(x) for x in manifest.read_text().splitlines()]
    assert len(recs) == 2 and all(Path(r["quarantine_path"]).exists() for r in recs)
    assert recs[0]["media_id"] is None and recs[0]["transcript_media_id"] == 1
    assert manifest.parent.parent == ctx.settings.quarantine_root()
    assert vids["Amelie"].with_suffix(".transcript.json").exists()

    p2 = requeue_lang.plan(ctx, include_untagged=True)
    assert {i["title"]: i["decision"] for i in p2["items"]}["Untagged"] == "requeue_untagged"


def test_execute_puts_files_back_when_stage_changed(ctx, tmp_path):
    vids = _setup(ctx, tmp_path)
    p = requeue_lang.plan(ctx)
    ctx.db.transition = lambda *a, **k: False
    res = requeue_lang.execute(ctx, p)
    assert res["requeued"] == 0 and res["results"][0]["reason"] == "stage changed concurrently"
    assert vids["Ant-Man"].with_suffix(".transcript.json").exists()


def test_cli_parses():
    from aibrarian.cli import build_parser

    a = build_parser().parse_args(["transcripts", "requeue-lang", "--execute",
                                   "--include-untagged"])
    assert a.tr_cmd == "requeue-lang" and a.execute and a.include_untagged


def test_sidecar_records_stream():
    from aibrarian.transcribe import TranscriptResult, result_to_dict

    d = result_to_dict(TranscriptResult([], "en", 1.0), "fp", "m", audio_stream_index=2,
                       audio_language="en")
    assert d["audio_stream_index"] == 2 and d["audio_language"] == "en"
    assert "audio_stream_index" not in result_to_dict(TranscriptResult([], "en", 1.0), "fp", "m")
