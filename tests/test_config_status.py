import pytest

from aibrarian.config import Settings
from aibrarian.status import ACTIONS, InvalidTransition, Stage, next_action, validate_transition


def test_csv_env_and_legacy_aliases(monkeypatch):
    monkeypatch.setenv("AIB_TRANSCRIBE_ROOTS", "01-movies, 05-instructional_videos")
    monkeypatch.setenv("QDRANT_COLLECTION", "legacy_coll")
    monkeypatch.setenv("BOOK_MIN_CONFIDENCE", "0.9")
    s = Settings(_env_file=None)
    assert s.transcribe_roots == ["01-movies", "05-instructional_videos"]
    assert s.qdrant_collection == "legacy_coll"
    assert s.book_min_confidence == 0.9


def test_aib_prefix_wins_over_legacy(monkeypatch):
    monkeypatch.setenv("OLLAMA_URL", "http://old:1")
    monkeypatch.setenv("AIB_OLLAMA_URL", "http://new:2")
    assert Settings(_env_file=None).ollama_url == "http://new:2"


def test_defaults_match_old_stack():
    s = Settings(_env_file=None)
    assert s.qdrant_collection == "movie_segments"
    assert s.window_collection == "movie_windows"
    assert s.llm_model == "qwen3:14b" and s.whisper_model == "large-v3"
    assert (s.book_min_confidence, s.book_review_confidence) == (0.85, 0.60)
    assert (s.clip_fuzzy_threshold, s.clip_padding_s) == (85, 30.0)


def test_bad_book_mode():
    with pytest.raises(ValueError):
        Settings(_env_file=None, book_mode="yolo")


def test_pipeline_chain_is_linear():
    stage = Stage.DISCOVERED
    seen = []
    while (spec := next_action(stage)) is not None:
        seen.append(spec.name)
        stage = spec.produces
    assert seen == ["probe", "extract_audio", "transcribe", "embed", "catalog"]
    assert stage is Stage.CATALOGED


def test_validate_transition():
    validate_transition("discovered", "probed", "probe")
    validate_transition("probed", "skipped", "extract_audio")
    with pytest.raises(InvalidTransition):
        validate_transition("discovered", "transcribed", "transcribe")
    with pytest.raises(InvalidTransition):
        validate_transition("audio_extracted", "embedded", "transcribe")
    with pytest.raises(InvalidTransition):
        validate_transition("discovered", "probed", "nope")


def test_gpu_resources():
    assert ACTIONS["transcribe"].resource == "gpu-whisper"
    assert ACTIONS["embed"].resource == "gpu-ollama"
