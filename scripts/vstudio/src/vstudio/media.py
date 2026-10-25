from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

VIDEO_EXTENSIONS = {".mp4", ".webm", ".mkv", ".flv", ".mov", ".m4v"}


@dataclass(frozen=True)
class StreamChoice:
    stream_id: str
    height: int
    quality: str


def run(command: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        text=True,
        capture_output=capture,
    )


def ffmpeg_filter_listing_has(listing: str, filter_name: str) -> bool:
    pattern = rf"^\s*[.A-Z|]{{2,3}}\s+{re.escape(filter_name)}\s+"
    return any(re.match(pattern, line) for line in listing.splitlines())


def resolve_ass_ffmpeg() -> str:
    candidates = [
        "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg",
        "/usr/local/opt/ffmpeg-full/bin/ffmpeg",
        shutil.which("ffmpeg"),
    ]
    checked: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in checked or not Path(candidate).is_file():
            continue
        checked.add(candidate)
        result = subprocess.run(
            [candidate, "-hide_banner", "-filters"],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0 and ffmpeg_filter_listing_has(result.stdout, "ass"):
            return candidate
    raise RuntimeError(
        "no FFmpeg binary with the libass 'ass' filter was found; install ffmpeg-full or add it to PATH"
    )


def you_get_command(*args: str) -> list[str]:
    return [sys.executable, "-m", "vstudio.youget_runner", *args]


def is_youtube_url(url: str) -> bool:
    return bool(re.search(r"(?:youtube\.com|youtu\.be)", url, flags=re.IGNORECASE))


def youtube_fallback_metadata(url: str, cookies: Path | None = None) -> dict[str, Any]:
    """Use yt-dlp only to resolve URLs when you-get's extractor is stale."""
    command = [sys.executable, "-m", "yt_dlp", "--no-playlist", "-J"]
    if cookies:
        command.extend(["--cookies", str(cookies)])
    command.append(url)
    result = run(command, capture=True)
    data = json.loads(result.stdout)
    streams: dict[str, dict[str, Any]] = {}
    for item in data.get("formats", []):
        height = int(item.get("height") or 0)
        if not height or item.get("vcodec") == "none":
            continue
        format_id = str(item["format_id"])
        codec = str(item.get("vcodec") or "")
        streams[format_id] = {
            "quality": f"{height}P {codec}",
            "height": height,
            "has_audio": item.get("acodec") not in (None, "none"),
        }
    return {
        "url": url,
        "title": data.get("title"),
        "site": "YouTube",
        "streams": streams,
        "backend": "you-get-with-yt-dlp-url-resolution",
    }


def inspect_url(url: str, cookies: Path | None = None) -> dict[str, Any]:
    command = you_get_command("--json")
    if cookies:
        command.extend(["--cookies", str(cookies)])
    command.append(url)
    try:
        result = run(command, capture=True)
        return json.loads(result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        if is_youtube_url(url):
            return youtube_fallback_metadata(url, cookies)
        stdout = getattr(exc, "stdout", "") or ""
        raise RuntimeError(f"you-get could not inspect the video: {stdout[-1000:]}") from exc


def stream_height(stream_id: str, info: dict[str, Any]) -> int:
    if info.get("height"):
        return int(info["height"])
    quality = str(info.get("quality", ""))
    candidates = re.findall(r"(?<!\d)(\d{3,4})\s*[pP](?!\w)|[xX](\d{3,4})(?!\d)", quality)
    heights = [int(a or b) for a, b in candidates]
    if heights:
        return max(heights)
    id_matches = re.findall(r"(?<!\d)(\d{3,4})(?:[pP]|-)", stream_id)
    return max((int(value) for value in id_matches), default=0)


def choose_stream(metadata: dict[str, Any], max_height: int = 720) -> StreamChoice:
    streams = metadata.get("streams") or {}
    if not streams:
        raise RuntimeError("you-get did not report downloadable streams")

    ranked: list[tuple[int, int, int, int, str, str]] = []
    for stream_id, info in streams.items():
        height = stream_height(stream_id, info)
        if height > max_height:
            continue
        quality = str(info.get("quality", ""))
        codec_rank = 3 if "avc" in quality.lower() else 2 if "hevc" in quality.lower() else 1
        video_only_rank = 1 if info.get("has_audio") is False else 0
        known_height = 1 if height else 0
        ranked.append((known_height, height, video_only_rank, codec_rank, str(stream_id), quality))

    if not ranked:
        raise RuntimeError(f"no stream at or below {max_height}p is available")
    known, height, _, _, stream_id, quality = max(ranked)
    if not known:
        height = 0
    return StreamChoice(stream_id=stream_id, height=height, quality=quality)


# A page description is mostly sponsor copy and boilerplate, so mining is
# deliberately stingy: a wrong hotword is worse than a missing one.
MAX_DESCRIPTION_TERMS = 15
MAX_CONTEXT_CHARS = 600
# Azure Speech rejects a phraseList keyword longer than this.
MAX_KEYWORD_CHARS = 50
_NOISE = re.compile(r"https?://\S+|www\.\S+|[@#]\w+", re.I)
# Chapter lists are timestamp-led lines; their prose is navigation, not vocabulary.
_CHAPTER_LINE = re.compile(r"^\s*\d{1,3}:\d{2}(?::\d{2})?\b.*$", re.M)
# Capitalised runs of one to four words, and CJK runs of two to six characters.
# Spaces and tabs only. \s+ spanned newlines and glued a name to whatever
# started the next line, producing hotwords like "Tokamak LE\n\nSubscribe".
_PROPER = re.compile(r"\b[A-Z][\w.+#-]*(?:[ \t]+[A-Z][\w.+#-]*){0,3}\b")
# Common nouns that lead a sentence and are useless as hotwords on their own.
_STOP = {
    "the", "this", "that", "a", "an", "and", "but", "for", "with", "casters",
    "caster", "players", "player", "subscribe", "follow", "watch", "video",
    "intro", "outro", "game", "games", "part", "episode", "new", "my", "our",
    "in", "on", "at", "it", "we", "you", "they", "he", "she", "if", "so",
}
_CJK_RUN = re.compile(r"[\u3400-\u9fff]{2,6}")


# A description is usually a paragraph or two of substance followed by socials,
# sponsor copy and legal text. Everything from the first such marker is dropped.
_BOILERPLATE = re.compile(
    r"^.*\b(discord|twitch|patreon|instagram|twitter|tiktok|facebook|merch|"
    r"sponsor|subscribe|follow me|my socials|business enquir|copyright|"
    r"all rights reserved|kijkwijzer)\b.*$",
    re.I | re.M,
)
# Platform, scheduling and legal words that are never worth an ASR hotword.
_BOILERPLATE_TERMS = {
    "discord", "twitch", "patreon", "instagram", "twitter", "tiktok", "facebook",
    "youtube", "merch", "support", "tech", "live", "cet", "cest", "pst", "est",
    "utc", "gmt", "dutch", "english", "kijkwijzer", "copyright", "music",
    "outro", "intro", "thumbnail", "edited", "channel", "stream", "streaming",
}


def _before_boilerplate(description: str) -> str:
    """Drop the lines that are socials, sponsor copy or legal text.

    Cutting the whole description at the first such line threw away 60 percent
    of a real page, including the caster's name, because the socials block sat
    near the top. Filtering line by line keeps the substance wherever it sits.
    """
    kept = [line for line in description.splitlines() if not _BOILERPLATE.match(line)]
    return "\n".join(kept)


def description_terms(description: str, *, already: Sequence[str] = ()) -> list[str]:
    """Names and terms worth adding as ASR hotwords, from a page description."""
    if not description:
        return []
    cleaned = _CHAPTER_LINE.sub(" ", _NOISE.sub(" ", _before_boilerplate(description)))
    seen = {a.lower() for a in already}
    out: list[str] = []
    for match in list(_PROPER.finditer(cleaned)) + list(_CJK_RUN.finditer(cleaned)):
        term = match.group(0).strip().strip(".,;:!?-")
        words = term.split()
        # Drop a leading common noun such as "Casters Lowko" -> "Lowko".
        while len(words) > 1 and words[0].lower() in _STOP:
            words.pop(0)
        term = " ".join(words)
        if len(term) < 2 or term.lower() in seen:
            continue
        if len(words) == 1 and (term.lower() in _STOP or term.lower() in _BOILERPLATE_TERMS):
            continue
        if all(w.lower() in _BOILERPLATE_TERMS for w in words):
            continue
        seen.add(term.lower())
        out.append(term)
        if len(out) >= MAX_DESCRIPTION_TERMS:
            break
    return out


def infer_context(title: str, description: str = "") -> str:
    """ASR hotwords from the title, optionally enriched from the page description.

    With no description the output is byte-identical to what it always was, so
    existing jobs are unaffected.
    """
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_.+#-]{2,}|[\u3400-\u9fff]{2,}", title)
    unique = list(dict.fromkeys(tokens))[:20]
    # Mined names join the vocabulary list rather than trailing after it. The
    # MAI hotword extractor reads only up to the first full stop after the
    # marker, so anything appended past it never reaches the recogniser.
    terms = unique + [t for t in description_terms(description, already=unique)]
    # No term may contain a full stop. The hotword extractor reads only as far
    # as the first one after the marker, so a token like "WAR." silently
    # truncated the list to whatever preceded it.
    terms = [t.strip(".,;:") for t in terms]
    # Azure rejects any keyword over 50 characters with a 400 that fails the job.
    terms = [t for t in terms if t and "." not in t and len(t) <= MAX_KEYWORD_CHARS]
    terms = list(dict.fromkeys(terms))[:20]
    vocabulary = ", ".join(terms) if terms else title
    # "Vocabulary:" is a marker _phrases_from_context recognises. The previous
    # wording said "Likely names," with a comma, matched nothing, and made the
    # extractor split the whole sentence into over-long fragments.
    context = f'The video title is "{title}". Vocabulary: {vocabulary}.'
    return context[:MAX_CONTEXT_CHARS]


def fetch_source_metadata(url: str, cookies: Path | None = None) -> dict[str, Any]:
    """Best-effort page metadata via yt-dlp. Never raises, never fails a job."""
    # Invoke the module, not a bare binary: yt-dlp is installed in the project
    # venv and is not on PATH, so "yt-dlp" always failed and the best-effort
    # fallback silently swallowed it.
    command = [sys.executable, "-m", "yt_dlp", "-J", "--skip-download", "--no-playlist"]
    if cookies:
        command += ["--cookies", str(cookies)]
    command.append(url)
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=60, check=False)
        if result.returncode != 0 or not result.stdout.strip():
            return {}
        payload = json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        "description": str(payload.get("description") or "")[:2000],
        "uploader": str(payload.get("uploader") or ""),
        "published": str(payload.get("upload_date") or ""),
    }


