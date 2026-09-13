"""The contract every ASR backend implements.

A backend answers two questions the runner needs before it starts: can it take
a whole media file, and does it return word-level times. Everything else about
scheduling, offsetting and writing artifacts belongs to the runner, so adding a
backend means writing one class and one registry entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, Sequence


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float
    # True when the time was interpolated rather than measured. A long run of
    # these means the aligner collapsed, which verify reports as a warning.
    repaired: bool = False

    def shifted(self, offset: float) -> "Word":
        return Word(
            self.text, round(self.start + offset, 6), round(self.end + offset, 6), self.repaired
        )

    def as_dict(self) -> dict[str, Any]:
        row = {"text": self.text, "start": self.start, "end": self.end}
        if self.repaired:
            row["repaired"] = True
        return row


@dataclass(frozen=True)
class Window:
    """One unit of audio handed to the backend."""

    index: int
    path: Path
    offset: float
    duration: float


@dataclass(frozen=True)
class Capabilities:
    name: str
    # True when the backend cannot accept a whole file and the runner must cut
    # the audio first. MAI takes the whole file; the local Qwen model has a
    # context limit and needs 45-second pieces.
    needs_segmentation: bool
    # Seconds per piece when the runner cuts. Ignored when it does not.
    segment_seconds: int | None
    # Concurrent windows the backend tolerates. A cloud endpoint takes several;
    # a single local GPU takes one.
    max_parallel: int
    # False when the backend returns text only and a forced aligner has to run
    # afterwards to recover word times.
    returns_word_times: bool
    # Human-readable statement of what must be installed or configured.
    requirements: str
    # True when cutting the audio costs accuracy at the seams, so the runner
    # only splits a whole-file backend when asked explicitly.
    prefers_whole_file: bool = True
    # Longest single request the backend will take. Above this the runner cuts
    # even though the backend would rather it did not, because a refused or
    # timed-out request loses the whole job and a seam loses a word.
    max_whole_file_seconds: float | None = None
    # Codecs the backend takes as they are, mapped to the container extension
    # to copy them into. A track already in one of these is handed over without
    # being decoded and re-encoded, which is both smaller and one generation
    # of loss fewer. Empty means everything is converted to 16 kHz mono WAV.
    accepts_codecs: tuple[tuple[str, str], ...] = ()
    # Above this, copying is the wrong call even when the codec is accepted:
    # a lossless multichannel track is far larger than the mono downmix.
    max_upload_bytes: int | None = None
    extras: tuple[str, ...] = field(default_factory=tuple)


class Backend(Protocol):
    capabilities: Capabilities

    def preflight(self) -> None:
        """Raise RuntimeError with an actionable message if unusable."""

    def transcribe(
        self,
        window: Window,
        *,
        context: str,
        language: str | None,
    ) -> tuple[str, Sequence[Word], dict[str, Any]]:
        """Return (text, words, extra) for one window, times relative to it."""

    # Optional. `report(live=...)` adds backend detail to `vstudio doctor`;
    # `transcribe_all(windows, ...)` replaces per-window scheduling for a
    # backend that has to batch, such as one that loads two models in turn.
