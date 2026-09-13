"""Turn whatever the user gave us into a media file plus what we know about it.

A URL is downloaded into the job directory. A local file is referenced where it
sits, because these are routinely gigabytes and copying one wastes minutes and
disk. Nothing else in the pipeline needs to know which it was.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from vstudio.media import (
    audio_streams,
    choose_audio_stream,
    download_video,
    fetch_source_metadata,
    infer_context,
    is_local_source,
    probe_media,
    title_from_filename,
)


def describe_source(source: str) -> dict[str, Any]:
    """Classify the argument before any work starts, so a typo fails fast."""
    if is_local_source(source):
        path = Path(source).expanduser().resolve()
        return {"kind": "local", "path": str(path), "title": title_from_filename(path)}
    if "://" in source:
        return {"kind": "url", "url": source, "title": ""}
    raise RuntimeError(
        f"{source!r} is neither a readable file nor a URL. Check the path, or pass "
        f"a link beginning with https://."
    )


def _audio_choice(media: Path, language: str | None) -> dict[str, Any]:
    streams = audio_streams(media)
    chosen = choose_audio_stream(streams, language=language)
    return {
        "audio_track": chosen["track"],
        "audio_track_reason": chosen["reason"],
        "audio_tracks": streams,
        "audio_track_ambiguous": len(streams) > 1,
    }


def acquire(
    source: str,
    job: Path,
    *,
    cookies: Path | None = None,
    max_height: int = 720,
    language: str | None = None,
) -> dict[str, Any]:
    described = describe_source(source)
    if described["kind"] == "local":
        media = Path(described["path"])
        probe = probe_media(media)
        if not probe["has_audio"]:
            raise RuntimeError(f"{media} has no audio stream, so there is nothing to transcribe.")
        title = described["title"]
        return {
            "source": source,
            "source_kind": "local",
            "media": str(media),
            "media_borrowed": True,
            "title": title,
            "duration": probe["duration"],
            "has_video": probe["has_video"],
            "width": probe["width"],
            "height": probe["height"],
            "page": {},
            "asr_context": infer_context(title),
            **_audio_choice(media, language),
        }

    media, metadata, choice = download_video(source, job / "media", cookies=cookies, max_height=max_height)
    page = fetch_source_metadata(source, cookies)
    title = str(metadata.get("title") or media.stem)
    probe = probe_media(media)
    return {
        "source": source,
        "source_kind": "url",
        "url": source,
        "site": metadata.get("site") or "",
        "media": str(media),
        "media_borrowed": False,
        "title": title,
        "stream": {"id": choice.stream_id, "height": choice.height, "quality": choice.quality},
        "duration": probe["duration"],
        "has_video": probe["has_video"],
        "width": probe["width"],
        "height": probe["height"],
        "page": page,
        "asr_context": infer_context(title, str(page.get("description") or "")),
        **_audio_choice(media, language),
    }