def download_youtube_via_direct_urls(
    url: str,
    job_dir: Path,
    metadata: dict[str, Any],
    choice: StreamChoice,
    cookies: Path | None,
) -> Path:
    """Download a YouTube video with yt-dlp, falling back to anonymous clients.

    YouTube's current GVS PO-token / SABR experiment blocks the default
    ``android_vr`` client's DASH streams with 403 for many videos, even after a
    clean metadata extract. Progressive format 18 (360p) on ``android`` /
    ``mweb`` / ``tv_simply`` still works anonymously, and the unbounded
    ``web_safari`` client still serves 1080p DASH. We try the preferred stream
    first, then walk a client/format fallback chain before giving up. Node is
    enabled as the JS runtime because several clients need it to solve the ``n``
    code, and ``yt-dlp-ejs`` supplies the challenge solver.
    """
    output_template = job_dir / "video.%(ext)s"
    base = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-playlist",
        "--no-write-subs",
        "--no-write-auto-subs",
        "--js-runtimes",
        "node",
        "--merge-output-format",
        "mkv",
        "-o",
        str(output_template),
    ]
    if cookies:
        base.extend(["--cookies", str(cookies)])

    # (client_override, format_selector): tried in order until one downloads.
    attempts: list[tuple[str | None, str]] = []
    stream_info = metadata["streams"][choice.stream_id]
    primary_selector = (
        choice.stream_id
        if stream_info.get("has_audio")
        else f"{choice.stream_id}+bestaudio"
    )
    attempts.append((None, primary_selector))
    # Anonymous fallbacks that bypass the 403/gated DASH path. The unbounded
    # web_safari client is an opportunistic upgrade (up to 1080p) tried right
    # after the primary; if it fails it falls through to the reliable progressive
    # format 18 (360p) on android/mweb/tv_simply.
    attempts.append(("web_safari", "bv*+ba/b"))
    for client in ("android", "mweb", "tv_simply"):
        attempts.append((client, "18"))

    last_error: Exception | None = None
    for client, selector in attempts:
        command = list(base)
        if client:
            command.extend(["--extractor-args", f"youtube:player_client={client}"])
            command.extend(["-f", selector])
        else:
            command.extend(["-f", selector])
        command.append(url)
        try:
            run(command)
            candidates = [
                path
                for path in job_dir.iterdir()
                if path.is_file()
                and path.suffix.lower() in VIDEO_EXTENSIONS
                and path.name.startswith("video.")
                and path.stat().st_size > 0
            ]
            if candidates:
                return max(candidates, key=lambda path: path.stat().st_mtime)
        except subprocess.CalledProcessError as exc:
            last_error = exc
            # A failed attempt may leave a partial/zero-byte file; clean it so the
            # next selector's output doesn't collide.
            for path in list(job_dir.iterdir()):
                if path.is_file() and path.name.startswith("video."):
                    try:
                        path.unlink()
                    except OSError:
                        pass
            continue
    detail = str(last_error) if last_error else "no attempt ran"
    raise RuntimeError(f"yt-dlp could not download the video: {detail}")


