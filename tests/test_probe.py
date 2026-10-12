from pathlib import Path

from aibrarian.probe import (
    extract_audio_cmd,
    layout_label,
    llm_view,
    media_info,
    select_audio_stream,
)

PROBE = {
    "format": {"format_name": "matroska,webm", "duration": "7200.5", "bit_rate": "20000000",
               "tags": {"title": "Blade Runner"}},
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "hevc", "profile": "Main 10",
         "width": 3840, "height": 2160, "pix_fmt": "yuv420p10le", "r_frame_rate": "24000/1001",
         "avg_frame_rate": "24000/1001", "color_space": "bt2020nc", "color_transfer": "smpte2084",
         "color_primaries": "bt2020", "color_range": "tv",
         "side_data_list": [{"side_data_type": "DOVI configuration record", "dv_profile": 8,
                             "dv_level": 6, "rpu_present_flag": 1},
                            {"side_data_type": "Mastering display metadata",
                             "max_luminance": "1000/1"}]},
        {"index": 1, "codec_type": "audio", "codec_name": "truehd", "channels": 8,
         "channel_layout": "7.1", "sample_rate": "48000", "tags": {"language": "ger"},
         "disposition": {"default": 1}},
        {"index": 2, "codec_type": "audio", "codec_name": "ac3", "channels": 6,
         "channel_layout": "5.1(side)", "sample_rate": "48000", "bit_rate": "640000",
         "tags": {"language": "eng", "title": "Surround"}, "disposition": {"default": 0}},
        {"index": 3, "codec_type": "audio", "codec_name": "aac", "channels": 2,
         "tags": {"language": "eng", "title": "Commentary"}, "disposition": {"comment": 1}},
    ] + [{"index": 4 + i, "codec_type": "subtitle", "codec_name": "subrip",
          "tags": {"language": "eng" if i == 0 else f"l{i}", "title": "SDH" if i == 0 else None},
          "disposition": {"forced": int(i == 1), "hearing_impaired": int(i == 0)}}
         for i in range(10)],
    "chapters": [{"start_time": "0.000", "end_time": "300.5", "tags": {"title": "Opening"}}],
}


def test_layout_labels():
    assert layout_label("5.1(side)", 6) == "5.1"
    assert layout_label("stereo", 2) == "stereo"
    assert layout_label("7.1(wide)", 8) == "7.1"
    assert layout_label(None, 1) == "mono"
    assert layout_label("", 6) == "5.1"
    assert layout_label("weird", 3) == "2.1"
    assert layout_label(None, 12) == "12ch"
    assert layout_label(None, None) is None


def test_media_info_full():
    info = media_info(PROBE)
    v = info["video"][0]
    assert v["r_frame_rate"] == 23.976 and v["pix_fmt"] == "yuv420p10le" and v["profile"] == "Main 10"
    assert v["hdr_format"] == "dolby_vision" and v["dolby_vision"]["dv_profile"] == 8
    assert v["color_transfer"] == "smpte2084" and "mastering_display" in v
    assert len(info["subtitles"]) == 10  # no cap that drops streams
    sub = info["subtitles"][0]
    assert sub["hearing_impaired"] and sub["title"] == "SDH" and info["subtitles"][1]["forced"]
    a = info["audio"][1]
    assert (a["index"], a["channels"], a["channel_layout"], a["layout"], a["sample_rate"],
            a["bit_rate"], a["language"], a["title"]) == (2, 6, "5.1(side)", "5.1", 48000,
                                                         640000, "eng", "Surround")
    assert info["chapters"] == [{"start": 0.0, "end": 300.5, "title": "Opening"}]
    assert info["container"]["tags"]["title"] == "Blade Runner"
    assert info["audio_layout"] == "5.1"  # layout of the selected (English) track


def test_llm_view_small():
    view = llm_view(media_info(PROBE))
    assert view["audio"] == ["ger:truehd:7.1", "eng:ac3:5.1", "eng:aac:stereo"]
    assert view["subtitles"][0] == "eng" and view["chapters"] == 1
    assert len(str(view)) < 600


def test_select_audio_stream_rules():
    info = media_info(PROBE)
    assert select_audio_stream(info)["index"] == 2  # first English (no English default)
    info["audio"][2]["default"] = True  # commentary marked default (english_default)
    assert select_audio_stream(info)["reason"] == "english_default"
    info["audio"][2]["default"] = False
    info["audio"][1]["default"] = True
    assert select_audio_stream(info)["index"] == 2
    no_eng = {"audio": [{"index": 1, "language": "ger"}, {"index": 5, "language": "fre",
                                                         "default": True}]}
    sel = select_audio_stream(no_eng)
    assert sel["index"] == 5 and not sel["english"] and sel["reason"] == "default"
    assert select_audio_stream({"audio": [{"index": 7, "language": None}]})["reason"] == "first"
    assert select_audio_stream({"audio": []}) is None


def test_extract_audio_maps_stream():
    cmd = extract_audio_cmd(Path("/a.mkv"), Path("/o.wav"), 3)
    assert cmd[cmd.index("-map") + 1] == "0:3" and cmd.index("-map") > cmd.index("-i")
    assert "-map" not in extract_audio_cmd(Path("/a.mkv"), Path("/o.wav"))


def test_ffprobe_error_includes_stderr():
    import subprocess

    import pytest

    from aibrarian.probe import ToolError, run_ffprobe

    def runner(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd, b"", b"moov atom not found\nInvalid data\n")

    with pytest.raises(ToolError, match="ffprobe exit 1: moov atom not found | Invalid data"):
        run_ffprobe(Path("/x.mp4"), runner=runner)


def test_load_wav_avoids_pyav(tmp_path, monkeypatch):
    """Regression: faster-whisper decode_audio passes metadata_errors to av.open,
    which newer PyAV rejects. We must hand it a decoded array instead of a path."""
    import struct
    import sys
    import wave

    import numpy as np

    from aibrarian.transcribe import WhisperTranscriber, load_wav

    p = tmp_path / "a.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<3h", 0, 16384, -32768))
    arr = load_wav(p)
    assert arr.dtype == np.float32 and arr.tolist() == [0.0, 0.5, -1.0]
    monkeypatch.setitem(sys.modules, "av", None)  # any PyAV use would raise ImportError

    seen = {}

    class Model:
        def transcribe(self, audio, **kw):
            seen["audio"] = audio
            return iter([]), type("I", (), {"language": "en", "duration": 0.0})()

    t = WhisperTranscriber("large-v3", "cpu")
    t._model = Model()
    assert t.transcribe(p, language="en").language == "en"
    assert isinstance(seen["audio"], np.ndarray)
