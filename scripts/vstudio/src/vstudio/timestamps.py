"""Turn timestamps cited in a summary into links that seek the player.

The agent writes `(02:15)` as plain text and this rewrites it afterwards. Doing
it as a deterministic pass rather than asking the model for links keeps seconds
arithmetic out of the model and makes the behaviour testable.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

# MM:SS or HH:MM:SS. Minutes and seconds stay two digits; hours may be one.
# Chinese and Japanese text writes the colon full width.
_CLOCK = r"\d{1,3}[:：]\d{2}(?:[:：]\d{2})?"
# A timestamp optionally followed by a range partner, in ASCII or full-width
# brackets, since a summary in Chinese or Japanese writes （02:15）.
_CITATION = re.compile(
    rf"[\(\[（【](?P<start>{_CLOCK})(?:\s*[–\-~～—]\s*{_CLOCK})?[\)\]）】]"
)
# Spans already inside a markdown link must be left alone so the pass is idempotent.
_LINKED = re.compile(r"\[[^\]]*\]\([^)]*\)")


def parse_timestamp(text: str) -> float | None:
    """Seconds for an MM:SS or HH:MM:SS string, or None if it is neither."""
    parts = text.strip().replace("：", ":").split(":")
    if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
        return None
    values = [int(p) for p in parts]
    if len(values) == 2:
        # MM:SS, where minutes are unbounded: a 95 minute video is cited 95:30.
        hours, (minutes, seconds) = 0, values
        if seconds > 59:
            return None
    else:
        hours, minutes, seconds = values
        if minutes > 59 or seconds > 59:
            return None
    return float(hours * 3600 + minutes * 60 + seconds)


def _linked_spans(markdown: str) -> list[tuple[int, int]]:
    return [m.span() for m in _LINKED.finditer(markdown)]


def iter_timestamps(
    markdown: str, *, include_linked: bool = False
) -> list[tuple[tuple[int, int], float]]:
    """Citations as ((start, end), seconds).

    By default already-linked citations are skipped, which is what makes the
    linkify pass idempotent. Pass `include_linked=True` to count every citation
    regardless: a reader auditing coverage cares that a moment was cited, not
    whether it has since been turned into a link.

    For a range only the start is reported; the range text is left intact.
    """
    skip = [] if include_linked else _linked_spans(markdown)
    found: list[tuple[tuple[int, int], float]] = []
    for match in _CITATION.finditer(markdown):
        span = match.span()
        if any(a <= span[0] and span[1] <= b for a, b in skip):
            continue
        seconds = parse_timestamp(match.group("start"))
        if seconds is not None:
            found.append((span, seconds))
    return found


def deep_link(url: str, site: str, seconds: float) -> str | None:
    """A URL that seeks to `seconds`, or None when the platform is unknown."""
    if not url:
        return None
    whole = int(seconds)
    host = url.lower()
    joiner = "&" if "?" in url else "?"
    if "youtube" in host or "youtu.be" in host or (site or "").lower() == "youtube":
        return f"{url}{joiner}t={whole}s"
    if "bilibili" in host or (site or "").lower() == "bilibili":
        # Plain seconds, no unit suffix. Unverified against a live player.
        return f"{url}{joiner}t={whole}"
    return None


def linkify_summary(
    markdown: str,
    *,
    url: str,
    site: str = "",
    duration: float | None = None,
    slack_seconds: float = 45.0,
) -> tuple[str, list[dict[str, Any]]]:
    """Rewrite plain timestamp citations as links. Safe to run twice.

    Returns the new text and a list of problems. A citation past `duration`,
    allowing one chunk of slack, is never linked: it is reported instead, since
    a reference the video cannot contain is a fabrication rather than rounding.
    """
    problems: list[dict[str, Any]] = []
    pieces: list[str] = []
    cursor = 0
    for (start, end), seconds in iter_timestamps(markdown):
        text = markdown[start:end]
        if duration is not None and seconds > duration + slack_seconds:
            problems.append(
                {"kind": "out_of_range", "citation": text, "seconds": seconds,
                 "duration": duration}
            )
            continue
        target = deep_link(url, site, seconds)
        if target is None:
            problems.append({"kind": "unlinkable_platform", "citation": text, "site": site})
            continue
        pieces.append(markdown[cursor:start])
        pieces.append(f"[{text}]({target})")
        cursor = end
    pieces.append(markdown[cursor:])
    return "".join(pieces), problems