AUDIO_ONLY_STREAM = StreamChoice(stream_id="audio-only", height=0, quality="audio only")
_DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _audio_file(job_dir: Path) -> Path | None:
    candidates = [p for p in job_dir.iterdir()
                  if p.is_file() and p.name.startswith("audio.") and p.stat().st_size > 0
                  and not p.name.endswith((".part", ".download", ".ytdl"))]
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


def _clear_audio(job_dir: Path) -> None:
    for path in list(job_dir.iterdir()):
        if path.is_file() and path.name.startswith("audio."):
            path.unlink(missing_ok=True)


def _yt_dlp_audio(url: str, job_dir: Path, cookies: Path | None) -> Path | None:
    # Quiet: progress lines land in the orchestrator's context ahead of the JSON
    # report, thousands of them on a long download. Errors still reach stderr.
    # bestaudio only: "/best" would fetch a muxed video into audio.* and report
    # it as audio-only, ignoring --max-height. A miss returns None instead, and
    # the caller takes the normal video path.
    command = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-write-subs",
               "--no-write-auto-subs", "--quiet", "--no-progress", "--js-runtimes", "node",
               "-f", "bestaudio", "-o", str(job_dir / "audio.%(ext)s")]
    if cookies:
        command.extend(["--cookies", str(cookies)])
    command.append(url)
    try:
        run(command)
    except subprocess.CalledProcessError:
        _clear_audio(job_dir)
        return None
    found = _audio_file(job_dir)
    if found is None or not _is_audio_only(found):
        _clear_audio(job_dir)
        return None
    return found


