import json
from pathlib import Path

import pytest

from vstudio import asr
from vstudio.asr.base import Capabilities, Window, Word


class FakeBackend:
    capabilities = Capabilities(
        name="fake", needs_segmentation=False, segment_seconds=None, max_parallel=4,
        returns_word_times=True, requirements="nothing", prefers_whole_file=True,
    )

    def preflight(self):
        return None

    def transcribe(self, window, *, context, language):
        # Times are relative to the window, which is what the runner promises.
        return "hi there", [Word("hi", 0.0, 0.5), Word("there", 0.5, 1.0)], {"language": "English"}


def test_word_times_are_shifted_onto_the_video_clock(monkeypatch, tmp_path):
    asr.register("fake", FakeBackend)
    monkeypatch.setattr(asr, "extract_audio", lambda media, target: media)
    monkeypatch.setattr(
        asr, "plan_windows",
        lambda audio, work, caps, split_seconds=None: [
            Window(0, audio, 0.0, 60.0), Window(1, audio, 60.0, 60.0)
        ],
    )
    media = tmp_path / "media.wav"
    media.write_bytes(b"")
    payload = asr.transcribe(media, tmp_path / "asr", provider="fake", title="t")
    assert [w["start"] for w in payload["words"]] == [0.0, 0.5, 60.0, 60.5]
    assert payload["word_count"] == 4
    assert len(payload["windows"]) == 2


def test_an_empty_transcript_says_what_to_check(monkeypatch, tmp_path):
    class Silent(FakeBackend):
        def transcribe(self, window, *, context, language):
            return "", [], {}

    asr.register("silent", Silent)
    monkeypatch.setattr(asr, "extract_audio", lambda media, target: media)
    monkeypatch.setattr(
        asr, "plan_windows", lambda audio, work, caps, split_seconds=None: [Window(0, audio, 0.0, 1.0)]
    )
    media = tmp_path / "media.wav"
    media.write_bytes(b"")
    with pytest.raises(RuntimeError, match="audible speech"):
        asr.transcribe(media, tmp_path / "asr", provider="silent", title="t")


def test_an_unknown_provider_lists_the_known_ones():
    with pytest.raises(RuntimeError, match="mai"):
        asr.load_backend("nope")


def test_a_backend_that_needs_cutting_gets_windows(monkeypatch, tmp_path):
    caps = Capabilities(
        name="cut", needs_segmentation=True, segment_seconds=45, max_parallel=1,
        returns_word_times=True, requirements="nothing", prefers_whole_file=False,
    )
    pieces = [tmp_path / "a.wav", tmp_path / "b.wav"]
    for piece in pieces:
        piece.write_bytes(b"")
    monkeypatch.setattr(asr, "extract_chunks", lambda audio, work, chunk_seconds: pieces)
    monkeypatch.setattr(asr, "media_duration", lambda path: 45.0)
    windows = asr.plan_windows(tmp_path / "audio.wav", tmp_path, caps)
    assert [w.offset for w in windows] == [0.0, 45.0]


def test_a_whole_file_backend_is_only_cut_when_asked(monkeypatch, tmp_path):
    monkeypatch.setattr(asr, "media_duration", lambda path: 600.0)
    windows = asr.plan_windows(tmp_path / "audio.wav", tmp_path, FakeBackend.capabilities)
    assert len(windows) == 1 and windows[0].duration == 600.0
