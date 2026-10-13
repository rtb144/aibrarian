import json
from pathlib import Path
from types import SimpleNamespace

from aibrarian import srtpair
from aibrarian.quarantine import restore

EN = "1\n00:00:01,000 --> 00:00:02,000\nWhere are you going tonight, my friend? " \
     "I think we should leave before the rain starts and the roads get bad.\n"
FR = "1\n00:00:01,000 --> 00:00:02,000\nOù vas-tu ce soir, mon ami ? Je pense que " \
     "nous devrions partir avant que la pluie commence et que les routes soient mauvaises.\n"


def w(p: Path, text="x"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def settings(tmp):
    return SimpleNamespace(media_root=tmp, quarantine_root=lambda: tmp / "_q")


def nodetect(_):
    return None, 0.0


def test_parse_hint():
    h = srtpair.parse_hint
    assert h(Path("/m/B/Bambi (1942).eng.0.srt"))["lang"] == "eng"
    t = h(Path("/m/T/Transporter.2.2005.1080p.BrRip.x264.YIFY+HI.eng.srt"))
    assert t == {"lang": "eng", "sdh": True, "forced": False}
    assert h(Path("/m/X/Subs/3_English.srt"))["lang"] == "eng"
    assert h(Path("/m/X/Subs/French.forced.srt")) == {"lang": "fre", "sdh": False,
                                                       "forced": True}
    assert h(Path("/m/X/Movie.2000.spa.srt"))["lang"] == "spa"
    assert h(Path("/m/X/Lethal Weapon 2 1989 1080p BluRay HEVC x265 5.1 BONE.srt"))["lang"] \
        is None
    assert srtpair.kept_langs("en, French") == ["eng", "fre"]


def test_pair_dups_suffix_quarantine_and_restore(tmp_path, monkeypatch):
    root = tmp_path / "01-movies"
    d = root / "Bambi (1942)"
    w(d / "Bambi (1942).mkv")
    w(d / "Bambi (1942) sample.mkv")
    w(d / "Bambi (1942).eng.srt", EN)               # already paired
    w(d / "Bambi.1942.1080p.eng.0.srt", EN)         # identical -> dup
    w(d / "Bambi.1942.1080p.eng.1.srt", EN + "more\n")  # different -> .1
    w(d / "Subs" / "2_English.srt", EN + "x\n")     # different -> .2
    w(d / "Subs" / "3_English.srt", EN + "yy\n")    # cap reached -> quarantine
    w(d / "Subs" / "4_French.srt", FR)              # other lang
    w(d / "Subs" / "5_English_SDH.srt", EN + "[music]\n")  # sdh slot
    w(d / "Subs" / "6.srt", FR)                     # detected french
    p = srtpair.plan(root, ["eng"])
    s = p["summary"]
    assert s["dirs"] == 1 and s["paired"] == {"eng": 2, "eng.sdh": 1}
    assert s["dup_quarantined"] == 1 and s["slot_full_quarantined"] == 1
    assert s["other_lang_quarantined"] == {"fre": 2}
    dsts = sorted(Path(a["dst"]).name for a in p["dirs"][0]["actions"] if a.get("dst"))
    assert dsts == ["Bambi (1942).eng.1.srt", "Bambi (1942).eng.2.srt",
                    "Bambi (1942).eng.sdh.srt"]
    assert "Bambi (1942).eng.1.srt" not in {x.name for x in d.iterdir()}  # dry run
    res = srtpair.execute(p, settings(tmp_path))
    assert res["renamed"] == 3 and res["quarantined"] == 4 and res["errors"] == 0
    assert not (d / "Subs").exists()  # emptied -> rmdir
    assert (d / "Bambi (1942).eng.srt").read_text() == EN  # never overwritten
    assert sorted(x.name for x in d.iterdir() if x.suffix == ".srt") == [
        "Bambi (1942).eng.1.srt", "Bambi (1942).eng.2.srt", "Bambi (1942).eng.sdh.srt",
        "Bambi (1942).eng.srt"]
    # re-run is a no-op
    assert srtpair.plan(root, ["eng"])["summary"]["dirs"] == 0
    # undo both manifests
    r1 = restore(Path(res["rename_manifest"]), execute=True)
    r2 = restore(Path(res["manifest"]), execute=True)
    assert r1["restored"] == 3 and r2["restored"] == 4
    assert (d / "Subs" / "4_French.srt").read_text() == FR
    assert (d / "Bambi.1942.1080p.eng.1.srt").exists()


def test_scene_name_and_text_detection(tmp_path):
    root = tmp_path / "m"
    d = root / "Lethal Weapon 2 (1989)"
    w(d / "Lethal Weapon 2 (1989).mkv")
    w(d / "Lethal Weapon 2 1989 1080p BluRay HEVC x265 5.1 BONE.srt", EN)
    p = srtpair.plan(root, ["eng"])
    a = p["dirs"][0]["actions"][0]
    assert a["lang"] == "eng" and a["lang_from"].startswith("text:")
    assert Path(a["dst"]).name == "Lethal Weapon 2 (1989).eng.srt"


def test_no_video_and_ambiguous(tmp_path):
    root = tmp_path / "m"
    w(root / "Pressure (2026) [1080p] WEB" / "Pressure (2026).mkv")
    w(root / "pressure (2026)" / "pressure.eng.srt", EN)
    w(root / "Two (2000)" / "a.mkv")
    w(root / "Two (2000)" / "b.mp4")
    w(root / "Two (2000)" / "x.eng.srt", EN)
    p = srtpair.plan(root, ["eng"], detector=nodetect)
    st = {d["name"]: d for d in p["dirs"]}
    assert st["Two (2000)"]["status"] == "ambiguous"
    nv = st["pressure (2026)"]
    assert nv["status"] == "no_video" and "Pressure (2026) [1080p] WEB" in nv["hint"]
    assert p["summary"]["no_video_dirs"] == 1 and p["summary"]["ambiguous_dirs"] == 1
    res = srtpair.execute(p, settings(tmp_path))
    assert res["quarantined"] == 0 and (root / "pressure (2026)" / "pressure.eng.srt").exists()
    p2 = srtpair.plan(root, ["eng"], include_no_video=True, detector=nodetect)
    res = srtpair.execute(p2, settings(tmp_path))
    assert res["quarantined"] == 1
    rec = json.loads(Path(res["manifest"]).read_text().splitlines()[0])
    assert rec["reason"] == "srt_pair:no_video"
    assert "srt-pair" in srtpair.format_plan(p2, res)


def test_never_overwrites_at_execute(tmp_path):
    root = tmp_path / "m"
    d = root / "A (2000)"
    w(d / "A (2000).mkv")
    w(d / "A.2000.eng.srt", EN)
    p = srtpair.plan(root, ["eng"])
    w(d / "A (2000).eng.srt", "appeared meanwhile")
    res = srtpair.execute(p, settings(tmp_path))
    assert res["skipped"] == 1 and (d / "A (2000).eng.srt").read_text() == "appeared meanwhile"