def _is_audio_only(path: Path) -> bool:
    try:
        probe = probe_media(path)
    except RuntimeError:
        return False
    return bool(probe["has_audio"] and not probe["has_video"] and probe["duration"])


def _container_suffix(path: Path) -> str:
    """`.webm` for a Matroska/WebM file, `.m4a` otherwise: DASH audio is one or the other."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=format_name", "-of",
         "default=noprint_wrappers=1:nokey=1", str(path)],
        text=True, capture_output=True, check=False,
    )
    name = (result.stdout or "").strip().lower()
    return ".webm" if ("webm" in name or "matroska" in name) else ".m4a"


def _dash_audio_urls(metadata: dict[str, Any]) -> list[str]:
    """The audio half of a DASH stream, which you-get lists as src = [video, audio]."""
    for info in (metadata.get("streams") or {}).values():
        src = info.get("src")
        if (isinstance(src, list) and len(src) == 2 and all(isinstance(part, list) for part in src)
                and src[1] and all(isinstance(u, str) and u.startswith("http") for u in src[1])):
            return list(src[1])
    return []


def _fetch_dash_audio(metadata: dict[str, Any], job_dir: Path) -> Path | None:
    import urllib.request

    urls = _dash_audio_urls(metadata)
    if not urls:
        return None
    extra = metadata.get("extra") if isinstance(metadata.get("extra"), dict) else {}
    headers = {"Referer": str(extra.get("referer") or metadata.get("url") or ""),
               "User-Agent": str(extra.get("ua") or _DEFAULT_UA)}
    target = job_dir / "audio.download"
    try:
        with target.open("wb") as out:
            for part in urls:
                request = urllib.request.Request(part, headers=headers)
                with urllib.request.urlopen(request, timeout=60) as response:
                    while chunk := response.read(1 << 20):
                        out.write(chunk)
        # The [video, audio] shape is a guess about the extractor; only a file
        # that probes as audio with no picture is accepted as the audio track.
        if not _is_audio_only(target):
            _clear_audio(job_dir)
            return None
        final = target.with_suffix(_container_suffix(target))
        target.replace(final)
        return final
    except Exception:
        # Any failure, including a connection dropped mid-body (IncompleteRead is
        # not an OSError), falls back to the video path with nothing left behind.
        _clear_audio(job_dir)
        return None


def download_audio(
    url: str,
    job_dir: Path,
    *,
    cookies: Path | None = None,
    metadata: dict[str, Any] | None = None,
) -> Path | None:
    """Fetch only the audio track, for a job that will never look at the picture.

    A summary needs the speech and nothing else, and the video stream is most of
    the bytes. Returns None when the site offers no separate audio track or the
    fetch fails, so the caller can fall back to the full download.
    """
    metadata = metadata if metadata is not None else inspect_url(url, cookies)
    job_dir.mkdir(parents=True, exist_ok=True)
    if metadata.get("backend") == "you-get-with-yt-dlp-url-resolution":
        return _yt_dlp_audio(url, job_dir, cookies)
    return _fetch_dash_audio(metadata, job_dir) or (
        _yt_dlp_audio(url, job_dir, cookies) if is_youtube_url(url) else None)


def download_video(
    url: str,
    job_dir: Path,
    *,
    cookies: Path | None = None,
    max_height: int = 720,
    metadata: dict[str, Any] | None = None,
) -> tuple[Path, dict[str, Any], StreamChoice]:
    metadata = metadata if metadata is not None else inspect_url(url, cookies)
    choice = choose_stream(metadata, max_height=max_height)
    job_dir.mkdir(parents=True, exist_ok=True)
    if metadata.get("backend") == "you-get-with-yt-dlp-url-resolution":
        video = download_youtube_via_direct_urls(url, job_dir, metadata, choice, cookies)
        return video, metadata, choice

    command = you_get_command(
        "--force",
        "--no-caption",
        "--format",
        choice.stream_id,
        "--output-dir",
        str(job_dir),
        "--output-filename",
        "video",
    )
    if cookies:
        command.extend(["--cookies", str(cookies)])
    command.append(url)
    run(command)

    candidates = [
        path
        for path in job_dir.iterdir()
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS and path.name.startswith("video")
    ]
    if not candidates:
        raise RuntimeError("you-get completed but no video file was found")
    return max(candidates, key=lambda path: path.stat().st_mtime), metadata, choice


def extract_chunks(video: Path, temp_dir: Path, chunk_seconds: int = 45) -> list[Path]:
    chunks_dir = temp_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    pattern = chunks_dir / "chunk-%05d.wav"
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            "-f",
            "segment",
            "-segment_time",
            str(chunk_seconds),
            "-reset_timestamps",
            "1",
            str(pattern),
        ]
    )
    chunks = sorted(chunks_dir.glob("chunk-*.wav"))
    if not chunks:
        raise RuntimeError("ffmpeg produced no audio chunks")
    return chunks


def media_duration(path: Path) -> float:
    result = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture=True,
    )
    return float(result.stdout.strip())


def format_timestamp(seconds: float) -> str:
    total = max(0, round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def take_frame(manifest_path: Path, timestamp: str) -> Path:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    frames = manifest.setdefault("screenshots", [])
    if len(frames) >= 10:
        raise RuntimeError("the 10-screenshot limit has been reached for this video")
    if manifest.get("audio_only"):
        raise RuntimeError("this job fetched the audio only, so there is no picture to grab. "
                           "Create the job again without --audio-only to get the video.")
    video = Path(manifest["media"])
    if not video.exists():
        raise RuntimeError("video has already been deleted")
    frames_dir = manifest_path.parent / "frames"
    frames_dir.mkdir(exist_ok=True)
    safe_timestamp = re.sub(r"[^0-9A-Za-z]+", "-", timestamp).strip("-") or "frame"
    output = frames_dir / f"{len(frames) + 1:02d}-{safe_timestamp}.jpg"
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            timestamp,
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(output),
        ]
    )
    frames.append({"timestamp": timestamp, "path": str(output)})
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output


def cleanup(manifest_path: Path, *, keep_video: bool = False) -> list[Path]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    removed: list[Path] = []
    video = Path(manifest["media"])
    # A local source is referenced where the user keeps it. Deleting it would
    # destroy their own recording, so only downloaded media is ever removed.
    borrowed = manifest.get("source_kind") == "local"
    if video.exists() and not keep_video and not borrowed:
        video.unlink()
        removed.append(video)
    for name in ("temp", "asr/work"):
        scratch = manifest_path.parent / name
        if scratch.exists():
            shutil.rmtree(scratch)
            removed.append(scratch)
    frames_dir = manifest_path.parent / "frames"
    if frames_dir.exists():
        shutil.rmtree(frames_dir)
        removed.append(frames_dir)
    if frames_dir in removed or not frames_dir.exists():
        # The frame files are gone, so the paths pointing at them are dangling.
        # Keep the count for the record and drop the broken references.
        screenshots = manifest.get("screenshots") or []
        if screenshots:
            manifest["screenshots_taken"] = len(screenshots)
        manifest["screenshots"] = []
    manifest["temporary_media_cleaned"] = True
    manifest["media_retained"] = video.exists()
    if borrowed:
        manifest["source_preserved"] = True
    manifest["cleaned"] = not video.exists()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return removed


def media_resolution(path: Path) -> tuple[int, int] | None:
    """Return (width, height) of the first video stream, or None if there is none."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    match = re.match(r"^\s*(\d+)x(\d+)", result.stdout or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


