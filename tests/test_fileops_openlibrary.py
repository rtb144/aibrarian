import json

from aibrarian import fileops
from aibrarian.openlibrary import norm, parse_dump_line, remove_nulls, score_match


def test_move_with_sidecars_dry_then_apply(tmp_path):
    src_dir = tmp_path / "media" / "downloads" / "rel"
    src_dir.mkdir(parents=True)
    v = src_dir / "x.mkv"
    v.write_bytes(b"v")
    (src_dir / "x.en.srt").write_text("s")
    (src_dir / "x.transcript.json").write_text("{}")
    dst = tmp_path / "media" / "01-movies" / "X (2000)" / "X (2000).mkv"
    roots = [tmp_path / "media"]
    dry = fileops.move_with_sidecars(v, dst, apply=False, allowed_roots=roots)
    assert {r.result for r in dry} == {"dry_run"} and len(dry) == 3 and v.exists()
    log = tmp_path / "ops.jsonl"
    res = fileops.move_with_sidecars(v, dst, apply=True, allowed_roots=roots,
                                     audit=fileops.jsonl_audit(log),
                                     cleanup_stop=tmp_path / "media" / "downloads")
    assert all(r.result == "ok" for r in res)
    assert (dst.parent / "X (2000).en.srt").exists()
    assert (dst.parent / "X (2000).transcript.json").exists()
    assert not src_dir.exists()  # empty parent removed
    assert len(log.read_text().splitlines()) == 3


def test_move_guards(tmp_path):
    a = tmp_path / "a.mkv"
    a.write_bytes(b"1")
    b = tmp_path / "b.mkv"
    b.write_bytes(b"2")
    assert fileops.move_with_sidecars(a, b, apply=True, allowed_roots=[tmp_path])[0].result \
        == "skip_dst_exists"
    assert fileops.move_with_sidecars(a, tmp_path.parent / "z.mkv", apply=True,
                                      allowed_roots=[tmp_path])[0].result == "skip_outside_root"
    assert fileops.move_with_sidecars(tmp_path / "nope", b, apply=True,
                                      allowed_roots=[tmp_path])[0].result == "skip_missing"


def test_delete_skipped_duplicates(tmp_path):
    src, dst, dst2 = tmp_path / "s.mkv", tmp_path / "d.mkv", tmp_path / "d2.mkv"
    src.write_bytes(b"same")
    dst.write_bytes(b"same")
    other = tmp_path / "o.mkv"
    other.write_bytes(b"x")
    dst2.write_bytes(b"different")
    recs = [{"result": "skip_dst_exists", "src": "/media/s.mkv", "dst": "/media/d.mkv"},
            {"result": "skip_dst_exists", "src": str(other), "dst": str(dst2)},
            {"result": "ok", "src": "a", "dst": "b"}]
    maps = fileops.parse_path_maps([f"/media={tmp_path}"])
    dry = fileops.delete_skipped_duplicates(recs, apply=False, path_maps=maps)
    assert [r.result for r in dry] == ["dry_run", "error"]
    fileops.delete_skipped_duplicates(recs, apply=True, path_maps=maps)
    assert not src.exists() and other.exists()


def test_successor_chain_and_subtitle_sync(tmp_path):
    a, b, c = (tmp_path / n for n in ("a.mkv", "b.mkv", "c.mkv"))
    c.write_bytes(b"v")
    (tmp_path / "a.srt").write_text("sub")
    recs = [{"op": "rename", "src": str(a), "dst": str(b), "result": "ok"},
            {"op": "move", "src": str(b), "dst": str(c), "result": "ok"}]
    assert fileops.build_successor_map(recs)[str(a)] == str(c)
    rep = fileops.subtitle_report(tmp_path, recs)
    assert rep["lost_after_move"][0]["media"] == str(c)
    res = fileops.sync_orphan_subtitles(recs, apply=True)
    assert res[0].result == "ok" and (tmp_path / "c.srt").read_text() == "sub"


def test_legacy_records():
    rows = [{"source": "/a", "dest": "/b", "status": "moved"}, {"nothing": 1}]
    assert fileops.legacy_records(rows) == [{"op": "move", "src": "/a", "dst": "/b",
                                            "result": "ok"}]


def test_openlibrary_parse():
    rec = {"title": "Dune", "subtitle": "Book\x00 One", "isbn_13": ["9780441013593"],
           "publish_date": "Aug 1990", "authors": [{"key": "/authors/OL1A"}],
           "publishers": ["Ace"]}
    line = "\t".join(["/type/edition", "/books/OL1M", "3", "2020", json.dumps(rec)])
    row = parse_dump_line(line.encode())
    assert row[0] == "/books/OL1M" and row[1] == "Dune: Book One" and row[5] == "9780441013593"
    assert row[6] == 1990 and row[2] == "dune book one"
    assert parse_dump_line("/type/work\tk\t1\t2\t{}") is None
    assert parse_dump_line("/type/edition\tk\t1\t2\tnot json") is None
    assert remove_nulls({"a\x00": ["b\x00"]}) == {"a": ["b"]}
    assert norm("Ça  va!") == "ca va"
    assert score_match("Dune", "Herbert", {"title_norm": "dune", "authors": ["Frank Herbert"]}) > 0.9
