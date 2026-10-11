import json
import os

from aibrarian.media import classify, fingerprint, parse_name, scan_root
from aibrarian.sidecars import (
    catalog_srt_files,
    load_existing_sidecar,
    related_sidecars,
    split_stem_parts,
    srt_time,
    to_srt,
    write_sidecars,
)


def test_parse_movie():
    m = parse_name("The.Matrix.1999.1080p.BluRay.x264.mkv")
    assert m["title"] == "The Matrix" and m["year"] == 1999 and m["season"] is None


def test_parse_episode():
    m = parse_name("Show.Name.S02E05.720p.WEB-DL.mkv")
    assert (m["title"], m["season"], m["episode"]) == ("Show Name", 2, 5)
    assert parse_name("show 3x07.avi")["episode"] == 7


def test_classify():
    assert classify(__import__("pathlib").Path("a.M2TS")) == "video"
    assert classify(__import__("pathlib").Path("a.m4b")) == "audio"
    assert classify(__import__("pathlib").Path("a.epub")) == "book"


def test_fingerprint_changes_with_content(tmp_path):
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x" * 100)
    a = fingerprint(f)
    f.write_bytes(b"y" * 100)
    os.utime(f, (1, 1))
    assert fingerprint(f) != a


def test_scan_root_tv_title_from_dir(tmp_path):
    d = tmp_path / "02-tvshows" / "Great Show (2010)" / "Season 01"
    d.mkdir(parents=True)
    (d / "s01e01.mkv").write_bytes(b"1")
    (d / ".hidden.mkv").write_bytes(b"1")
    (d / "notes.txt").write_text("x")
    files = scan_root(tmp_path, "02-tvshows", kinds=["video"])
    assert len(files) == 1
    assert files[0].title == "Great Show" and files[0].episode == 1


def test_srt_format():
    assert srt_time(3661.5) == "01:01:01,500"
    out = to_srt([{"start": 0, "end": 1.25, "text": " hi "}])
    assert out.startswith("1\n00:00:00,000 --> 00:00:01,250\nhi")


def test_sidecar_roundtrip_and_reuse(tmp_path):
    v = tmp_path / "m.mkv"
    v.write_bytes(b"v")
    tr = {"fingerprint": "fp", "model": "large-v3", "language": "en",
          "segments": [{"start": 0, "end": 1, "text": "hello", "words": []}]}
    tp, sp = write_sidecars(v, tr)
    assert json.loads(tp.read_text())["language"] == "en" and sp.exists()
    assert load_existing_sidecar(v, "fp", "large-v3") is not None
    assert load_existing_sidecar(v, "other", "large-v3") is None
    assert load_existing_sidecar(v, "fp", "medium") is None
    assert sorted(p.name for p in related_sidecars(v)) == ["m.srt", "m.transcript.json"]


def test_srt_catalog(tmp_path):
    d = tmp_path / "01-movies" / "X"
    d.mkdir(parents=True)
    (d / "X (2000).mkv").write_bytes(b"v")
    (d / "X (2000).en.forced.srt").write_text("1")
    (d / "orphan.srt").write_text("1")
    rows = {r["srt"].split("/")[-1]: r for r in catalog_srt_files(tmp_path)}
    assert rows["X (2000).en.forced.srt"]["paired"] and rows["X (2000).en.forced.srt"]["root"] == "01-movies"
    assert not rows["orphan.srt"]["paired"]
    assert split_stem_parts("Movie (2000).en.forced") == ("Movie (2000)", "en.forced")


def test_legacy_sidecar_accepted_when_newer(tmp_path):
    import os
    import time

    from aibrarian.transcribe import result_from_dict

    v = tmp_path / "old.mkv"
    v.write_bytes(b"v")
    os.utime(v, (time.time() - 100, time.time() - 100))
    legacy = {"language": "en", "segments": [
        {"start": 0, "end": 2, "text": " hi ", "words": [{"start": 0, "end": 1, "word": " hi"}]}]}
    (tmp_path / "old.transcript.json").write_text(json.dumps(legacy))
    got = load_existing_sidecar(v, "fp", "large-v3")
    assert got and got["legacy"]
    assert result_from_dict(got).segments[0].words == [(0.0, 1.0, "hi")]
    assert load_existing_sidecar(v, "fp", "large-v3", accept_legacy=False) is None
