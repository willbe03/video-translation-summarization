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


ACCEPTS = Capabilities(
    name="c", needs_segmentation=False, segment_seconds=None, max_parallel=1,
    returns_word_times=True, requirements="",
    accepts_codecs=(("opus", ".ogg"), ("mp3", ".mp3")),
    passthrough_max_bitrate=192_000,
    convert_to=(("libopus", ".ogg", 192_000),),
    max_upload_bytes=300 * 1024 * 1024,
)


def _sixty_seconds(monkeypatch):
    monkeypatch.setattr(asr, "media_duration", lambda path: 60.0)


def test_an_accepted_codec_under_the_bitrate_is_copied(tmp_path, monkeypatch):
    """Uploading the track the file already holds is smaller and spares the
    speech a decode and re-encode before the recogniser ever sees it."""
    calls = []
    _sixty_seconds(monkeypatch)
    monkeypatch.setattr(asr, "_track_codec", lambda media, track: "opus")
    monkeypatch.setattr(asr, "run", _fake_ffmpeg(calls, size=120_000 * 60 // 8))
    out = asr.extract_audio(tmp_path / "in.mkv", tmp_path / "audio.wav", capabilities=ACCEPTS)
    assert out.suffix == ".ogg"
    assert len(calls) == 1 and "copy" in calls[0]


def test_an_accepted_codec_above_the_bitrate_is_converted(tmp_path, monkeypatch):
    """Past the ceiling the track carries detail the recogniser cannot use, so
    converting it is smaller at no cost that matters for speech."""
    calls = []
    _sixty_seconds(monkeypatch)
    monkeypatch.setattr(asr, "_track_codec", lambda media, track: "mp3")
    monkeypatch.setattr(asr, "run", _fake_ffmpeg(calls, size=320_000 * 60 // 8))
    out = asr.extract_audio(tmp_path / "in.mkv", tmp_path / "audio.wav", capabilities=ACCEPTS)
    assert out.suffix == ".ogg"
    assert "-b:a" in calls[-1] and "192000" in calls[-1]


def test_an_unaccepted_codec_is_converted(tmp_path, monkeypatch):
    calls = []
    _sixty_seconds(monkeypatch)
    monkeypatch.setattr(asr, "_track_codec", lambda media, track: "dts")
    monkeypatch.setattr(asr, "run", _fake_ffmpeg(calls, size=1024))
    out = asr.extract_audio(tmp_path / "in.mkv", tmp_path / "audio.wav", capabilities=ACCEPTS)
    assert out.suffix == ".ogg" and "libopus" in calls[0]
    assert len(calls) == 1  # no pointless remux of a codec that cannot be copied


def test_the_bitrate_is_measured_not_read_from_the_container(tmp_path, monkeypatch):
    """Matroska stores Opus with no declared bitrate, so reading the source
    would convert every web video for no reason."""
    calls = []
    _sixty_seconds(monkeypatch)
    monkeypatch.setattr(asr, "_track_codec", lambda media, track: "opus")
    monkeypatch.setattr(asr, "run", _fake_ffmpeg(calls, size=100_000 * 60 // 8))
    out = asr.extract_audio(tmp_path / "in.mkv", tmp_path / "audio.wav", capabilities=ACCEPTS)
    assert out.suffix == ".ogg"


def test_a_copy_that_comes_out_too_large_is_converted(tmp_path, monkeypatch):
    calls = []
    _sixty_seconds(monkeypatch)
    monkeypatch.setattr(asr, "_track_codec", lambda media, track: "mp3")
    monkeypatch.setattr(asr, "run", _fake_ffmpeg(calls, size=400 * 1024 * 1024))
    out = asr.extract_audio(tmp_path / "in.mkv", tmp_path / "audio.wav", capabilities=ACCEPTS)
    assert out.suffix == ".ogg" and any("libopus" in c for c in calls)


def test_a_codec_that_will_not_remux_falls_back_rather_than_failing(tmp_path, monkeypatch):
    import subprocess as sp

    calls = []
    _sixty_seconds(monkeypatch)
    monkeypatch.setattr(asr, "_track_codec", lambda media, track: "opus")

    def run(cmd):
        calls.append(cmd)
        if "copy" in cmd:
            raise sp.CalledProcessError(1, cmd)
        Path(cmd[-1]).write_bytes(b"\0")

    monkeypatch.setattr(asr, "run", run)
    out = asr.extract_audio(tmp_path / "in.mkv", tmp_path / "audio.wav", capabilities=ACCEPTS)
    assert out.suffix == ".ogg"


def test_a_backend_declaring_nothing_still_gets_mono_pcm(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(asr, "run", _fake_ffmpeg(calls, size=16))
    out = asr.extract_audio(tmp_path / "in.mkv", tmp_path / "audio.wav",
                            capabilities=FakeBackend.capabilities)
    assert out.suffix == ".wav" and "pcm_s16le" in calls[0]


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


def test_a_missing_encoder_falls_through_the_chain(tmp_path, monkeypatch):
    """libopus is optional in some ffmpeg builds. Falling straight to PCM would
    turn a 9 MB upload into 165 MB, so MP3 is tried in between."""
    import subprocess as sp

    calls = []
    _sixty_seconds(monkeypatch)
    monkeypatch.setattr(asr, "_track_codec", lambda media, track: "dts")

    def run(cmd):
        calls.append(cmd)
        if "libopus" in cmd:
            raise sp.CalledProcessError(1, cmd)
        Path(cmd[-1]).write_bytes(b"\0")

    monkeypatch.setattr(asr, "run", run)
    chain = Capabilities(
        name="c", needs_segmentation=False, segment_seconds=None, max_parallel=1,
        returns_word_times=True, requirements="",
        convert_to=(("libopus", ".ogg", 192_000), ("libmp3lame", ".mp3", 192_000)),
    )
    out = asr.extract_audio(tmp_path / "in.mkv", tmp_path / "audio.wav", capabilities=chain)
    assert out.suffix == ".mp3"


def test_every_encoder_failing_still_yields_mono_pcm(tmp_path, monkeypatch):
    import subprocess as sp

    calls = []
    monkeypatch.setattr(asr, "_track_codec", lambda media, track: "dts")

    def run(cmd):
        calls.append(cmd)
        if "pcm_s16le" not in cmd:
            raise sp.CalledProcessError(1, cmd)
        Path(cmd[-1]).write_bytes(b"\0")

    monkeypatch.setattr(asr, "run", run)
    chain = Capabilities(
        name="c", needs_segmentation=False, segment_seconds=None, max_parallel=1,
        returns_word_times=True, requirements="",
        convert_to=(("libopus", ".ogg", 192_000), ("libmp3lame", ".mp3", 192_000)),
    )
    out = asr.extract_audio(tmp_path / "in.mkv", tmp_path / "audio.wav", capabilities=chain)
    assert out.suffix == ".wav"
