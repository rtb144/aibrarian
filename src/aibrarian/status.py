"""Media pipeline state machine (pure logic, no I/O).

Happy path for a video::

    discovered -> probed -> audio_extracted -> transcribed -> embedded -> cataloged

Terminal / side states: ``failed`` (retryable, remembers ``failed_stage``),
``skipped`` (nothing to do, e.g. no audio stream) and ``needs_review``
(a human must decide, e.g. low-confidence Jellyfin match).
See docs/status-model.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Stage(StrEnum):
    DISCOVERED = "discovered"
    PROBED = "probed"
    AUDIO_EXTRACTED = "audio_extracted"
    TRANSCRIBED = "transcribed"
    EMBEDDED = "embedded"
    CATALOGED = "cataloged"
    FAILED = "failed"
    SKIPPED = "skipped"
    NEEDS_REVIEW = "needs_review"


PIPELINE: tuple[Stage, ...] = (
    Stage.DISCOVERED, Stage.PROBED, Stage.AUDIO_EXTRACTED,
    Stage.TRANSCRIBED, Stage.EMBEDDED, Stage.CATALOGED,
)
SIDE_STATES = frozenset({Stage.FAILED, Stage.SKIPPED, Stage.NEEDS_REVIEW})


@dataclass(frozen=True)
class ActionSpec:
    name: str
    requires: Stage
    produces: Stage
    resource: str  # concurrency tag used by Prefect: cpu | gpu-whisper | gpu-ollama | io


ACTIONS: dict[str, ActionSpec] = {
    a.name: a
    for a in (
        ActionSpec("probe", Stage.DISCOVERED, Stage.PROBED, "io"),
        ActionSpec("extract_audio", Stage.PROBED, Stage.AUDIO_EXTRACTED, "cpu"),
        ActionSpec("transcribe", Stage.AUDIO_EXTRACTED, Stage.TRANSCRIBED, "gpu-whisper"),
        ActionSpec("embed", Stage.TRANSCRIBED, Stage.EMBEDDED, "gpu-ollama"),
        ActionSpec("catalog", Stage.EMBEDDED, Stage.CATALOGED, "gpu-ollama"),
    )
}

MAX_ATTEMPTS = 3


class InvalidTransition(ValueError):
    pass


def validate_transition(current: Stage | str, target: Stage | str, action: str) -> None:
    """Raise :class:`InvalidTransition` unless ``action`` may move current->target."""
    current, target = Stage(current), Stage(target)
    if action == "discover":
        if target is not Stage.DISCOVERED:
            raise InvalidTransition("discover can only produce 'discovered'")
        return
    if action in ("reset", "retry"):
        if target not in PIPELINE:
            raise InvalidTransition(f"{action} must target a pipeline stage")
        return
    spec = ACTIONS.get(action)
    if spec is None:
        raise InvalidTransition(f"unknown action {action!r}")
    if current is not spec.requires:
        raise InvalidTransition(
            f"{action} requires stage {spec.requires}, item is in {current}")
    if target not in {spec.produces, *SIDE_STATES}:
        raise InvalidTransition(f"{action} cannot move item to {target}")


def next_action(stage: Stage | str) -> ActionSpec | None:
    stage = Stage(stage)
    for spec in ACTIONS.values():
        if spec.requires is stage:
            return spec
    return None


def should_give_up(attempts: int, max_attempts: int = MAX_ATTEMPTS) -> bool:
    return attempts >= max_attempts


def backlog_stages() -> list[Stage]:
    """Stages that still have work to do (everything before cataloged)."""
    return list(PIPELINE[:-1])
