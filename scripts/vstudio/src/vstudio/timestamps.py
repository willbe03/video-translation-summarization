"""Turn timestamps cited in a summary into links that seek the player.

The agent writes `(02:15)` as plain text and this rewrites it afterwards. Doing
it as a deterministic pass rather than asking the model for links keeps seconds
arithmetic out of the model and makes the behaviour testable.

There is exactly one citation form, shared by the notes, the summary, the
coverage checks and this link pass: one time in ASCII parentheses with ASCII
digits and an ASCII colon, `(01:38)`, minutes running past 59 on a long video,
`(75:20)`. Several moments are separate citations, `(01:38) (02:09)`. Ranges,
lists in one bracket, words inside the bracket, other brackets, HH:MM:SS and
full-width punctuation or digits are not citations; `forbidden_citations`
finds them so the stage that produced them can be sent back.
"""

from __future__ import annotations

import re
from typing import Any

# [0-9], not \d: \d also matches full-width and other Unicode digits.
CITATION = re.compile(r"\(([0-9]{1,3}):([0-5][0-9])\)")
# Spans already inside a markdown link must be left alone so the pass is idempotent.
_LINKED = re.compile(r"\[[^\]]*\]\([^)]*\)")
# Anything in a bracket of any kind that looks like a clock time, in any digits
# and either colon. A bracket that holds one but is not exactly CITATION is a
# citation in a forbidden form. Angle brackets are left out: markdown autolinks
# and HTML use them, and a port number would read as a clock.
_OPEN, _CLOSE = "([（【［「『〔〖｛{", ")]）】］」』〕〗｝}"
_ANY_BRACKET = re.compile(
    rf"[{re.escape(_OPEN)}]([^{re.escape(_OPEN + _CLOSE)}]{{0,60}})[{re.escape(_CLOSE)}]"
)
_CLOCK_LIKE = re.compile(r"\d{1,3}\s*[:：]\s*\d{1,2}")
# Appended to every refusal: a bracket around a ratio, a score or a time of day
# reads as a citation, and the fix there is to drop the brackets.
FORBIDDEN_HINT = ("If one of these is not a citation, such as a ratio, a score or a time of "
                  "day, write it without brackets.")


def parse_timestamp(text: str) -> float | None:
    """Seconds for an `MM:SS` string in ASCII, or None for anything else."""
    match = re.fullmatch(r"([0-9]{1,3}):([0-5][0-9])", text.strip())
    if not match:
        return None
    return float(int(match.group(1)) * 60 + int(match.group(2)))


def _linked_spans(markdown: str) -> list[tuple[int, int]]:
    return [m.span() for m in _LINKED.finditer(markdown)]


def iter_timestamps(
    markdown: str, *, include_linked: bool = False
) -> list[tuple[tuple[int, int], float]]:
    """Citations in the allowed form as ((start, end), seconds).

    By default already-linked citations are skipped, which is what makes the
    linkify pass idempotent. Pass `include_linked=True` to count every citation
    regardless: a reader auditing coverage cares that a moment was cited, not
    whether it has since been turned into a link.
    """
    skip = [] if include_linked else _linked_spans(markdown)
    found: list[tuple[tuple[int, int], float]] = []
    for match in CITATION.finditer(markdown):
        span = match.span()
        if any(a <= span[0] and span[1] <= b for a, b in skip):
            continue
        found.append((span, float(int(match.group(1)) * 60 + int(match.group(2)))))
    return found


def forbidden_citations(markdown: str) -> list[str]:
    """Every bracketed clock time not written in the one allowed form, in order."""
    return [
        match.group(0) for match in _ANY_BRACKET.finditer(markdown)
        if _CLOCK_LIKE.search(match.group(1)) and not CITATION.fullmatch(match.group(0))
    ]


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
    Only the one allowed form is linked; `apply-synthesize` refuses a summary
    holding any other form before this runs.
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
