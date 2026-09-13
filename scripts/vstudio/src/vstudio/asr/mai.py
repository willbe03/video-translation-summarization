"""MAI-Transcribe-2 ASR provider.

MAI-Transcribe is served through the Azure Speech Fast Transcription API
(``speechtotext/transcriptions:transcribe`` with ``enhancedMode``), not as a
deployable Foundry model. It returns per-word ``offsetMilliseconds`` and
``durationMilliseconds`` when ``modelOptions.timestamps = "word"`` is set, so a
single call replaces the separate local Qwen3-ASR + Qwen3-ForcedAligner pass.

Credentials are read from the environment so they inherit into the ``uv run``
subprocess from the Hermes session that launched it: ``MAI_SPEECH_KEY`` and
``MAI_SPEECH_REGION`` (with ``AZURE_SPEECH_KEY`` / ``AZURE_SPEECH_REGION`` as
fallbacks). Chunks are transcribed in parallel over plain HTTPS, so this path
scales with video length instead of saturating a single GPU.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Sequence

API_VERSION = "2025-10-15"
# Azure Speech rejects a phraseList keyword longer than this.
MAX_KEYWORD_CHARS = 50
MAI_MODEL_NAME = "MAI-Transcribe-2"
SECONDS_PER_MS = 0.001

_LANG_NAMES = {
    "en": "English",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "it": "Italian",
    "ru": "Russian",
    "pt": "Portuguese",
}


def _env(first: str, second: str) -> str | None:
    return os.environ.get(first) or os.environ.get(second)


def mai_region() -> str:
    return _env("MAI_SPEECH_REGION", "AZURE_SPEECH_REGION") or "westus"


def mai_endpoint() -> str:
    return (
        f"https://{mai_region()}.api.cognitive.microsoft.com/"
        f"speechtotext/transcriptions:transcribe"
    )


def mai_api_key() -> str:
    key = _env("MAI_SPEECH_KEY", "AZURE_SPEECH_KEY")
    if not key:
        raise RuntimeError(
            "MAI-Transcribe needs a Speech key. Set MAI_SPEECH_KEY (or AZURE_SPEECH_KEY) "
            "and MAI_SPEECH_REGION (or AZURE_SPEECH_REGION) in the Hermes environment."
        )
    return key


def _definition(*, phrases: Sequence[str], locale: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "enhancedMode": {
            "enabled": True,
            "model": MAI_MODEL_NAME,
            "modelOptions": {
                "timestamps": "word",
                "transcribeStyle": "clean",
            },
        }
    }
    if phrases:
        payload["phraseList"] = {"phrases": list(phrases)}
    if locale:
        payload["locales"] = [locale]
    return payload


class _MaiError(RuntimeError):
    pass


# Audio is uploaded as 16 kHz mono PCM, 32 KB per second of speech, so an hour
# is roughly 110 MB going up before any transcribing starts. A fixed timeout
# was fine for a five minute clip and failed a long one on a slow link.
BASE_TIMEOUT_SECONDS = 120
# 1 MB per second is a pessimistic 8 Mbit upload, which is what makes this a
# ceiling on waiting rather than an estimate of how long it will take.
SECONDS_PER_MEGABYTE = 1.0
MAX_TIMEOUT_SECONDS = 3600


# The endpoint reads the declared type, so a copied Opus track announced as
# audio/wav is rejected rather than transcribed.
_CONTENT_TYPES = {
    ".wav": "audio/wav", ".ogg": "audio/ogg", ".opus": "audio/ogg",
    ".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".flac": "audio/flac",
}


def content_type(audio: Path) -> str:
    return _CONTENT_TYPES.get(audio.suffix.lower(), "application/octet-stream")


def request_timeout(size_bytes: int) -> int:
    """How long to wait for one request, scaled by what has to be uploaded."""
    megabytes = size_bytes / (1024 * 1024)
    return int(min(MAX_TIMEOUT_SECONDS, BASE_TIMEOUT_SECONDS + megabytes * SECONDS_PER_MEGABYTE))


def transcribe_audio(
    audio: Path,
    *,
    phrases: Sequence[str] = (),
    locale: str | None = None,
    timeout_s: int | None = None,
    max_retries: int = 3,
) -> dict[str, Any]:
    """Transcribe a single audio file and return the full MAI JSON response.

    Retries transient HTTP 429 (throttle) and 503 (busy) responses with
    exponential backoff so parallel chunk transcription stays robust.
    """
    import time

    endpoint = mai_endpoint()
    if timeout_s is None:
        timeout_s = request_timeout(audio.stat().st_size)
    key = mai_api_key()
    boundary = "--hermes-mai-boundary-" + os.urandom(8).hex()
    definition = json.dumps(_definition(phrases=phrases, locale=locale), ensure_ascii=False)
    audio_bytes = audio.read_bytes()

    body = bytearray()
    body += f"--{boundary}\r\n".encode("utf-8")
    body += b'Content-Disposition: form-data; name="definition"\r\n'
    body += b"Content-Type: application/json\r\n\r\n"
    body += definition.encode("utf-8")
    body += b"\r\n"
    body += f"--{boundary}\r\n".encode("utf-8")
    body += (
        f'Content-Disposition: form-data; name="audio"; filename="audio{audio.suffix}"\r\n'
    ).encode("utf-8")
    body += f"Content-Type: {content_type(audio)}\r\n\r\n".encode("utf-8")
    body += audio_bytes
    body += f"\r\n--{boundary}--\r\n".encode("utf-8")

    quoted_endpoint = f"{endpoint}?api-version={urllib.parse.quote(API_VERSION)}"

    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        request = urllib.request.Request(
            quoted_endpoint,
            data=bytes(body),
            method="POST",
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Ocp-Apim-Subscription-Key": key,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                raw = response.read()
            break
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 503) and attempt < max_retries:
                last_error = exc
                time.sleep(2 ** attempt)
                continue
            detail = exc.read().decode("utf-8", errors="replace")
            raise _MaiError(f"MAI-Transcribe HTTP {exc.code}: {detail[:1000]}") from exc
        except urllib.error.URLError as exc:
            last_error = exc
            if attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            raise _MaiError(f"MAI-Transcribe request failed: {exc}") from exc
    else:
        raise _MaiError(f"MAI-Transcribe throttled: {last_error}")

    data = json.loads(raw.decode("utf-8"))
    if isinstance(data, dict) and data.get("error"):
        raise _MaiError(f"MAI-Transcribe error: {data['error']}")
    return data


def extract_words(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten MAI phrases into the downstream ``{text, start_time, end_time}`` shape (seconds)."""
    words: list[dict[str, Any]] = []
    for phrase in payload.get("phrases", []):
        for word in phrase.get("words", []):
            text = str(word.get("text", "")).strip()
            if not text:
                continue
            offset_ms = int(word.get("offsetMilliseconds") or 0)
            duration_ms = int(word.get("durationMilliseconds") or 0)
            words.append(
                {
                    "text": text,
                    "start_time": round(offset_ms * SECONDS_PER_MS, 6),
                    "end_time": round((offset_ms + duration_ms) * SECONDS_PER_MS, 6),
                }
            )
    return words