def asr_weights_present(model_id: str) -> Path | None:
    """Return the cached snapshot directory for a HuggingFace repo, if any.

    Honours HF_HUB_CACHE and HF_HOME the same way huggingface_hub does, so the
    pre-flight check does not quietly look in the wrong place for anyone who
    moved their cache.
    """
    override = os.environ.get("HF_HUB_CACHE")
    if override:
        roots = [Path(override)]
    elif os.environ.get("HF_HOME"):
        roots = [Path(os.environ["HF_HOME"]) / "hub"]
    else:
        roots = [Path.home() / ".cache" / "huggingface" / "hub"]
    folder = "models--" + model_id.replace("/", "--")
    for root in roots:
        candidate = root / folder
        if candidate.is_dir() and any(candidate.rglob("*.safetensors")):
            return candidate
    return None


def synth_probe_clip(target: Path, seconds: float = 1.0) -> Path:
    """Write a short tone used only to prove an ASR endpoint answers."""
    run([
        "ffmpeg",
        "-v", "error", "-y", "-f", "lavfi",
        "-i", f"sine=frequency=440:duration={seconds}",
        "-ar", "16000", "-ac", "1", str(target),
    ])
    return target


def is_local_source(value: str) -> bool:
    """True when the argument names a file on disk rather than a URL."""
    if "://" in value:
        return False
    return Path(value).expanduser().is_file()


