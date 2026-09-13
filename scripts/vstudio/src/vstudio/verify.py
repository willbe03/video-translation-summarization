"""Mechanical checks on a finished translation.

Every acceptance run of this pipeline ended with an agent writing the same
throwaway difflib script to answer the same seven questions. Six of them have
one right answer and belong in code. The seventh, whether a source edit stayed
within the proofreader's brief, needs judgement, so this computes the
substitution pairs and leaves only the verdict to a reader.
"""

from __future__ import annotations

import difflib
import re
from typing import Any, Mapping, Sequence

from vstudio.cues import strip_punctuation

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
    """
    stripped = BANNED_PUNCTUATION.sub("", text)
    return re.findall(r"[^\s]+", stripped)


def source_substitutions(cues: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every place the displayed source diverged from the untouched ASR words."""
    out: list[dict[str, Any]] = []
    for cue in cues:
        exact, shown = _words(str(cue.get("source_exact", ""))), _words(str(cue.get("source", "")))
        if exact == shown:
            continue
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, exact, shown).get_opcodes():
            if tag == "equal":
                continue
            was, now = " ".join(exact[i1:i2]), " ".join(shown[j1:j2])
            out.append({
                "id": int(cue["id"]),
                "was": was,
                "now": now,
                "added_words": max(0, (j2 - j1) - (i2 - i1)),
            })
    return out


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

    bad_duration = [c["id"] for c in final if float(c["end"]) <= float(c["start"])]
    if bad_duration:
        failures.append(f"{len(bad_duration)} cue(s) have non-positive duration: {bad_duration[:5]}")

    overlaps = [
        final[i + 1]["id"]
        for i in range(len(final) - 1)
        if float(final[i]["end"]) > float(final[i + 1]["start"]) + 1e-6
    ]
    if overlaps:
        failures.append(f"{len(overlaps)} cue(s) overlap the previous one: {overlaps[:5]}")

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
        and int(c["end_word"]) - int(c["start_word"]) + 1 > MAX_ALIGNED_WORDS
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
