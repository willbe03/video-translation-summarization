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
    monkeypatch.setattr(asr, "extract_audio", lambda media, target, **kw: media)
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
    monkeypatch.setattr(asr, "extract_audio", lambda media, target, **kw: media)
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


def test_a_single_track_file_needs_no_choosing():
    from vstudio.media import choose_audio_stream

    chosen = choose_audio_stream([{"track": 0, "language": "", "default": False, "flags": []}])
    assert chosen["track"] == 0 and chosen["reason"] == "the only audio track"


def test_a_commentary_track_is_never_chosen():
    """Transcribing a director's commentary produces a fluent, wrong subtitle
    file that nothing downstream would flag."""
    from vstudio.media import choose_audio_stream

    streams = [
        {"track": 0, "language": "eng", "default": False, "flags": ["comment"]},
        {"track": 1, "language": "eng", "default": True, "flags": []},
    ]
    assert choose_audio_stream(streams)["track"] == 1


def test_the_expected_language_wins_over_the_default_flag():
    from vstudio.media import choose_audio_stream

    streams = [
        {"track": 0, "language": "eng", "default": True, "flags": []},
        {"track": 1, "language": "jpn", "default": False, "flags": []},
    ]
    chosen = choose_audio_stream(streams, language="ja-JP")
    assert chosen["track"] == 1 and "ja" in chosen["reason"]


def test_the_default_flag_decides_when_no_language_was_asked_for():
    from vstudio.media import choose_audio_stream

    streams = [
        {"track": 0, "language": "jpn", "default": False, "flags": []},
        {"track": 1, "language": "eng", "default": True, "flags": []},
    ]
    chosen = choose_audio_stream(streams)
    assert chosen["track"] == 1 and "default" in chosen["reason"]


def test_an_unresolvable_choice_says_so_rather_than_pretending():
    from vstudio.media import choose_audio_stream

    streams = [
        {"track": 0, "language": "jpn", "default": False, "flags": []},
        {"track": 1, "language": "eng", "default": False, "flags": []},
    ]
    chosen = choose_audio_stream(streams)
    assert chosen["track"] == 0
    assert "nothing to prefer another by" in chosen["reason"]


def test_every_track_being_unusable_still_yields_one():
    """Better a commentary than a crash: the report says what was picked."""
    from vstudio.media import choose_audio_stream

    streams = [{"track": 0, "language": "eng", "default": True, "flags": ["comment"]}]
    assert choose_audio_stream(streams)["track"] == 0


def test_no_audio_at_all_is_an_error_with_a_reason():
    import pytest as _pytest

    from vstudio.media import choose_audio_stream

    with _pytest.raises(RuntimeError, match="nothing to transcribe"):
        choose_audio_stream([])


def _fake_ffmpeg(calls, *, size):
    """Record the command and write an output file of the given size."""
    def run(cmd):
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"\0" * size)
    return run


def test_an_accepted_codec_is_copied_rather_than_re_encoded(tmp_path, monkeypatch):
    """Uploading the track the file already holds is smaller and spares the
    speech a decode and re-encode before the recogniser ever sees it."""
    calls = []
    monkeypatch.setattr(asr, "_track_codec", lambda media, track: "opus")
    monkeypatch.setattr(asr, "run", _fake_ffmpeg(calls, size=1024))
    caps = Capabilities(
        name="c", needs_segmentation=False, segment_seconds=None, max_parallel=1,
        returns_word_times=True, requirements="", accepts_codecs=(("opus", ".ogg"),),
        max_upload_bytes=10 * 1024,
    )
    out = asr.extract_audio(tmp_path / "in.mkv", tmp_path / "audio.wav", capabilities=caps)
    assert out.suffix == ".ogg"
    assert "copy" in calls[0] and len(calls) == 1


def test_an_unaccepted_codec_falls_back_to_mono_pcm(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(asr, "_track_codec", lambda media, track: "dts")
    monkeypatch.setattr(asr, "run", _fake_ffmpeg(calls, size=1024))
    caps = Capabilities(
        name="c", needs_segmentation=False, segment_seconds=None, max_parallel=1,
        returns_word_times=True, requirements="", accepts_codecs=(("opus", ".ogg"),),
    )
    out = asr.extract_audio(tmp_path / "in.mkv", tmp_path / "audio.wav", capabilities=caps)
    assert out.suffix == ".wav" and "pcm_s16le" in calls[0]


def test_a_copy_that_comes_out_too_large_is_re_encoded(tmp_path, monkeypatch):
    """A lossless multichannel track is bigger than the mono downmix, so
    copying it would be the wrong call even though the codec is accepted."""
    calls = []
    monkeypatch.setattr(asr, "_track_codec", lambda media, track: "flac")
    monkeypatch.setattr(asr, "run", _fake_ffmpeg(calls, size=10 ** 6))
    caps = Capabilities(
        name="c", needs_segmentation=False, segment_seconds=None, max_parallel=1,
        returns_word_times=True, requirements="", accepts_codecs=(("flac", ".flac"),),
        max_upload_bytes=1024,
    )
    out = asr.extract_audio(tmp_path / "in.mkv", tmp_path / "audio.wav", capabilities=caps)
    assert out.suffix == ".wav" and any("pcm_s16le" in c for c in calls)


def test_the_declared_content_type_matches_what_was_uploaded():
    """The endpoint reads the declared type, so a copied Opus track announced
    as audio/wav is refused rather than transcribed."""
    from vstudio.asr.mai import MaiBackend, content_type

    for codec, suffix in MaiBackend.capabilities.accepts_codecs:
        assert content_type(Path(f"a{suffix}")) != "application/octet-stream", codec
    assert content_type(Path("a.wav")) == "audio/wav"


def test_media_longer_than_the_endpoint_takes_is_cut_anyway(tmp_path, monkeypatch):
    from vstudio.asr.mai import MaiBackend

    monkeypatch.setattr(asr, "media_duration", lambda path: 3 * 3600)
    pieces = [tmp_path / "a.wav", tmp_path / "b.wav"]
    for piece in pieces:
        piece.write_bytes(b"")
    monkeypatch.setattr(asr, "extract_chunks", lambda audio, work, chunk_seconds: pieces)
    windows = asr.plan_windows(tmp_path / "audio.wav", tmp_path, MaiBackend.capabilities)
    assert len(windows) == 2


def test_the_request_timeout_grows_with_what_has_to_be_uploaded():
    from vstudio.asr.mai import request_timeout

    assert request_timeout(11 * 1024 * 1024) < request_timeout(165 * 1024 * 1024)
    assert request_timeout(10**12) <= 3600