def probe_media(path: Path) -> dict[str, Any]:
    """Stream inventory for any container ffmpeg can read.

    No extension allowlist: ffmpeg decides what it can decode, so mp4, mkv,
    webm, mov, avi, ts, mp3, m4a, flac, opus and wav all arrive here the same
    way. Only the absence of audio makes a file unusable.
    """
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "stream=codec_type,width,height", "-show_entries", "format=duration",
            "-of", "json", str(path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe cannot read {path}: {result.stderr.strip()[:200]}")
    payload = json.loads(result.stdout or "{}")
    streams = payload.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    duration = payload.get("format", {}).get("duration")
    return {
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
        "has_video": video is not None,
        "width": int(video["width"]) if video and video.get("width") else None,
        "height": int(video["height"]) if video and video.get("height") else None,
        "duration": round(float(duration), 3) if duration else None,
    }


# A track marked with any of these is not the programme audio: it is narration
# for the visually impaired, a director's commentary, or a karaoke mix. Picking
# one transcribes the wrong thing and nothing downstream would notice.
_UNUSABLE_DISPOSITIONS = ("comment", "visual_impaired", "descriptions", "karaoke")


def audio_streams(path: Path) -> list[dict[str, Any]]:
    """Every audio track in the file, with what is known about each."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_streams",
         "-of", "json", str(path)],
        text=True, capture_output=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe cannot read the audio tracks of {path}")
    streams = []
    for order, stream in enumerate(json.loads(result.stdout or "{}").get("streams") or []):
        tags = stream.get("tags") or {}
        disposition = stream.get("disposition") or {}
        streams.append({
            "track": order,                       # index among audio streams, for -map 0:a:N
            "stream_index": int(stream.get("index", order)),
            "codec": stream.get("codec_name") or "",
            "channels": int(stream.get("channels") or 0),
            "language": str(tags.get("language") or "").lower(),
            "title": str(tags.get("title") or ""),
            "default": bool(disposition.get("default")),
            "flags": sorted(k for k, v in disposition.items() if v and k in _UNUSABLE_DISPOSITIONS),
        })
    return streams


# Containers tag a track with ISO 639-2 while a locale like ja-JP carries
# ISO 639-1, and the two do not share a prefix: "jpn" does not start with "ja".
# Matching on one or the other silently picked the wrong track.
_LANGUAGE_ALIASES = {
    "en": {"en", "eng"}, "zh": {"zh", "zho", "chi", "cmn", "yue"},
    "ja": {"ja", "jpn"}, "ko": {"ko", "kor"}, "fr": {"fr", "fra", "fre"},
    "de": {"de", "deu", "ger"}, "es": {"es", "spa"}, "it": {"it", "ita"},
    "ru": {"ru", "rus"}, "pt": {"pt", "por"}, "nl": {"nl", "nld", "dut"},
    "ar": {"ar", "ara"}, "hi": {"hi", "hin"}, "tr": {"tr", "tur"},
    "pl": {"pl", "pol"}, "sv": {"sv", "swe"}, "id": {"id", "ind"},
    "vi": {"vi", "vie"}, "th": {"th", "tha"}, "uk": {"uk", "ukr"},
}


def language_forms(value: str) -> set[str]:
    """Every code that names the same language as `value`, or just it."""
    code = str(value or "").replace("_", "-").split("-")[0].strip().lower()
    if not code:
        return set()
    for forms in _LANGUAGE_ALIASES.values():
        if code in forms:
            return forms
    return {code}


def choose_audio_stream(
    streams: Sequence[Mapping[str, Any]], *, language: str | None = None
) -> dict[str, Any]:
    """Pick the track to transcribe and say why.

    A file with one track needs no thought. With several the order is: drop the
    tracks that are not the programme, prefer one whose language was asked for,
    then the one the container marks default, then the first. The reason is
    reported so an orchestrator can question the choice when a file carries two
    plausible languages.
    """
    if not streams:
        raise RuntimeError("the file has no audio stream, so there is nothing to transcribe")
    if len(streams) == 1:
        return {**streams[0], "reason": "the only audio track"}

    usable = [s for s in streams if not s["flags"]] or list(streams)
    wanted = language_forms(language or "")
    if wanted:
        name = sorted(wanted)[0]
        matching = [s for s in usable if s["language"] in wanted]
        if matching:
            best = min(matching, key=lambda s: (not s["default"], s["track"]))
            return {**best, "reason": f"the only {name} track" if len(matching) == 1
                    else f"the default {name} track"}
    marked = [s for s in usable if s["default"]]
    if len(marked) == 1:
        return {**marked[0], "reason": "the track the file marks as default"}
    best = min(usable, key=lambda s: (not s["default"], s["track"]))
    return {**best, "reason": "the first usable track, with nothing to prefer another by"}


def title_from_filename(path: Path) -> str:
    """A readable title from a filename, for job naming and ASR hotwords."""
    stem = re.sub(r"[._]+", " ", path.stem)
    stem = re.sub(r"\s{2,}", " ", stem).strip()
    return stem or path.name
