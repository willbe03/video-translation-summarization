"""Audio-only acquisition for summary jobs, and its fallback to the full video."""

from pathlib import Path

import pytest

from vstudio import acquire as acq
from vstudio import media
from vstudio.media import StreamChoice


def _stub_common(monkeypatch, calls):
    monkeypatch.setattr(acq, "inspect_url", lambda url, cookies: calls.append("inspect") or
                        {"title": "T", "site": "Bilibili", "streams": {}})
    monkeypatch.setattr(acq, "fetch_source_metadata", lambda url, cookies: {})
    monkeypatch.setattr(acq, "probe_media", lambda path: {
        "has_audio": True, "has_video": path.suffix == ".mp4", "width": None, "height": None,
        "duration": 60.0})
    monkeypatch.setattr(acq, "audio_streams", lambda path: [{"track": 0}])
    monkeypatch.setattr(acq, "choose_audio_stream", lambda streams, language=None:
                        {"track": 0, "reason": "only track"})


def test_audio_only_uses_the_audio_track_and_inspects_once(tmp_path, monkeypatch):
    calls = []
    _stub_common(monkeypatch, calls)
    audio = tmp_path / "media" / "audio.m4a"

    def fake_audio(url, job_dir, *, cookies=None, metadata=None):
        assert metadata is not None
        calls.append("audio")
        return audio

    monkeypatch.setattr(acq, "download_audio", fake_audio)
    monkeypatch.setattr(acq, "download_video", lambda *a, **k: pytest.fail("no video download"))
    facts = acq.acquire("https://www.bilibili.com/video/BVx", tmp_path, audio_only=True)
    assert facts["media"] == str(audio)
    assert facts["audio_only"] is True
    assert facts["has_video"] is False
    assert calls == ["inspect", "audio"]


def test_audio_only_falls_back_to_the_video_without_a_second_lookup(tmp_path, monkeypatch):
    calls = []
    _stub_common(monkeypatch, calls)
    video = tmp_path / "media" / "video.mp4"
    monkeypatch.setattr(acq, "download_audio", lambda *a, **k: None)

    def fake_video(url, job_dir, *, cookies=None, max_height=720, metadata=None):
        assert metadata is not None, "the fallback must reuse the first inspection"
        calls.append("video")
        return video, metadata, StreamChoice("dash-480", 480, "480P")

    monkeypatch.setattr(acq, "download_video", fake_video)
    facts = acq.acquire("https://www.bilibili.com/video/BVx", tmp_path, audio_only=True)
    assert facts["media"] == str(video)
    assert facts["audio_only"] is False
    assert calls == ["inspect", "video"]


def test_the_default_still_downloads_the_video(tmp_path, monkeypatch):
    calls = []
    _stub_common(monkeypatch, calls)
    video = tmp_path / "media" / "video.mp4"
    monkeypatch.setattr(acq, "download_audio", lambda *a, **k: pytest.fail("audio only not asked"))
    monkeypatch.setattr(acq, "download_video", lambda url, job_dir, **k:
                        (video, {"title": "T"}, StreamChoice("dash-480", 480, "480P")))
    facts = acq.acquire("https://www.bilibili.com/video/BVx", tmp_path)
    assert facts["audio_only"] is False


def test_the_audio_half_of_a_dash_stream_is_found():
    metadata = {"streams": {
        "dash-flv480-AVC": {"src": [["https://cdn/v.m4s"], ["https://cdn/a.m4s"]]},
    }}
    assert media._dash_audio_urls(metadata) == ["https://cdn/a.m4s"]
    # A progressive stream lists one flat list of parts and has no separate audio.
    assert media._dash_audio_urls({"streams": {"mp4": {"src": ["https://cdn/v.mp4"]}}}) == []


def test_download_audio_picks_the_path_by_backend(tmp_path, monkeypatch):
    used = []
    monkeypatch.setattr(media, "_yt_dlp_audio", lambda url, d, c: used.append("yt-dlp") or None)
    monkeypatch.setattr(media, "_fetch_dash_audio", lambda m, d: used.append("dash") or None)
    resolved = {"backend": "you-get-with-yt-dlp-url-resolution", "streams": {}}
    assert media.download_audio("https://www.youtube.com/watch?v=x", tmp_path, metadata=resolved) is None
    assert used == ["yt-dlp"]
    used.clear()
    assert media.download_audio("https://www.bilibili.com/video/BVx", tmp_path,
                                metadata={"streams": {}}) is None
    assert used == ["dash"], "a non-YouTube site with no DASH audio falls back, no yt-dlp"
    used.clear()
    media.download_audio("https://www.youtube.com/watch?v=x", tmp_path, metadata={"streams": {}})
    assert used == ["dash", "yt-dlp"]


def test_a_dropped_dash_download_leaves_nothing_behind(tmp_path, monkeypatch):
    import http.client
    import urllib.request

    class Dropping:
        def __init__(self):
            self.sent = False

        def read(self, size):
            if not self.sent:
                self.sent = True
                return b"\0" * 1024
            raise http.client.IncompleteRead(b"")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout: Dropping())
    metadata = {"url": "https://www.bilibili.com/video/BVx",
                "streams": {"dash": {"src": [["https://cdn/v.m4s"], ["https://cdn/a.m4s"]]}}}
    assert media._fetch_dash_audio(metadata, tmp_path) is None
    assert list(tmp_path.iterdir()) == []


def test_dash_audio_that_probes_as_video_is_refused(tmp_path, monkeypatch):
    import urllib.request

    class Body:
        def __init__(self):
            self.chunks = [b"\0" * 1024]

        def read(self, size):
            return self.chunks.pop() if self.chunks else b""

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout: Body())
    monkeypatch.setattr(media, "probe_media", lambda path: {
        "has_audio": True, "has_video": True, "width": 640, "height": 360, "duration": 60.0})
    metadata = {"streams": {"dash": {"src": [["https://cdn/a"], ["https://cdn/b"]]}}}
    assert media._fetch_dash_audio(metadata, tmp_path) is None
    assert list(tmp_path.iterdir()) == []


def test_a_frame_cannot_be_taken_from_an_audio_only_job(tmp_path):
    import json

    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"media": str(tmp_path / "audio.m4a"), "audio_only": True}))
    with pytest.raises(RuntimeError, match="audio only"):
        media.take_frame(manifest, "00:01:00")
