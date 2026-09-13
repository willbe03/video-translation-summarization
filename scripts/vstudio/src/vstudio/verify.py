"""Mechanical checks on a finished translation.

Every acceptance run of this pipeline ended with an agent writing the same
throwaway difflib script to answer the same seven questions. Six of them have
one right answer and belong in code. The seventh, whether a source edit stayed
within the proofreader's brief, needs judgement, so this computes the
substitution pairs and leaves only the verdict to a reader.
"""

from __future__ import annotations

import difflib
import math
import re
from typing import Any, Mapping, Sequence

from vstudio.cues import (
    ass_timestamp,
    join_units,
    lexical_weight,
    split_units,
    strip_punctuation,
    unit_weight,
)

# The segmentation prompt enforces this; exceeding it means a cue slipped past.
MAX_ALIGNED_WORDS = 17
# Isolated interpolations are routine. A long run means the aligner collapsed.
REPAIRED_RUN_WARN = 10
BANNED_PUNCTUATION = re.compile(r"[,.，。]")


def cue_coherence_issues(cues: Sequence[Mapping[str, Any]]) -> list[str]:
    english_dangling = (
        "firstly",
        "secondly",
        "then",
        "however",
        "therefore",
        "moreover",
        "furthermore",
        "because",
        "although",
        "whereas",
        "meanwhile",
        "for example",
        "such as",
    )
    chinese_dangling = (
        "首先",
        "其次",
        "然后",
        "但是",
        # 不过 is deliberately absent. As a bare suffix it matches idioms that
        # legitimately end a cue, such as 再合适不过, and every false positive
        # burns a whole proofread repair round on a cue the model correctly
        # refuses to change.
        "因为",
        "所以",
        "而且",
        "此外",
        "例如",
        "比如",
        "以及",
        "或者",
        "如果",
        "虽然",
        "然而",
        "因此",
    )
    issues: list[str] = []
    for cue in cues:
        cue_id = int(cue["id"])
        for field in ("source", "translation"):
            text = strip_punctuation(str(cue.get(field, ""))).strip()
            lowered = text.casefold()
            if any(lowered == word or lowered.endswith(f" {word}") for word in english_dangling):
                issues.append(f"cue {cue_id} {field} ends with a dangling transition")
            if any(text.endswith(word) for word in chinese_dangling):
                issues.append(f"cue {cue_id} {field} ends with a dangling transition")
    return issues


def _words(text: str) -> list[str]:
    """Tokens for comparison, with the punctuation the pipeline strips removed.

    `source_exact` keeps the commas and periods that `source` legitimately
    loses, so comparing raw tokens reported a substitution on almost every cue
    and buried the handful that matter.

    Han and kana compare a character at a time, so a proofreader who swaps a
    Japanese comma for a space has not added a word.
    """
    stripped = BANNED_PUNCTUATION.sub("", text)
    return [unit for unit in split_units(stripped) if unit_weight(unit)]


def _reading(unit: str) -> str:
    """A unit with katakana folded to hiragana, so a respelling lines up with what it replaced."""
    return "".join(chr(ord(ch) - 0x60) if "ァ" <= ch <= "ヶ" else ch for ch in unit)


def _respellings(cue_id: int, exact: Sequence[str], shown: Sequence[str]) -> list[dict[str, Any]]:
    """Runs of units that read the same but are written differently."""
    out: list[dict[str, Any]] = []
    start = None
    for index in range(len(exact) + 1):
        differs = index < len(exact) and exact[index] != shown[index]
        if differs and start is None:
            start = index
        elif not differs and start is not None:
            out.append({"id": cue_id, "was": join_units(exact[start:index]),
                        "now": join_units(shown[start:index]), "added_words": 0})
            start = None
    return out


