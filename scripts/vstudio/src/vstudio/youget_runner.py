from __future__ import annotations

import sys

from you_get.extractors.youtube import YouTube


def _patch_youtube_dethrottle() -> None:
    """Keep current YouTube URLs usable when you-get's JS regex lags."""
    original = YouTube.dethrottle

    def safe_dethrottle(js: str, url: str) -> str:
        try:
            return original(js, url)
        except (KeyError, TypeError, AttributeError):
            # The n parameter only controls throttling. Leaving it unchanged is
            # slower but preferable to making the extractor unusable.
            return url

    YouTube.dethrottle = staticmethod(safe_dethrottle)


def main() -> None:
    _patch_youtube_dethrottle()
    from you_get.__main__ import main as you_get_main

    you_get_main()


if __name__ == "__main__":
    main()
