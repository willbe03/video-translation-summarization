from __future__ import annotations

import copy
import difflib
import re
import unicodedata
from pathlib import Path
from typing import Any, Mapping, Sequence

_CJK = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")
# Han and kana, the scripts written without spaces between words. Hangul is
# left out because Korean separates words with spaces.
_UNSPACED = re.compile(r"[\u3400-\u9fff\u3040-\u30ff]")
_UNIT = re.compile(r"[\u3400-\u9fff\u3040-\u30ff]|[^\u3400-\u9fff\u3040-\u30ff]+")
# Two characters is about the length of a Chinese or Japanese word.
UNSPACED_CHARACTERS_PER_WORD = 2
_NO_SPACE_BEFORE = set(",.!?;:%)]}，。！？；：、）】》」』")
_NO_SPACE_AFTER = set("([{（【《「『")
_CJK_PUNCTUATION = set("，。！？；：、）】》」』")


def offset_words(words: Sequence[Mapping[str, Any]], offset: float, *, chunk_id: int) -> list[dict[str, Any]]:
    shifted: list[dict[str, Any]] = []
    for index, word in enumerate(words):
        shifted.append(
            {
                "text": str(word["text"]),
                "start_time": round(float(word["start_time"]) + offset, 6),
                "end_time": round(float(word["end_time"]) + offset, 6),
                "chunk_id": chunk_id,
                "word_index": index,
            }
        )
    return shifted


def validate_boundaries(boundaries: Sequence[Mapping[str, Any]], word_count: int) -> bool:
    if word_count <= 0:
        raise ValueError("word_count must be positive")
    if not boundaries:
        raise ValueError("at least one subtitle boundary is required")
    expected = 0
    for boundary in boundaries:
        start = int(boundary["start_word"])
        end = int(boundary["end_word"])
        if start != expected:
            raise ValueError(f"subtitle boundaries must be contiguous; expected word {expected}, got {start}")
        if end < start or end >= word_count:
            raise ValueError(f"invalid subtitle boundary {start}..{end} for {word_count} words")
        expected = end + 1
    if expected != word_count:
        raise ValueError(f"subtitle boundaries must include final word {word_count - 1}")
    return True


# One rendered subtitle line at the shipped style holds roughly 42 Latin
# columns, and two lines per language is the most a viewer can read, so this is
# the width past which a cue is genuinely unreadable rather than merely long.
MAX_CUE_WIDTH = 84


def _split_overlong(
    start: int, end: int, limit: float, weights: Sequence[float]
) -> list[tuple[int, int]]:
    """Halve a range until every piece fits, splitting as evenly as possible."""
    total = sum(weights[start : end + 1])
    if total <= limit or start == end:
        return [(start, end)]
    middle, running = start, 0.0
    for index in range(start, end):
        running += weights[index]
        middle = index
        if running >= total / 2:
            break
    return (_split_overlong(start, middle, limit, weights)
            + _split_overlong(middle + 1, end, limit, weights))


def _match_token(value: str) -> str:
    """A word reduced to what both sides can agree on: letters and digits."""
    return re.sub(r"[^\w]", "", value, flags=re.UNICODE).casefold()


def split_units(text: str) -> list[str]:
    """Break text into the pieces two transcripts are compared on.

    Spaced text breaks at whitespace. Han and kana break into single
    characters, because a worker writes Chinese and Japanese without spaces
    while a recogniser may return them a character or a word at a time, and a
    character is the only unit both sides always share.
    """
    normalised = unicodedata.normalize("NFKC", text)
    return [unit for token in normalised.split() for unit in _UNIT.findall(token)]


def unit_weight(unit: str) -> float:
    """A unit's share of a word: none for punctuation, half for a Han or kana character.

    The long vowel mark weighs nothing, so respelling a word from hiragana
    into katakana, which adds marks without adding sounds, does not lengthen it.
    """
    if not _match_token(unit) or unit == "ー":
        return 0.0
    return 1 / UNSPACED_CHARACTERS_PER_WORD if _UNSPACED.fullmatch(unit) else 1.0


def lexical_weight(text: str) -> float:
    """How many words text counts as against the cue cap and the batch size."""
    return sum(unit_weight(unit) for unit in split_units(text))


def join_units(units: Sequence[str]) -> str:
    joined = ""
    for unit in units:
        if joined and not (_UNSPACED.match(joined[-1]) and _UNSPACED.match(unit[0])):
            joined += " "
        joined += unit
    return joined


