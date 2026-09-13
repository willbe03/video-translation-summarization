"""Mechanical checks that a summary actually covers its video.

Two failure modes are worth catching without a human: the transcript stopping
early, and the summary describing only the opening. Both are things the skill
warns about in prose and never verified.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from vstudio.timestamps import iter_timestamps

# Below this fraction of the video, the latest cited moment suggests the writer
# stopped reading partway rather than that the tail was uninteresting.
COVERAGE_WARN_FRACTION = 0.75


def check_summary(
    manifest: Mapping[str, Any],
    transcript: Sequence[Mapping[str, Any]] | None,
    summary_markdown: str | None,
) -> dict[str, Any]:
    """Judge coverage. Returns {ok, failures, warnings, details}."""
    failures: list[str] = []
    warnings: list[str] = []
    details: dict[str, Any] = {}

    duration = manifest.get("duration")
    # Speech ending a little before the video does is normal: credits, music,
    # a held final frame. Forty-five seconds is the slack before it looks like
    # transcription stopped early.
    chunk = float(manifest.get("tail_slack_seconds") or 45)
    details["duration"] = duration
    details["tail_slack_seconds"] = chunk

    if not summary_markdown or not summary_markdown.strip():
        failures.append("summary.md is missing or empty")
    if not transcript:
        failures.append("transcript.json is missing or empty")

    if transcript:
        last_end = max(float(seg.get("end") or 0) for seg in transcript)
        details["transcript_end"] = last_end
        details["segments"] = len(transcript)
        if duration and last_end < float(duration) - chunk:
            failures.append(
                f"transcript stops at {last_end:.0f}s but the video runs {float(duration):.0f}s, "
                f"a gap of more than {chunk:.0f}s"
            )

    if summary_markdown:
        # Count every citation, linked or not. The skill runs link-timestamps
        # before this gate, and skipping linked ones made a fully covered
        # summary report zero citations and warn that coverage was unverifiable.
        cited = [
            seconds for _, seconds in iter_timestamps(summary_markdown, include_linked=True)
        ]
        details["timestamps_cited"] = len(cited)
        details["latest_citation"] = max(cited) if cited else None
        if duration:
            beyond = [s for s in cited if s > float(duration) + chunk]
            if beyond:
                failures.append(
                    f"summary cites {len(beyond)} timestamp(s) past the end of the video: "
                    + ", ".join(f"{s:.0f}s" for s in sorted(beyond))
                )
        if not cited:
            warnings.append(
                "summary cites no timestamp, so timeline coverage cannot be verified"
            )
        elif duration and max(cited) < float(duration) * COVERAGE_WARN_FRACTION:
            warnings.append(
                f"latest cited moment is {max(cited):.0f}s, before "
                f"{COVERAGE_WARN_FRACTION:.0%} of a {float(duration):.0f}s video, "
                "which usually means only the opening was summarized"
            )

    return {"ok": not failures, "failures": failures, "warnings": warnings, "details": details}
