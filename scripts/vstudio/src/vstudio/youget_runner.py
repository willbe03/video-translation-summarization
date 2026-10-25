from __future__ import annotations

import sys
from typing import Callable

from vstudio.jobs import BILIBILI_EN_FALLBACK_ENV, bilibili_en_fallback_enabled

# you-get is imported inside the patch functions, not here: importing it
# replaces sys.stdout, which only the child process this module runs as
# should see.

# BILIBILI_EN_FALLBACK_ENV is off unless set. Outside mainland China
# www.bilibili.com/video/ can come back as a page without its video data, which
# you-get reports as a bare "oops". The /en/video/ page for the same video
# carries the original title, description, owner and part names (only the
# site's own interface is English), so with it set an empty page is fetched
# again from there. Streams come from the playurl API either way, so quality
# does not depend on which page was parsed.
_STATE = "__INITIAL_STATE__="


def _patch_youtube_dethrottle() -> None:
    """Keep current YouTube URLs usable when you-get's JS regex lags."""
    from you_get.extractors.youtube import YouTube

    original = YouTube.dethrottle

    def safe_dethrottle(js: str, url: str) -> str:
        try:
            return original(js, url)
        except (KeyError, TypeError, AttributeError):
            # The n parameter only controls throttling. Leaving it unchanged is
            # slower but preferable to making the extractor unusable.
            return url

    YouTube.dethrottle = staticmethod(safe_dethrottle)


def _warn(message: str) -> None:
    # stderr, because `--json` callers parse stdout.
    print(f"vstudio: {message}", file=sys.stderr)


def with_en_fallback(get_content: Callable[..., str]) -> Callable[..., str]:
    """Wrap you-get's page fetch so an empty Bilibili video page is retried from /en/video/.

    Only a bilibili.com/video/ page lacking its state is retried; API calls and
    pages that already carry the state go through untouched, with one request
    as before.
    When the retry fails too, the original page comes back so you-get fails the
    way it would have, after a warning that says why.
    """
    if getattr(get_content, "_vstudio_en_fallback", False):
        return get_content

    def fetch(url: str, *args, **kwargs) -> str:
        content = get_content(url, *args, **kwargs)
        if "bilibili.com/video/" not in url or _STATE in (content or ""):
            return content
        en_url = url.replace("bilibili.com/video/", "bilibili.com/en/video/", 1)
        try:
            en_content = get_content(en_url, *args, **kwargs)
        except Exception as exc:  # any fetch failure leaves the original outcome
            _warn(f"{url} had no video data and fetching {en_url} failed: {exc}")
            return content
        if _STATE not in (en_content or ""):
            _warn(f"neither {url} nor {en_url} carried video data")
            return content
        return en_content

    fetch._vstudio_en_fallback = True  # type: ignore[attr-defined]
    return fetch


def _patch_bilibili() -> None:
    try:
        import you_get.extractors.bilibili as module

        module.get_content = with_en_fallback(module.get_content)
    except (ImportError, AttributeError) as exc:
        _warn(f"{BILIBILI_EN_FALLBACK_ENV} is set but the Bilibili extractor "
              f"could not be patched: {exc}")


def main() -> None:
    _patch_youtube_dethrottle()
    if bilibili_en_fallback_enabled():
        _patch_bilibili()
    from you_get.__main__ import main as you_get_main

    you_get_main()


if __name__ == "__main__":
    main()