def boundaries_from_lines(
    lines: Sequence[str], words: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Recover cue boundaries from a transcript rewritten with line breaks.

    Asking for word indices made the worker do arithmetic it is bad at and
    wrapped the answer in JSON that was mostly scaffolding. Asking it to put the
    words back with a break at each cue end is the same decision expressed in
    the medium it is good at, and it is checkable: the words it wrote are
    aligned against the words the recogniser heard, so a drop or an insertion
    shows up with a position rather than as a silent renumbering.
    """
    spoken: list[str] = []
    word_of_spoken: list[int] = []
    for index, word in enumerate(words):
        for unit in split_units(str(word["text"])):
            normalised = _match_token(unit)
            if normalised:
                spoken.append(normalised)
                word_of_spoken.append(index)
    written: list[str] = []
    owner_of_written: list[int] = []
    for index, line in enumerate(lines):
        for unit in split_units(line):
            normalised = _match_token(unit)
            if normalised:
                written.append(normalised)
                owner_of_written.append(index)

    # A word split across two units, such as a two character Chinese word,
    # belongs to the line its first matched unit was written on.
    owner: list[int | None] = [None] * len(words)
    matcher = difflib.SequenceMatcher(None, spoken, written, autojunk=False)
    matched = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            continue
        matched += i2 - i1
        for offset in range(i2 - i1):
            word = word_of_spoken[i1 + offset]
            if owner[word] is None:
                owner[word] = owner_of_written[j1 + offset]

    notes: list[str] = []
    dropped = len(set(word_of_spoken)) - sum(1 for line in owner if line is not None)
    if dropped:
        notes.append(f"{dropped} of {len(words)} words were not written back")
    added = len(written) - matched
    if added:
        notes.append(f"{added} word(s) in the answer are not in the transcript")

    # A word the worker dropped joins the cue before it, which is where the
    # reader would look for it anyway.
    carried = 0
    for index in range(len(owner)):
        if owner[index] is None:
            owner[index] = owner[index - 1] if index else 0
            carried += 1

    boundaries: list[dict[str, Any]] = []
    start = 0
    for index in range(1, len(owner) + 1):
        if index == len(owner) or owner[index] != owner[start]:
            boundaries.append({"start_word": start, "end_word": index - 1, "text": ""})
            start = index
    return boundaries, notes


def repair_boundaries(
    boundaries: Sequence[Mapping[str, Any]],
    word_count: int,
    *,
    max_words: int = 17,
    weights: Sequence[float] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Force a set of proposed cue ranges to cover every word exactly once.

    A worker that misses one index or overlaps two ranges by a word has made a
    counting slip, not a judgement error, and failing a whole stage over it
    wastes every other worker's answer. Gaps become their own cue and overlaps
    are trimmed, which keeps the invariant the timestamps depend on: each word
    belongs to exactly one cue, in order.

    `weights` is each word's share of `max_words`, one each when not given.
    """
    weights = list(weights) if weights is not None else [1.0] * word_count
    ordered = sorted(
        ({"start_word": int(b["start_word"]), "end_word": int(b["end_word"]),
          "text": str(b.get("text") or "")} for b in boundaries),
        key=lambda b: (b["start_word"], b["end_word"]),
    )
    repaired: list[dict[str, Any]] = []
    notes: list[str] = []
    cursor = 0
    for boundary in ordered:
        start, end = boundary["start_word"], min(boundary["end_word"], word_count - 1)
        if start > cursor:
            repaired.append({"start_word": cursor, "end_word": start - 1, "text": ""})
            notes.append(f"filled a gap at words {cursor}-{start - 1}")
        elif start < cursor:
            notes.append(f"trimmed an overlap at word {start}")
            start = cursor
        if end < start:
            notes.append(f"dropped an empty range ending at word {end}")
            continue
        pieces = _split_overlong(start, end, max_words, weights)
        if len(pieces) > 1:
            notes.append(
                f"split a {end - start + 1} word cue at word {start} into {len(pieces)}"
            )
        for index, (piece_start, piece_end) in enumerate(pieces):
            # Only an unsplit range keeps the proposed display text; a piece of
            # one would show words it no longer covers.
            text = boundary["text"] if len(pieces) == 1 else ""
            repaired.append({"start_word": piece_start, "end_word": piece_end, "text": text})
        cursor = end + 1
    if cursor < word_count:
        repaired.append({"start_word": cursor, "end_word": word_count - 1, "text": ""})
        notes.append(f"filled a tail gap at words {cursor}-{word_count - 1}")
    return repaired, notes


def join_words(words: Sequence[Mapping[str, Any]]) -> str:
    result = ""
    for word in words:
        text = str(word["text"]).strip()
        if not text:
            continue
        if not result:
            result = text
            continue
        previous = result[-1]
        no_space = (
            text[0] in _NO_SPACE_BEFORE
            or previous in _NO_SPACE_AFTER
            or (_CJK.search(previous) is not None and _CJK.search(text[0]) is not None)
            or (
                _CJK.search(text[0]) is not None
                and (
                    previous in _CJK_PUNCTUATION
                    or (previous in _NO_SPACE_BEFORE and _CJK.search(result[:-1]) is not None)
                )
            )
        )
        result += text if no_space else f" {text}"
    return result


def display_width(value: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in value)


def boundary_readability_issues(
    words: Sequence[Mapping[str, Any]],
    boundaries: Sequence[Mapping[str, Any]],
    *,
    max_duration: float = 7.0,
    max_width: int = MAX_CUE_WIDTH,
) -> list[str]:
    issues: list[str] = []
    for index, boundary in enumerate(boundaries):
        start_word = int(boundary["start_word"])
        end_word = int(boundary["end_word"])
        selected = words[start_word : end_word + 1]
        if not selected:
            issues.append(f"cue {index} has no words")
            continue
        duration = float(selected[-1]["end"]) - float(selected[0]["start"])
        text = str(boundary.get("text") or join_words(selected))
        width = display_width(text)
        if duration > max_duration:
            issues.append(f"cue {index} duration {duration:.2f}s exceeds {max_duration:.2f}s")
        if width > max_width:
            issues.append(f"cue {index} display width {width} exceeds {max_width}")
    return issues


def strip_punctuation(value: str) -> str:
    """Remove commas and periods while preserving other meaningful punctuation."""
    without = value.translate(str.maketrans("", "", ",.，。"))
    return re.sub(r"\s+", " ", without).strip()


def _lexical_tokens(value: str) -> list[str]:
    return re.findall(r"\w+", strip_punctuation(value).casefold(), flags=re.UNICODE)


def build_cues(
    words: Sequence[Mapping[str, Any]],
    boundaries: Sequence[Mapping[str, Any]],
    *,
    first_id: int = 0,
) -> list[dict[str, Any]]:
    """Turn word-index boundaries into timed cues.

    ``source`` is what the subtitle shows and ``source_exact`` is the ASR words
    untouched, so a later stage can prove nothing rewrote the speaker.
    """
    validate_boundaries(boundaries, len(words))
    cues: list[dict[str, Any]] = []
    for offset, boundary in enumerate(boundaries):
        start_word = int(boundary["start_word"])
        end_word = int(boundary["end_word"])
        selected = words[start_word : end_word + 1]
        aligned_text = join_words(selected)
        source = str(boundary.get("text") or aligned_text).strip()
        if sorted(_lexical_tokens(source)) != sorted(_lexical_tokens(aligned_text)):
            # The proposal added or dropped words. Keep what was actually heard.
            source = aligned_text
        cues.append(
            {
                "id": first_id + offset,
                "start": float(selected[0]["start"]),
                "end": float(selected[-1]["end"]),
                "source": strip_punctuation(source),
                "source_exact": aligned_text,
                "start_word": start_word,
                "end_word": end_word,
            }
        )
    return cues


def apply_translations(
    segments: Sequence[Mapping[str, Any]], translations: Mapping[int, str]
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for segment in segments:
        segment_id = int(segment["id"])
        if segment_id not in translations or not str(translations[segment_id]).strip():
            raise ValueError(f"missing translation for subtitle segment {segment_id}")
        item = copy.deepcopy(dict(segment))
        item["translation"] = strip_punctuation(str(translations[segment_id]))
        output.append(item)
    extras = set(translations) - {int(segment["id"]) for segment in segments}
    if extras:
        raise ValueError(f"translations contain unknown segment ids: {sorted(extras)}")
    return output


def ass_timestamp(seconds: float) -> str:
    centiseconds = max(0, round(float(seconds) * 100))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    secs, cs = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def _ass_text(value: str) -> str:
    return (
        value.replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\r", " ")
        .replace("\n", " ")
        .strip()
    )


def render_bilingual_ass(
    segments: Sequence[Mapping[str, Any]],
    output: Path,
    *,
    title: str,
    source_language: str,
    target_language: str,
    font_name: str = "Arial",
) -> None:
    header = f"""[Script Info]
Title: {title}
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Bilingual,{font_name},42,&H00FFFFFF,&H000000FF,&H00101010,&H90000000,0,0,0,0,100,100,0,0,1,2,1,2,60,60,45,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header.rstrip()]
    for segment in segments:
        source = _ass_text(strip_punctuation(str(segment["source"])))
        translation = _ass_text(strip_punctuation(str(segment["translation"])))
        text = f"{source}\\N{translation}"
        lines.append(
            f"Dialogue: 0,{ass_timestamp(float(segment['start']))},{ass_timestamp(float(segment['end']))},Bilingual,,0,0,0,,{text}"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
