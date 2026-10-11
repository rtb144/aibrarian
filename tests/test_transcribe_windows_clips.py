from pathlib import Path

from aibrarian.clips import best_quote_match, best_word_match, build_ffmpeg_cmd, extract_clip
from aibrarian.eta import estimate, fmt
from aibrarian.search import rrf_fuse
from aibrarian.transcribe import (
    RawSegment,
    TranscriptResult,
    chunk_segments,
    result_from_dict,
    result_to_dict,
)
from aibrarian.windows import make_windows, merge_hits, window_point_id


def raw(n, step=10.0):
    return [RawSegment(i * step, i * step + step, f"line {i}", [(i * step, i * step + 1, f"w{i}")])
            for i in range(n)]


def test_chunk_segments_respects_target():
    chunks = chunk_segments(raw(10), 30.0)
    assert [c.seq for c in chunks] == list(range(len(chunks)))
    assert all(c.end - c.start <= 30.0 for c in chunks)
    assert chunks[0].text == "line 0 line 1 line 2"
    assert chunks[0].words[0] == (0.0, 1.0, "w0")
    assert chunk_segments([], 30) == []


def test_result_dict_roundtrip():
    r = TranscriptResult(raw(2), "en", 20.0)
    back = result_from_dict(result_to_dict(r, "fp", "m"))
    assert back.language == "en" and back.segments[1].words == [(10.0, 11.0, "w1")]


def segs(n):
    return [{"id": i, "media_id": 7, "seq": i, "start_s": i * 10.0, "end_s": i * 10.0 + 10,
             "text": f"text {i}", "words": None} for i in range(n)]


def test_make_windows_covers_tail():
    w = make_windows(segs(12), size=5, stride=5)
    assert w[-1].last_seq == 11
    assert w[0].first_seq == 0 and w[0].last_seq == 4
    assert make_windows(segs(3), 5, 2)[0].last_seq == 2
    assert window_point_id(3, 4) == 3_000_004


def test_merge_hits():
    hits = [{"score": 0.5, "payload": {"movie_id": 1, "start": 0, "end": 50}},
            {"score": 0.9, "payload": {"movie_id": 1, "start": 40, "end": 90}},
            {"score": 0.7, "payload": {"movie_id": 2, "start": 0, "end": 10}}]
    m = merge_hits(hits)
    assert m[0] == {"movie_id": 1, "start": 0, "end": 90, "score": 0.9, "title": None, "path": None}
    assert len(m) == 2


def test_quote_match_across_boundary():
    s = segs(3)
    s[1]["text"] = "I'll be"
    s[2]["text"] = "back soon"
    m = best_quote_match("I'll be back", s, 85)
    assert m and m.start == 10.0 and m.end == 30.0
    assert best_quote_match("completely unrelated words here", s, 95) is None


def test_word_match_index():
    s = segs(1)
    s[0]["words"] = [[0, 1, "hello"], [1, 2, "there"], [5, 6, "hello"], [6, 7, "there!"]]
    first = best_word_match("hello there", s, 85, 0)
    second = best_word_match("hello there", s, 85, 1)
    assert first and second and {first.start, second.start} == {0.0, 5.0}
    assert best_word_match("hello there", s, 85, 2) is None
    assert best_word_match("x", segs(1), 85) is None  # no word timings


def test_ffmpeg_cmd_and_extract(tmp_path):
    cmd = build_ffmpeg_cmd(Path("/in.mkv"), Path("/out.mp4"), 10, 20)
    assert cmd[cmd.index("-ss") + 1] == "10.000" and cmd[cmd.index("-t") + 1] == "10.000"
    calls = []
    out, s, e = extract_clip(Path("/in.mkv"), tmp_path, "My Movie!", 5, 10, 30, 25,
                             runner=lambda c, **k: calls.append(c))
    assert (s, e) == (0.0, 25) and out.name == "My_Movie_0-25.mp4" and calls


def test_rrf():
    sc = rrf_fuse([[1, 2, 3], [3, 1]])
    assert max(sc, key=sc.get) in (1, 3) and sc[2] < sc[1]


def test_eta():
    from datetime import datetime, timedelta
    t0 = datetime(2026, 1, 1)
    times = [t0 + timedelta(minutes=10 * i) for i in range(6)] + [t0 + timedelta(days=3)]
    e = estimate(times, 10)
    assert e["median_gap_s"] == 600 and e["eta_s"] == 6000  # downtime gap ignored
    assert estimate([], 0)["eta_s"] == 0 and estimate([], 5)["eta_s"] is None
    assert fmt(90061) == "1d 1h 1m"
