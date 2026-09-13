"""The stages an agent drives: plan work, then apply what the workers returned.

Nothing here calls a model. `plan_*` slices an artifact into task directories;
`apply_*` reads the answers, checks them against what was asked, and writes the
next artifact. Anything a check can catch is caught here rather than left for a
model to notice.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from vstudio import prompts, tasks
from vstudio.cues import (
    apply_translations,
    boundary_readability_issues,
    boundaries_from_lines,
    build_cues,
    lexical_weight,
    render_bilingual_ass,
    repair_boundaries,
    strip_punctuation,
    validate_boundaries,
)

# Every stage has a range its batch may sit in. Below the floor a worker has
# lost the surrounding speech it needs to judge a break or keep a term
# consistent. Above the ceiling it is writing enough in one answer that a
# truncation costs more than another round would have. The preferred size is
# what to use when nothing is known about how many workers can run at once.
@dataclass(frozen=True)
class Batching:
    floor: int
    preferred: int
    ceiling: int


# A floor of 150 words is about a minute of speech, enough to see whole
# sentences either side of a break. Twenty cues is roughly the same span, which
# is what carries a pronoun chain and a repeated term through a translation.
SEGMENT_BATCH = Batching(floor=150, preferred=400, ceiling=1200)
TRANSLATE_BATCH = Batching(floor=20, preferred=40, ceiling=150)
PROOFREAD_BATCH = Batching(floor=20, preferred=60, ceiling=200)

CONCURRENCY_ENV = "VSTUDIO_CONCURRENCY"


def batch_size(total: int, batching: Batching, concurrency: int | None) -> int:
    """Spread the work across every worker the harness will run at once.

    This is not a cap. Given how many workers can run together, the batch is
    whatever divides the work evenly among them, so a short video uses fewer,
    larger batches and a long one uses more. The two bounds are quality
    limits rather than scheduling ones: an even split that would fall below the
    floor is widened, which leaves slots idle on purpose because a worker
    without context segments worse, and one that would pass the ceiling is
    narrowed, which spills into a second wave.
    """
    if not concurrency or concurrency < 1:
        return batching.preferred
    even = -(-total // concurrency)
    return max(batching.floor, min(batching.ceiling, even))


def _concurrency(explicit: int | None) -> int | None:
    if explicit is not None:
        return explicit
    configured = os.environ.get(CONCURRENCY_ENV)
    try:
        return int(configured) if configured else None
    except ValueError:
        return None


def _wave_plan(count: int, concurrency: int | None) -> dict[str, Any]:
    """Tell the orchestrator whether one round of dispatch will do."""
    if not concurrency or count <= concurrency:
        return {"tasks_planned": count, "waves": 1}
    return {
        "tasks_planned": count,
        "waves": -(-count // concurrency),
        "dispatch_at_once": concurrency,
        "note": (
            f"this harness runs {concurrency} workers at a time, so dispatch the brief "
            f"list in groups of {concurrency} and wait for each group before starting the "
            f"next. Run apply once, after the last group."
        ),
    }
# Neighbours shown around a proofreading batch so a fix at a seam can see what
# it has to agree with.
PROOFREAD_CONTEXT_CUES = 4
SUMMARY_SECONDS_PER_TASK = 900


def _read(job: Path, *parts: str) -> Any:
    path = job.joinpath(*parts)
    if not path.is_file():
        raise RuntimeError(f"{path} does not exist yet. Run the stage that writes it first.")
    return json.loads(path.read_text(encoding="utf-8"))


def _write(job: Path, value: Any, *parts: str) -> Path:
    path = job.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


_TERMINAL = (".", "?", "!", "。", "？", "！")


def batch_words(words: Sequence[Mapping[str, Any]], size: int) -> list[tuple[int, int]]:
    """Contiguous word ranges of about `size`, ending at a sentence where possible.

    Size is measured with `lexical_weight`, so a recogniser that returns
    Japanese a character at a time still gets batches of about `size` words.
    """
    weights = [lexical_weight(str(word.get("text", ""))) for word in words]
    ranges: list[tuple[int, int]] = []
    start = 0
    total = len(words)
    while start < total:
        end, filled = start, weights[start]
        while end + 1 < total and filled + weights[end + 1] <= size:
            end += 1
            filled += weights[end]
        if end < total - 1:
            # Walk back looking for a sentence end, but never shorten a batch by
            # more than a quarter: landing mid-clause costs less than halving
            # the context a worker gets to read.
            floor = max(start, end - max(1, (end - start + 1) // 4))
            for index in range(end, floor - 1, -1):
                if str(words[index].get("text", "")).rstrip().endswith(_TERMINAL):
                    end = index
                    break
        ranges.append((start, end))
        start = end + 1
    return ranges


def batch_items(items: Sequence[Any], size: int) -> list[list[Any]]:
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


# --- segment ---


def plan_segment(
    job: Path,
    *,
    language: str,
    size: int | None = None,
    concurrency: int | None = None,
) -> dict[str, Any]:
    asr = _read(job, "asr", "words.json")
    words = asr["words"]
    tasks.reset_stage(job, "segment")
    limit = _concurrency(concurrency)
    weight = math.ceil(sum(lexical_weight(str(word["text"])) for word in words))
    size = size or batch_size(weight, SEGMENT_BATCH, limit)
    ranges = batch_words(words, size)
    instructions = prompts.segment(language=language)
    directories = []
    for index, (start, end) in enumerate(ranges):
        payload = [
            {
                "index": offset,
                "text": word["text"],
                "start": round(float(word["start"]), 3),
                "end": round(float(word["end"]), 3),
            }
            for offset, word in enumerate(words[start : end + 1])
        ]
        directories.append(
            tasks.write_task(
                job, "segment", index, len(ranges),
                instructions=instructions,
                payload=payload,
                meta={"first_word": start, "last_word": end},
            )
        )
    return tasks.plan(job, "segment", directories, words=len(words),
                      words_per_task=size, **_wave_plan(len(ranges), limit))


def apply_segment(job: Path) -> dict[str, Any]:
    asr = _read(job, "asr", "words.json")
    words = asr["words"]
    boundaries: list[dict[str, Any]] = []
    repairs: list[str] = []
    for directory in tasks.task_dirs(job, "segment"):
        meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
        offset = int(meta["first_word"])
        count = int(meta["last_word"]) - offset + 1
        lines = tasks.read_lines_result(directory)
        local, notes = boundaries_from_lines(lines, words[offset : offset + count])
        if not local:
            raise RuntimeError(f"{directory} proposed no cue boundaries. Re-dispatch it.")
        repaired, repair_notes = repair_boundaries(
            local, count, max_words=prompts.MAX_WORDS_PER_CUE,
            weights=[lexical_weight(str(w["text"])) for w in words[offset : offset + count]],
        )
        local = repaired
        repairs.extend(f"{directory.name}: {note}" for note in notes + repair_notes)
        validate_boundaries(local, count)
        for item in local:
            boundaries.append(
                {
                    "start_word": int(item["start_word"]) + offset,
                    "end_word": int(item["end_word"]) + offset,
                    "text": str(item.get("text") or ""),
                }
            )
    validate_boundaries(boundaries, len(words))
    cues = build_cues(words, boundaries)
    _write(job, cues, "cues", "cues.json")
    issues = boundary_readability_issues(words, boundaries)
    _write(job, issues, "cues", "readability.json")
    return {"cues": len(cues), "readability_warnings": len(issues),
            "boundary_repairs": repairs}


# --- translate ---


def plan_translate(
    job: Path,
    *,
    source_language: str,
    target_language: str,
    instructions: str = "",
    page_text: str = "",
    size: int | None = None,
    concurrency: int | None = None,
) -> dict[str, Any]:
    cues = _read(job, "cues", "cues.json")
    tasks.reset_stage(job, "translate")
    limit = _concurrency(concurrency)
    size = size or batch_size(len(cues), TRANSLATE_BATCH, limit)
    body = prompts.translate(
        source_language=source_language,
        target_language=target_language,
        instructions=instructions,
        page_text=page_text,
    )
    batches = batch_items(cues, size)
    directories = [
        tasks.write_task(
            job, "translate", index, len(batches),
            instructions=body,
            payload=[
                {
                    "id": int(c["id"]),
                    "start": round(float(c["start"]), 2),
                    "end": round(float(c["end"]), 2),
                    "source": c["source"],
                }
                for c in batch
            ],
        )
        for index, batch in enumerate(batches)
    ]
    return tasks.plan(job, "translate", directories, cues=len(cues),
                      cues_per_task=size, **_wave_plan(len(batches), limit))


def apply_translate(job: Path) -> dict[str, Any]:
    cues = _read(job, "cues", "cues.json")
    translations: dict[int, str] = {}
    uncertainties: list[dict[str, Any]] = []
    for directory in tasks.task_dirs(job, "translate"):
        rows, questions, junk = parse_id_lines(tasks.read_lines_result(directory))
        if junk:
            raise RuntimeError(
                f"{directory} wrote {len(junk)} line(s) that do not start with a cue id, "
                f"the first being {junk[0][:60]!r}. Re-dispatch it."
            )
        translations.update(rows)
        uncertainties.extend(questions)
    absent = tasks.missing((int(c["id"]) for c in cues), translations)
    if absent:
        raise RuntimeError(
            f"{len(absent)} cues came back untranslated, ids {absent[:20]}"
            f"{' ...' if len(absent) > 20 else ''}. Re-dispatch the task whose payload holds them."
        )
    translated = apply_translations(cues, translations)
    _write(job, translated, "cues", "translated.json")
    _write(job, [{**u, "stage": "translate"} for u in uncertainties], "cues", "uncertainties.json")
    return {"cues": len(translated), "uncertainties": len(uncertainties)}


def parse_id_lines(
    lines: Sequence[str],
) -> tuple[dict[int, str], list[dict[str, Any]], list[str]]:
    """Split `<id><tab><text>` lines from `? <id> <question>` lines and junk.

    Whitespace rather than a literal tab, because a worker asked for a tab
    sometimes writes spaces and the id is unambiguous either way.
    """
    rows: dict[int, str] = {}
    questions: list[dict[str, Any]] = []
    junk: list[str] = []
    for line in lines:
        if line.startswith("?"):
            parts = line[1:].strip().split(None, 1)
            if parts and parts[0].rstrip(":.").isdigit():
                questions.append({"id": int(parts[0].rstrip(":.")),
                                  "question": parts[1].strip() if len(parts) > 1 else ""})
            else:
                questions.append({"id": None, "question": line[1:].strip()})
            continue
        head, _, tail = line.partition("\t")
        if not tail:
            head, _, tail = line.partition(" ")
        head = head.strip().rstrip(":.")
        if not head.isdigit() or not tail.strip():
            junk.append(line)
            continue
        rows[int(head)] = tail.strip()
    return rows, questions, junk


def _normalize_uncertainties(value: Any) -> list[dict[str, Any]]:
    """Accept the shape the prompt asked for and the bare strings models also send."""
    out: list[dict[str, Any]] = []
    for item in value or []:
        if isinstance(item, dict):
            out.append({"id": item.get("id"), "question": str(item.get("question") or item)})
        elif str(item).strip():
            out.append({"id": None, "question": str(item).strip()})
    return out


# --- proofread ---


def plan_proofread(
    job: Path,
    *,
    source_language: str,
    target_language: str,
    instructions: str = "",
    page_text: str = "",
    size: int | None = None,
    only: Sequence[int] | None = None,
    concurrency: int | None = None,
) -> dict[str, Any]:
    cues = _read(job, "cues", "translated.json")
    by_id = {int(c["id"]): c for c in cues}
    selected = [c for c in cues if only is None or int(c["id"]) in set(only)]
    if not selected:
        raise RuntimeError("no cues selected for proofreading")
    # A targeted rerun sits beside the full pass rather than replacing it, and
    # apply lets the later task win for every cue it covers.
    root = tasks.stage_root(job, "proofread")
    if only is None or not root.is_dir():
        tasks.reset_stage(job, "proofread")
    offset = len(tasks.task_dirs(job, "proofread")) if root.is_dir() else 0
    limit = _concurrency(concurrency)
    size = size or batch_size(len(selected), PROOFREAD_BATCH, limit)
    body = prompts.proofread(
        source_language=source_language,
        target_language=target_language,
        instructions=instructions,
        page_text=page_text,
    )
    order = [int(c["id"]) for c in cues]
    position = {cue_id: index for index, cue_id in enumerate(order)}
    batches = batch_items(selected, size)
    directories = []
    for index, batch in enumerate(batches):
        ids = [int(c["id"]) for c in batch]
        first, last = position[ids[0]], position[ids[-1]]
        before = order[max(0, first - PROOFREAD_CONTEXT_CUES) : first]
        after = order[last + 1 : last + 1 + PROOFREAD_CONTEXT_CUES]
        payload = [_proof_row(by_id[i], context=True) for i in before]
        payload += [_proof_row(c) for c in batch]
        payload += [_proof_row(by_id[i], context=True) for i in after]
        directories.append(
            tasks.write_task(
                job, "proofread", offset + index, offset + len(batches),
                instructions=body,
                payload=payload,
                meta={"ids": ids},
            )
        )
    return tasks.plan(job, "proofread", directories, cues=len(selected),
                      cues_per_task=size, **_wave_plan(len(batches), limit))


def _proof_row(cue: Mapping[str, Any], *, context: bool = False) -> dict[str, Any]:
    row = {"id": int(cue["id"]), "source": cue["source"], "draft": cue["translation"]}
    if context:
        row["context"] = True
    return row


def apply_proofread(job: Path) -> dict[str, Any]:
    cues = _read(job, "cues", "translated.json")
    translated = {int(c["id"]): dict(c) for c in cues}
    by_id = {int(c["id"]): dict(c) for c in cues}
    unchecked: list[str] = []
    uncertainties: list[dict[str, Any]] = []
    for directory in tasks.task_dirs(job, "proofread"):
        result = tasks.read_result(directory)
        assigned = set(json.loads((directory / "meta.json").read_text(encoding="utf-8"))["ids"])
        uncertainties = [u for u in uncertainties if u["id"] not in assigned]
        if isinstance(result, dict):
            uncertainties.extend(_normalize_uncertainties(result.get("uncertainties")))
            checked = result.get("checked")
            if not isinstance(checked, int) or checked < len(assigned):
                unchecked.append(f"{directory.name} reported checked={checked} of {len(assigned)}")
        # Tasks apply in the order they were planned, so a targeted rerun starts
        # its cues over from the translation instead of stacking on the full pass.
        for cue_id in assigned:
            by_id[cue_id] = dict(translated[cue_id])
        for row in tasks.rows(result, "cues"):
            cue_id = int(row["id"])
            if cue_id not in assigned:
                raise RuntimeError(
                    f"{directory} returned cue {cue_id}, which was context only. "
                    f"A context row must never be edited."
                )
            target = by_id[cue_id]
            for key, field in (("source", "source"), ("translation", "translation")):
                value = str(row.get(key) or "").strip()
                if value and strip_punctuation(value) != target[field]:
                    target[field] = strip_punctuation(value)
    changed = sum(by_id[i][field] != translated[i][field]
                  for i in by_id for field in ("source", "translation"))
    final = [by_id[int(c["id"])] for c in cues]
    _write(job, final, "cues", "final.json")
    existing = _read(job, "cues", "uncertainties.json") if (job / "cues" / "uncertainties.json").is_file() else []
    # Rebuilt rather than appended, so applying again does not repeat the notes.
    kept = [u for u in existing if u.get("stage") != "proofread"]
    _write(job, kept + [{**u, "stage": "proofread"} for u in uncertainties],
           "cues", "uncertainties.json")
    return {"cues": len(final), "fields_changed": changed, "coverage_warnings": unchecked}


# --- render ---


def render(job: Path, *, font: str = "Arial") -> Path:
    manifest = _read(job, "manifest.json")
    source = job / "cues" / "final.json"
    if not source.is_file():
        source = job / "cues" / "translated.json"
    cues = json.loads(source.read_text(encoding="utf-8"))
    output = job / "out" / f"{job.name}.ass"
    render_bilingual_ass(
        cues,
        output,
        title=str(manifest.get("title") or job.name),
        source_language=str(manifest.get("source_language") or ""),
        target_language=str(manifest.get("target_language") or ""),
        font_name=font,
    )
    return output


# --- summary ---


def plan_summarize(
    job: Path,
    *,
    language: str,
    instructions: str = "",
    seconds: int = SUMMARY_SECONDS_PER_TASK,
    concurrency: int | None = None,
) -> dict[str, Any]:
    asr = _read(job, "asr", "words.json")
    segments = _transcript_segments(job, asr)
    tasks.reset_stage(job, "summarize")
    body = prompts.summarize(language=language, instructions=instructions)
    parts: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    anchor = 0.0
    for segment in segments:
        if current and float(segment["start"]) - anchor >= seconds:
            parts.append(current)
            current, anchor = [], float(segment["start"])
        current.append(segment)
    if current:
        parts.append(current)
    directories = [
        tasks.write_task(
            job, "summarize", index, len(parts),
            instructions=body,
            payload=part,
            meta={"start": part[0]["start"], "end": part[-1]["end"]},
        )
        for index, part in enumerate(parts)
    ]
    return tasks.plan(job, "summarize", directories, parts=len(parts),
                      **_wave_plan(len(parts), _concurrency(concurrency)))


# Without cues, the transcript is cut into fixed stretches so a summary worker
# still has something to cite. Thirty seconds is short enough that a citation
# points at a recognisable moment and long enough to hold a whole thought.
FALLBACK_SEGMENT_SECONDS = 30.0


def _transcript_segments(job: Path, asr: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Transcript lines to summarize, from cues when they exist and words otherwise."""
    cues_path = job / "cues" / "cues.json"
    if cues_path.is_file():
        cues = json.loads(cues_path.read_text(encoding="utf-8"))
        return [
            {"start": round(float(c["start"]), 2), "end": round(float(c["end"]), 2), "text": c["source"]}
            for c in cues
        ]
    # Not from the ASR windows: a provider that takes the whole file reports one
    # window spanning the video, which would make the entire transcript a single
    # row and leave nothing for the summary stage to split or cite.
    segments: list[dict[str, Any]] = []
    bucket: list[str] = []
    anchor = end = 0.0
    for word in asr["words"]:
        start = float(word["start"])
        if bucket and start - anchor >= FALLBACK_SEGMENT_SECONDS:
            segments.append({"start": round(anchor, 2), "end": round(end, 2),
                             "text": " ".join(bucket)})
            bucket, anchor = [], start
        if not bucket:
            anchor = start
        bucket.append(str(word["text"]))
        end = float(word["end"])
    if bucket:
        segments.append({"start": round(anchor, 2), "end": round(end, 2), "text": " ".join(bucket)})
    if not segments:
        raise RuntimeError("the ASR artifact holds no transcript segments to summarize")
    return segments


def apply_summarize(job: Path) -> dict[str, Any]:
    notes: list[dict[str, Any]] = []
    for index, directory in enumerate(tasks.task_dirs(job, "summarize")):
        meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
        text = tasks.read_text_result(directory)
        notes.append({"part": index, "start": meta["start"], "end": meta["end"], "notes": text})
    _write(job, notes, "summary", "notes.json")
    return {"parts": len(notes)}


def plan_synthesize(job: Path, *, language: str, instructions: str = "") -> dict[str, Any]:
    notes = _read(job, "summary", "notes.json")
    manifest = _read(job, "manifest.json")
    tasks.reset_stage(job, "synthesize")
    directory = tasks.write_task(
        job, "synthesize", 0, 1,
        instructions=prompts.synthesize(
            title=str(manifest.get("title") or job.name),
            language=language,
            instructions=instructions,
        ),
        payload=notes,
    )
    return tasks.plan(job, "synthesize", [directory], parts=len(notes))


def apply_synthesize(job: Path) -> Path:
    directory = tasks.task_dirs(job, "synthesize")[0]
    text = tasks.read_text_result(directory)
    output = job / "out" / "summary.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text + "\n", encoding="utf-8")
    return output