def combined_phrase_text(payload: dict[str, Any]) -> str:
    phrases = payload.get("combinedPhrases") or payload.get("phrases") or []
    return "".join(str(item.get("text", "")) for item in phrases).strip()


def detected_language(payload: dict[str, Any]) -> str:
    for phrase in payload.get("phrases") or []:
        locale = phrase.get("locale")
        if locale:
            return str(locale)
    return ""


def _map_language_name(payload: dict[str, Any]) -> str:
    code = detected_language(payload).split("-")[0].lower()
    return _LANG_NAMES.get(code, code.capitalize() or "English")


def _phrases_from_context(context: str) -> list[str]:
    """Turn the '-context' string into a phraseList for keyword biasing."""
    terms: list[str] = []
    if not context:
        return terms
    lower = context.casefold()
    for marker in ("vocabulary:", "terms:", "names:"):
        idx = lower.find(marker)
        if idx != -1:
            tail = context[idx + len(marker):]
            for delimiter in (", topic", " topic", "."):
                cut = tail.find(delimiter)
                if cut != -1:
                    tail = tail[:cut]
                    break
            terms = [piece.strip() for piece in tail.split(",") if piece.strip()]
            break
    if not terms:
        terms = [t.strip() for t in context.replace(";", ",").split(",") if len(t.strip()) >= 2]
    seen: list[str] = []
    for term in terms:
        # Azure 400s the whole job on an over-long keyword, so drop rather than
        # truncate: a clipped phrase is not the word anyone said.
        if term not in seen and 2 <= len(term) <= MAX_KEYWORD_CHARS:
            seen.append(term)
    return seen[:20]