def source_substitutions(cues: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every place the displayed source diverged from the untouched ASR words."""
    out: list[dict[str, Any]] = []
    for cue in cues:
        exact, shown = _words(str(cue.get("source_exact", ""))), _words(str(cue.get("source", "")))
        if exact == shown:
            continue
        readings = difflib.SequenceMatcher(
            None, [_reading(u) for u in exact], [_reading(u) for u in shown]
        )
        for tag, i1, i2, j1, j2 in readings.get_opcodes():
            if tag == "equal":
                out.extend(_respellings(int(cue["id"]), exact[i1:i2], shown[j1:j2]))
                continue
            added = sum(map(unit_weight, shown[j1:j2])) - sum(map(unit_weight, exact[i1:i2]))
            out.append({
                "id": int(cue["id"]),
                "was": join_units(exact[i1:i2]),
                "now": join_units(shown[j1:j2]),
                "added_words": max(0, math.floor(added)),
            })
    return out


def _differs_from_asr(cue: Mapping[str, Any], words: Sequence[Mapping[str, Any]]) -> bool:
    """Whether a cue is not timed exactly like the first and last word it covers."""
    if cue.get("start_word") is None or cue.get("end_word") is None:
        return False
    first, last = int(cue["start_word"]), int(cue["end_word"])
    if not 0 <= first <= last < len(words):
        return True
    return (abs(float(cue["start"]) - float(words[first]["start"])) > 1e-6
            or abs(float(cue["end"]) - float(words[last]["end"])) > 1e-6)


def longest_repaired_run(words: Sequence[Mapping[str, Any]]) -> int:
    """Longest consecutive stretch of interpolated word timings."""
    longest = run = 0
    for word in words:
        run = run + 1 if word.get("repaired") else 0
        longest = max(longest, run)
    return longest


def verify_translation(
    manifest: Mapping[str, Any],
    final: Sequence[Mapping[str, Any]],
    draft: Sequence[Mapping[str, Any]] | None,
    words: Sequence[Mapping[str, Any]] | None,
    *,
    ass_dialogue_count: int | None = None,
    ass_codec: str | None = None,
    ass_times: Sequence[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    failures: list[str] = []
    warnings: list[str] = []
    details: dict[str, Any] = {"cues": len(final)}

    if not final:
        return {"ok": False, "failures": ["cues/final.json is empty"], "warnings": [], "details": details}

    ids = [int(c["id"]) for c in final]
    if ids != list(range(len(final))):
        failures.append("cue ids are not a contiguous run from zero")

    if ass_codec is not None and ass_codec != "ass":
        failures.append(f"the subtitle file is not an ass stream (ffprobe reports {ass_codec!r})")
    if ass_dialogue_count is not None:
        details["ass_dialogue_lines"] = ass_dialogue_count
        if ass_dialogue_count != len(final):
            failures.append(
                f"the ass file has {ass_dialogue_count} dialogue lines but there are {len(final)} cues"
            )
    if ass_times is not None and len(ass_times) == len(final):
        mistimed = [
            c["id"] for c, (start, end) in zip(final, ass_times)
            if (start, end) != (ass_timestamp(float(c["start"])), ass_timestamp(float(c["end"])))
        ]
        if mistimed:
            failures.append(
                f"{len(mistimed)} ass dialogue line(s) are not timed like their cues: {mistimed[:5]}"
            )

    bad_duration = [c["id"] for c in final if float(c["end"]) <= float(c["start"])]
    if bad_duration:
        failures.append(f"{len(bad_duration)} cue(s) have non-positive duration: {bad_duration[:5]}")

    # Where two people talk at once the transcript overlaps, and the cues keep
    # that timing rather than invent one nobody heard.
    details["overlapping_cues"] = sum(
        1 for i in range(len(final) - 1)
        if float(final[i]["end"]) > float(final[i + 1]["start"]) + 1e-6
    )

    if words:
        off_asr = [c["id"] for c in final if _differs_from_asr(c, words)]
        if off_asr:
            failures.append(
                f"{len(off_asr)} cue timestamp(s) differ from the ASR word times: {off_asr[:5]}"
            )

    if draft:
        by_id = {int(c["id"]): c for c in draft}
        moved = [
            c["id"] for c in final
            if int(c["id"]) in by_id
            and (float(c["start"]), float(c["end"]))
            != (float(by_id[int(c["id"])]["start"]), float(by_id[int(c["id"])]["end"]))
        ]
        details["timestamps_moved"] = len(moved)
        if moved:
            failures.append(f"{len(moved)} cue timestamp(s) changed after the draft: {moved[:5]}")

    punctuated = [
        c["id"] for c in final
        if BANNED_PUNCTUATION.search(str(c["source"]) + str(c["translation"]))
    ]
    if punctuated:
        failures.append(f"{len(punctuated)} cue(s) contain a comma or period: {punctuated[:5]}")

    overlong = [
        c["id"] for c in final
        if c.get("end_word") is not None
        and lexical_weight(str(c.get("source_exact", ""))) > MAX_ALIGNED_WORDS
    ]
    if overlong:
        failures.append(f"{len(overlong)} cue(s) exceed {MAX_ALIGNED_WORDS} aligned words: {overlong[:5]}")

    subs = source_substitutions(final)
    added = [s for s in subs if s["added_words"] > 0]
    details["substitutions"] = len(subs)
    details["cues_with_drift"] = len({s["id"] for s in subs})
    if added:
        failures.append(
            f"{len(added)} source edit(s) added words, which the proofread prompt forbids: "
            + "; ".join(f"cue {s['id']} {s['was']!r} -> {s['now']!r}" for s in added[:5])
        )
    if subs and not added:
        warnings.append(
            f"{len(subs)} source substitution(s) need adjudication; each must be an ASR typo, "
            "capitalization, proper-name, terminology, or filler fix"
        )

    dangling = cue_coherence_issues(final)
    if dangling:
        warnings.append(f"{len(dangling)} possible dangling transition(s); check them by eye")

    if words:
        run = longest_repaired_run(words)
        details["longest_repaired_run"] = run
        if run > REPAIRED_RUN_WARN:
            warnings.append(
                f"longest interpolated timestamp run is {run} words, which suggests the aligner "
                "collapsed a chunk tail; re-run the stage with a shorter --chunk-seconds"
            )

    details["usage_report"] = manifest.get("usage_report")
    details["cleaned"] = {
        k: manifest.get(k) for k in ("temporary_media_cleaned", "video_retained", "source_preserved")
    }
    return {"ok": not failures, "failures": failures, "warnings": warnings,
            "details": details, "adjudicate": subs}
