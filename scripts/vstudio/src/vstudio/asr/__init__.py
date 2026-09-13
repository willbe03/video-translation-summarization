"""ASR backend registry and the runner that schedules them.

The runner owns everything that is the same whatever transcribes the audio:
deciding whether to cut the media, running windows concurrently, shifting word
times back onto the video's own clock, and writing the artifacts. A backend
only has to turn one piece of audio into words.
"""

from __future__ import annotations

import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from vstudio.asr.base import Backend, Capabilities, Window, Word
from vstudio.media import extract_chunks, format_timestamp, media_duration, run

_BACKENDS: dict[str, Callable[[], Backend]] = {}


def register(name: str, factory: Callable[[], Backend]) -> None:
    _BACKENDS[name] = factory


def _load_mai() -> Backend:
    from vstudio.asr.mai import MaiBackend

    return MaiBackend()


def _load_qwen_local() -> Backend:
    from vstudio.asr.qwen_local import QwenLocalBackend

    return QwenLocalBackend()


register("mai", _load_mai)
register("qwen-local", _load_qwen_local)


def backend_names() -> list[str]:
    return sorted(_BACKENDS)


def load_backend(name: str) -> Backend:
    try:
        factory = _BACKENDS[name]
    except KeyError:
        raise RuntimeError(
            f"unknown ASR provider {name!r}; available: {', '.join(backend_names())}"
        ) from None
    return factory()


