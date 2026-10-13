import csv
import json
import os

from fastapi.testclient import TestClient

from aibrarian import langcheck
from aibrarian.api import create_app

EN = ["Where are you going tonight?", "I told you, I have to see my brother first.",
      "He never listens to anything I say.", "We should leave before the rain starts.",
      "The car is parked behind the old church."]
DE = ["Wohin gehst du heute Abend?", "Ich habe dir gesagt, ich muss zuerst meinen Bruder sehen.",
      "Er hört nie auf das, was ich sage.", "Wir sollten gehen, bevor der Regen anfängt.",
      "Das Auto steht hinter der alten Kirche."]

EN3 = [f"{t} Number {w}." for w in ("one", "two", "three") for t in EN]
DE3 = [f"{t} Nummer {w}." for w in ("eins", "zwei", "drei") for t in DE]


def _sidecar(root, rel, lines, lang):
    v = root / rel
    v.parent.mkdir(parents=True, exist_ok=True)
    v.write_bytes(b"video")
    tp = v.with_suffix(".transcript.json")
    tp.write_text(json.dumps({"version": 1, "language": lang, "segments": [
        {"start": i, "end": i + 1, "text": t} for i, t in enumerate(lines)]}))
    return tp


def test_analyze_flags():
    assert langcheck.analyze(EN, "en")["flags"] == []
    de = langcheck.analyze(DE, "de")
    assert de["detected_lang"] == "de" and de["non_english"] and not de["mismatch"]
    mis = langcheck.analyze(DE, "en")
    assert mis["mismatch"] and "lang_mismatch" in mis["flags"]
    rep = langcheck.analyze(EN[:1] * 30 + EN, "en")
    assert rep["repetitive"] and rep["repeat_ratio"] > 0.5
    assert "near_empty" in langcheck.analyze(["...", "♪"], None)["flags"]
    assert langcheck.norm_lang("zh-CN") == "zh" and langcheck.norm_lang("English") == "en"


def test_run_is_read_only_and_writes_reports(ctx, settings):
    root = settings.media_root / "01-movies"
    en = _sidecar(root, "A (2000)/A (2000).mkv", EN3, "en")
    de = _sidecar(root, "B (2001)/B (2001).mkv", DE3, "de")
    bad = _sidecar(root, "C (2002)/C (2002).mkv", ["Thank you."] * 40, "en")
    before = {p: (p.read_bytes(), os.stat(p).st_mtime_ns) for p in (en, de, bad)}
    ctx.db.transcribed_media_for_langcheck = lambda: [
        {"id": 7, "path": str(de.parent / "B (2001).mkv"), "title": "Das Boot", "language": "de"}]
    saved = []
    ctx.db.save_langcheck = lambda rows: saved.extend(rows) or len(rows)
    res = langcheck.run(ctx)
    s = res["summary"]
    assert s["checked"] == 3 and s["non_english"] == 1 and s["repetitive"] == 1
    assert s["stored"] == 3 and len(saved) == 3
    by = {r["path"]: r for r in res["rows"]}
    assert by[str(de)]["title"] == "Das Boot" and by[str(de)]["media_id"] == 7
    assert by[str(de)]["needs_review"] and not by[str(en)]["needs_review"]
    assert by[str(bad)]["needs_review"]
    data = json.loads((settings.report_dir / "langcheck.json").read_text())
    assert data["summary"]["checked"] == 3
    rows = list(csv.DictReader((settings.report_dir / "langcheck.csv").open()))
    assert rows[0]["needs_review"] == "True" and len(rows) == 3
    assert {p: (p.read_bytes(), os.stat(p).st_mtime_ns) for p in before} == before
    assert "need review" in langcheck.format_summary(res)
    assert langcheck.run(ctx, limit=1)["summary"]["checked"] == 1


def test_run_without_db(ctx, settings, tmp_path):
    _sidecar(settings.media_root / "01-movies", "X/X.mkv", DE, None)
    res = langcheck.run(ctx, out=tmp_path / "o" / "r.json", store=False)
    assert res["rows"][0]["title"] == "X" and res["rows"][0]["detected_lang"] == "de"
    assert (tmp_path / "o" / "r.csv").exists()


class LangDB:
    def __init__(self):
        self.reviews = {}

    def langcheck_list(self, *a):
        return {"total": 1, "counts": {}, "items": [{"id": 1, "path": "p"}], "args": list(a)}

    def langcheck_review(self, item_id, review, note=None):
        if item_id != 1:
            return None
        self.reviews[item_id] = review
        return {"id": 1, "review": review}


def test_api_review(ctx):
    ctx.db = LangDB()
    cl = TestClient(create_app(ctx, migrate=False))
    assert cl.get("/transcripts/lang?non_english=true").json()["args"][:2] == [True, True]
    assert cl.post("/transcripts/lang/1/review", json={"review": "wrong"}).json()["review"] == "wrong"
    assert ctx.db.reviews == {1: "wrong"}
    assert cl.post("/transcripts/lang/1/review", json={"review": "bogus"}).status_code == 422
    assert cl.post("/transcripts/lang/2/review", json={"review": "correct"}).status_code == 404
    assert "Transcript language check" in cl.get("/transcripts/lang/ui").text


def test_cli_parser():
    from aibrarian.cli import build_parser

    a = build_parser().parse_args(["transcripts", "langcheck", "--limit", "5", "--out", "/tmp/x"])
    assert a.tr_cmd == "langcheck" and a.limit == 5