def _media_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        text=True, capture_output=True, check=True,
    )
    return float(result.stdout.strip())


# --- Backend ---

from vstudio.asr.base import Backend, Capabilities, Window, Word  # noqa: E402


class MaiBackend:
    capabilities = Capabilities(
        name="mai",
        needs_segmentation=False,
        segment_seconds=None,
        # The Fast Transcription endpoint answers several requests at once; six
        # is what stayed clear of 429 across the runs this was built against.
        max_parallel=6,
        returns_word_times=True,
        requirements="MAI_SPEECH_KEY and MAI_SPEECH_REGION in the environment",
        prefers_whole_file=True,
        # Azure documents a hard 500 MB and five hour limit and recommends
        # staying under 300 MB and two hours for consistent speed. At 32 KB per
        # second that size guidance binds at about two and a half hours, so two
        # hours is the point where cutting beats risking the whole request.
        max_whole_file_seconds=7200,
        # Documented as accepted by the endpoint. Web video is almost always
        # Opus or AAC already, so the usual case uploads a tenth of the bytes
        # and skips a decode and re-encode of the speech entirely.
        # Measured against the endpoint rather than taken from its
        # documentation, which lists AAC among the accepted codecs but rejects
        # it in both an MP4 container and raw ADTS with "Format not
        # recognised". WAV, FLAC, MP3 and Opus in OGG all transcribe.
        accepts_codecs=(("opus", ".ogg"), ("mp3", ".mp3"), ("flac", ".flac")),
        # Speech carries no detail past this that a recogniser can use, so a
        # richer track is worth converting rather than uploading whole.
        passthrough_max_bitrate=192_000,
        # Opus stands in for the AAC the endpoint will not take. It is at least
        # as good per bit for speech, and 192k is generous for it. Two hours is
        # about 170 MB, inside every documented limit. MP3 follows because
        # libopus is optional in some ffmpeg builds while libmp3lame is
        # everywhere, and the endpoint takes both.
        convert_to=(("libopus", ".ogg", 192_000), ("libmp3lame", ".mp3", 192_000)),
        # The endpoint refuses anything over 500 MB; stopping well short leaves
        # room for the multipart envelope and a slow link.
        max_upload_bytes=300 * 1024 * 1024,
    )

    def preflight(self) -> None:
        mai_api_key()

    def report(self, *, live: bool = False) -> dict[str, Any]:
        """Extra detail for doctor. `live` spends one short request proving the
        endpoint answers, which is the only way to catch a stale key."""
        detail: dict[str, Any] = {"region": mai_region()}
        if not live:
            return detail
        import tempfile
        from vstudio.media import synth_probe_clip

        with tempfile.TemporaryDirectory() as scratch:
            clip = synth_probe_clip(Path(scratch) / "probe.wav")
            try:
                transcribe_audio(clip, timeout_s=60, max_retries=0)
                detail["endpoint_answered"] = True
            except RuntimeError as exc:
                detail["endpoint_answered"] = False
                detail["endpoint_error"] = str(exc)[:200]
        return detail

    def transcribe(self, window, *, context, language):
        payload = transcribe_audio(
            window.path,
            phrases=_phrases_from_context(context),
            locale=language,
        )
        words = [
            Word(w["text"], w["start_time"], w["end_time"]) for w in extract_words(payload)
        ]
        return (
            combined_phrase_text(payload),
            words,
            {"locale": detected_language(payload), "language": _map_language_name(payload)},
        )
