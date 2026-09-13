"""ASR backend registry and the runner that schedules them.

The runner owns everything that is the same whatever transcribes the audio:
deciding whether to cut the media, running windows concurrently, shifting word
times back onto the video's own clock, and writing the artifacts. A backend
only has to turn one piece of audio into words.
"""

from __future__ import annotations

import json
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


def extract_audio(media: Path, target: Path) -> Path:
    """One mono 16 kHz WAV, which is what every backend here wants."""
    target.parent.mkdir(parents=True, exist_ok=True)
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(media),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(target),
    ])
    return target


def plan_windows(
    audio: Path,
    work_dir: Path,
    capabilities: Capabilities,
    *,
    split_seconds: int | None = None,
) -> list[Window]:
    """Cut the audio when the backend needs it or the caller asked for it."""
    seconds = capabilities.segment_seconds if capabilities.needs_segmentation else split_seconds
    if not seconds:
        return [Window(0, audio, 0.0, media_duration(audio))]
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
) -> dict[str, Any]:
    """Transcribe one media file and write words.json and transcript.md."""
    backend = load_backend(provider)
    backend.preflight()
    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir = out_dir / "work"

    timings: dict[str, float] = {}
    started = time.monotonic()
    audio = extract_audio(media, work_dir / "audio.wav")
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