def extract_audio(
    media: Path,
    target: Path,
    *,
    track: int = 0,
    capabilities: Capabilities | None = None,
) -> Path:
    """The chosen audio track, ready to hand to the backend.

    A track already in a codec the backend accepts, and not encoded more richly
    than speech recognition can use, is copied: a fraction of the bytes and one
    generation of loss fewer than decoding and re-encoding it. Anything else is
    converted once, to a bitrate comfortably above what the recogniser needs.

    The track is named explicitly rather than left to ffmpeg's default, which
    on a file carrying a dub or a commentary is not necessarily the programme.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    accepted = dict(capabilities.accepts_codecs) if capabilities else {}
    suffix = accepted.get(_track_codec(media, track)) if accepted else None
    if suffix:
        copied = target.with_suffix(suffix)
        if _copy_track(media, copied, track) and _acceptable_copy(copied, capabilities):
            return copied
        copied.unlink(missing_ok=True)

    for convert in (capabilities.convert_to if capabilities else ()):
        if _convert(media, target, track, convert):
            return target.with_suffix(convert[1])
    # Mono PCM needs no optional encoder, so it is what is left when every
    # preferred one is missing from this ffmpeg build.
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(media),
         "-map", f"0:a:{track}", "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", str(target)])
    return target


def _convert(media: Path, target: Path, track: int, convert: tuple[str, str, int]) -> bool:
    codec, extension, bitrate = convert
    out = target.with_suffix(extension)
    try:
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(media),
             "-map", f"0:a:{track}", "-vn", "-af", REBASE_TO_ZERO,
             "-c:a", codec, "-b:a", str(bitrate), str(out)])
    except subprocess.CalledProcessError:
        out.unlink(missing_ok=True)
        return False
    return out.is_file() and out.stat().st_size > 0


def _copy_track(media: Path, target: Path, track: int) -> bool:
    """Remux one track without touching its samples. False if it will not go."""
    try:
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(media),
             "-map", f"0:a:{track}", "-vn", "-c:a", "copy", str(target)])
    except subprocess.CalledProcessError:
        return False  # not every codec remuxes into every container
    return target.is_file() and target.stat().st_size > 0


def _acceptable_copy(copied: Path, capabilities: Capabilities | None) -> bool:
    """Whether to keep the copy, judged on what it actually came out as.

    The bitrate is measured from the remuxed file rather than read from the
    source, because a container often declares nothing: Matroska stores Opus
    with no bit_rate at all, and guessing high would convert every web video
    for no reason.
    """
    if capabilities is None:
        return True
    if capabilities.max_upload_bytes and copied.stat().st_size > capabilities.max_upload_bytes:
        return False
    if copied.suffix == ".ogg" and _start_time(copied) != 0.0:
        return False
    ceiling = capabilities.passthrough_max_bitrate
    if not ceiling:
        return True
    seconds = media_duration(copied)
    if seconds <= 0:
        return True
    return (copied.stat().st_size * 8 / seconds) <= ceiling


def _track_codec(media: Path, track: int) -> str:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", f"a:{track}",
         "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", str(media)],
        text=True, capture_output=True, check=False,
    )
    return (result.stdout or "").strip().splitlines()[0] if result.stdout.strip() else ""


# An Ogg Opus file whose first sample is not at time zero is refused by MAI and
# by libsndfile alike as "Supported file format but file is malformed", and
# ffmpeg leaves such an offset on some tracks it encodes. Every conversion is
# rebased to zero, padding or trimming the start so word times stay on the
# video clock, and a copied Ogg file with an offset is converted instead.
REBASE_TO_ZERO = "aresample=async=1:first_pts=0"


def _start_time(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=start_time",
         "-of", "default=nw=1:nk=1", str(path)],
        text=True, capture_output=True, check=False,
    )
    try:
        return float((result.stdout or "").strip().splitlines()[0])
    except (IndexError, ValueError):
        return 0.0


def plan_windows(
    audio: Path,
    work_dir: Path,
    capabilities: Capabilities,
    *,
    split_seconds: int | None = None,
) -> list[Window]:
    """Cut the audio when the backend needs it or the caller asked for it."""
    duration = media_duration(audio)
    seconds = capabilities.segment_seconds if capabilities.needs_segmentation else split_seconds
    if not seconds and capabilities.max_whole_file_seconds and duration > capabilities.max_whole_file_seconds:
        # The backend would rather take the whole file, but this one is past
        # what it accepts, and a refused request loses more than a seam does.
        seconds = int(capabilities.max_whole_file_seconds)
    if not seconds:
        return [Window(0, audio, 0.0, duration)]
    pieces = extract_chunks(audio, work_dir, chunk_seconds=seconds)
    windows: list[Window] = []
    cursor = 0.0
    for index, piece in enumerate(pieces):
        duration = media_duration(piece)
        windows.append(Window(index, piece, cursor, duration))
        cursor += duration
    return windows


def _run_windows(
    backend: Backend,
    windows: list[Window],
    *,
    context: str,
    language: str | None,
    concurrency: int,
) -> list[tuple[str, list[Word], dict[str, Any], float]]:
    batch = getattr(backend, "transcribe_all", None)
    if batch is not None:
        started = time.monotonic()
        results = batch(windows, context=context, language=language)
        elapsed = (time.monotonic() - started) / max(1, len(windows))
        return [(t, list(w), e, elapsed) for t, w, e in results]

    def one(window: Window):
        started = time.monotonic()
        text, words, extra = backend.transcribe(window, context=context, language=language)
        return text, list(words), extra, time.monotonic() - started

    workers = max(1, min(concurrency, backend.capabilities.max_parallel, len(windows)))
    if workers == 1:
        return [one(w) for w in windows]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, windows))


def transcribe(
    media: Path,
    out_dir: Path,
    *,
    provider: str,
    title: str,
    context: str = "",
    language: str | None = None,
    concurrency: int = 4,
    split_seconds: int | None = None,
    track: int = 0,
) -> dict[str, Any]:
    """Transcribe one media file and write words.json and transcript.md."""
    backend = load_backend(provider)
    backend.preflight()
    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir = out_dir / "work"

    timings: dict[str, float] = {}
    started = time.monotonic()
    audio = extract_audio(media, work_dir / "audio.wav", track=track,
                          capabilities=backend.capabilities)
    timings["extract_audio"] = round(time.monotonic() - started, 3)

    started = time.monotonic()
    windows = plan_windows(audio, work_dir, backend.capabilities, split_seconds=split_seconds)
    timings["plan_windows"] = round(time.monotonic() - started, 3)

    started = time.monotonic()
    results = _run_windows(
        backend, windows, context=context, language=language, concurrency=concurrency
    )
    timings["transcribe"] = round(time.monotonic() - started, 3)

    words: list[dict[str, Any]] = []
    segments: list[dict[str, Any]] = []
    detected = ""
    per_window: list[dict[str, Any]] = []
    for window, (text, window_words, extra, elapsed) in zip(windows, results):
        detected = detected or str(extra.get("language") or "")
        for word in window_words:
            shifted = word.shifted(window.offset)
            words.append({"index": len(words), **shifted.as_dict()})
        segments.append(
            {"start": window.offset, "end": window.offset + window.duration, "text": text}
        )
        per_window.append(
            {
                "index": window.index,
                "start": round(window.offset, 3),
                "duration": round(window.duration, 3),
                "words": len(window_words),
                "seconds": round(elapsed, 3),
            }
        )

    if not words:
        raise RuntimeError(
            f"{provider} returned no words. Check that the media has audible speech "
            f"and that {backend.capabilities.requirements} is satisfied."
        )

    payload = {
        "provider": provider,
        "audio_track": track,
        "uploaded_format": audio.suffix.lstrip("."),
        "uploaded_bytes": audio.stat().st_size,
        "language": detected,
        "context": context,
        "duration": round(sum(w.duration for w in windows), 3),
        "word_count": len(words),
        "windows": per_window,
        "timings": timings,
        "words": words,
    }
    (out_dir / "words.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [f"# Transcript: {title}", "", f"ASR hotwords: {context}", ""]
    for segment in segments:
        lines.append(
            f"[{format_timestamp(segment['start'])}–{format_timestamp(segment['end'])}] "
            f"{segment['text']}"
        )
    (out_dir / "transcript.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return payload
