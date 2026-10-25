"""The opt-in Bilibili /en/video/ retry, the switch for it, and /en/ links."""

import subprocess
import sys

import pytest

from vstudio import acquire as acq
from vstudio import jobs, media, youget_runner

STATE_PAGE = '<script>window.__INITIAL_STATE__={"videoData":{"title":"原标题"}};(function(){})</script>'
EMPTY_PAGE = "<script>window.__INITIAL_STATE__&&(window.webAbTest={})</script>"


def _fetcher(pages, calls):
    def get_content(url, *args, **kwargs):
        calls.append(url)
        page = pages[url]
        if isinstance(page, Exception):
            raise page
        return page
    return get_content


def test_an_empty_video_page_is_fetched_again_from_en_keeping_the_query():
    url = "https://www.bilibili.com/video/BV1x/?p=2&spm_id_from=a/video/b"
    en = "https://www.bilibili.com/en/video/BV1x/?p=2&spm_id_from=a/video/b"
    calls = []
    fetch = youget_runner.with_en_fallback(_fetcher({url: EMPTY_PAGE, en: STATE_PAGE}, calls))
    assert fetch(url, headers={"Referer": "x"}) == STATE_PAGE
    assert calls == [url, en]


def test_a_page_with_its_state_or_a_non_page_url_costs_one_request():
    page = "https://www.bilibili.com/video/BV1x/"
    api = "https://api.bilibili.com/x/player/playurl?bvid=BV1x"
    calls = []
    fetch = youget_runner.with_en_fallback(_fetcher({page: STATE_PAGE, api: "{}"}, calls))
    assert fetch(page) == STATE_PAGE and fetch(api) == "{}"
    assert calls == [page, api]


@pytest.mark.parametrize("en_page", [RuntimeError("HTTP 403"), EMPTY_PAGE])
def test_a_failed_retry_returns_the_original_page_and_says_why(en_page, capsys):
    url = "https://www.bilibili.com/video/BV1x/"
    en = "https://www.bilibili.com/en/video/BV1x/"
    fetch = youget_runner.with_en_fallback(_fetcher({url: EMPTY_PAGE, en: en_page}, []))
    assert fetch(url) == EMPTY_PAGE
    err = capsys.readouterr().err
    assert "vstudio:" in err and en in err


def test_wrapping_twice_does_not_stack():
    fetch = youget_runner.with_en_fallback(lambda url: STATE_PAGE)
    assert youget_runner.with_en_fallback(fetch) is fetch


def test_the_switch_is_off_unless_set(monkeypatch):
    monkeypatch.delenv(jobs.BILIBILI_EN_FALLBACK_ENV, raising=False)
    assert not jobs.bilibili_en_fallback_enabled()
    for value, expected in (("1", True), ("on", True), ("0", False), ("off", False)):
        monkeypatch.setenv(jobs.BILIBILI_EN_FALLBACK_ENV, value)
        assert jobs.bilibili_en_fallback_enabled() is expected


@pytest.mark.parametrize("value, patched", [(None, False), ("0", False), ("1", True)])
def test_the_runner_patches_only_when_switched_on(value, patched, tmp_path):
    """Run in a child process, as media.py does, so the patch cannot leak."""
    code = (
        "import sys; import you_get.extractors.bilibili as b; "
        "import you_get.__main__ as m; m.main = lambda: None; "
        "from vstudio import youget_runner as r; r.main(); "
        "print(getattr(b.get_content, '_vstudio_en_fallback', False))"
    )
    env = {k: v for k, v in __import__("os").environ.items() if k != jobs.BILIBILI_EN_FALLBACK_ENV}
    if value is not None:
        env[jobs.BILIBILI_EN_FALLBACK_ENV] = value
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            env=env, cwd=tmp_path, check=True)
    assert result.stdout.strip() == str(patched)


def test_configure_writes_the_switch(monkeypatch, tmp_path, capsys):
    from vstudio import cli
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    # cli.main loads the file into os.environ; setting the key first makes
    # monkeypatch restore it, so the switch does not leak into later tests.
    monkeypatch.setenv(jobs.BILIBILI_EN_FALLBACK_ENV, "")
    for flag, stored in (("on", "1"), ("off", "0")):
        cli.main(["configure", "--bilibili-en-fallback", flag])
        assert f"{jobs.BILIBILI_EN_FALLBACK_ENV}={stored}" in jobs.config_path().read_text()
    capsys.readouterr()


@pytest.mark.parametrize("given, expected", [
    ("https://www.bilibili.com/en/video/BV1x/?p=2", "https://www.bilibili.com/video/BV1x/?p=2"),
    ("https://www.bilibili.com/video/BV1x/", "https://www.bilibili.com/video/BV1x/"),
    ("https://www.youtube.com/watch?v=x", "https://www.youtube.com/watch?v=x"),
])
def test_an_en_link_is_accepted_as_the_same_video(given, expected):
    assert media.canonical_url(given) == expected


def test_acquire_downloads_and_records_the_canonical_url(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(acq, "download_video", lambda url, *a, **k: seen.append(url) or (
        tmp_path / "v.mp4", {"title": "T", "site": "Bilibili"},
        media.StreamChoice("dash", 480, "480P")))
    monkeypatch.setattr(acq, "fetch_source_metadata", lambda url, cookies: seen.append(url) or {})
    monkeypatch.setattr(acq, "probe_media", lambda path: {
        "has_audio": True, "has_video": True, "width": 854, "height": 480, "duration": 60.0})
    monkeypatch.setattr(acq, "audio_streams", lambda path: [{"track": 0}])
    monkeypatch.setattr(acq, "choose_audio_stream", lambda streams, language=None:
                        {"track": 0, "reason": "only track"})
    facts = acq.acquire("https://www.bilibili.com/en/video/BV1x/", tmp_path)
    assert facts["url"] == "https://www.bilibili.com/video/BV1x/"
    assert seen == ["https://www.bilibili.com/video/BV1x/"] * 2


def test_a_failed_bilibili_inspection_points_at_the_switch_only_when_off(monkeypatch):
    def fail(command, capture=False):
        raise subprocess.CalledProcessError(1, command, output="oops")
    monkeypatch.setattr(media, "run", fail)
    monkeypatch.delenv(jobs.BILIBILI_EN_FALLBACK_ENV, raising=False)
    for url in ("https://www.bilibili.com/video/BV1x/", "https://b23.tv/BV1x"):
        with pytest.raises(RuntimeError, match="--bilibili-en-fallback on"):
            media.inspect_url(url)
    with pytest.raises(RuntimeError) as raised:
        media.inspect_url("https://space.bilibili.com/123")
    assert "--bilibili-en-fallback" not in str(raised.value)
    monkeypatch.setenv(jobs.BILIBILI_EN_FALLBACK_ENV, "1")
    with pytest.raises(RuntimeError) as raised:
        media.inspect_url("https://www.bilibili.com/video/BV1x/")
    assert "--bilibili-en-fallback" not in str(raised.value)
