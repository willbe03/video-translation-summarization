"""The stages after verify: post-check resolves overlapping cues, post-process times them for display.

Verify holds every cue to the times the recogniser heard. These stages run once
it has passed and change what reaches the screen, so their output is the
delivered subtitle file rather than anything verify reads.

Post-check finds cues that overlap in time and hands each stretch, with a few
neighbours for context, to a deep worker that may retime, merge or delete
lines. Its answers are checked here and the stage repeats on whatever still
overlaps, up to a fixed number of rounds. Post-process needs no model: it snaps
neighbours separated by a short gap together and pads each resulting group so
a line appears slightly before its speech and lingers after it.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from collections import Counter
from itertools import accumulate
from pathlib import Path
from typing import Any, Mapping, Sequence

from vstudio import pipeline, prompts, tasks
from vstudio.cues import join_units, speech_span, split_units, strip_punctuation, unit_weight

POSTCHECK_CONTEXT_CUES = 3
POSTCHECK_MAX_ROUNDS = 3
POSTCHECK_BATCH = pipeline.Batching(floor=1, preferred=6, ceiling=20)
# Workers read times to the millisecond, so anything finer is not an overlap
# anyone could see or fix.
EPSILON = 0.0005
LONG_LINE_SECONDS = 7.0

POSTPROCESS_ENV = "VSTUDIO_POSTPROCESS"
SNAP_GAP_ENV = "VSTUDIO_SNAP_GAP_MS"
LEAD_ENV = "VSTUDIO_LEAD_MS"
LAG_ENV = "VSTUDIO_LAG_MS"
DEFAULT_SNAP_GAP_MS = 300
DEFAULT_LEAD_MS = 100
DEFAULT_LAG_MS = 200


# --- post-check ---


def overlap_clusters(cues: Sequence[Mapping[str, Any]]) -> list[list[int]]:
    """Index runs in which each cue starts before an earlier one in the run has ended."""
    clusters: list[list[int]] = []
    current: list[int] = []
    reach = -math.inf
    for index, cue in enumerate(cues):
        if current and float(cue["start"]) < reach - EPSILON:
            current.append(index)
        else:
            if len(current) > 1:
                clusters.append(current)
            current = [index]
            reach = -math.inf
        reach = max(reach, float(cue["end"]))
    if len(current) > 1:
        clusters.append(current)
    return clusters


def overlapping_ids(cues: Sequence[Mapping[str, Any]]) -> list[int]:
    return [int(cues[i]["id"]) for cluster in overlap_clusters(cues) for i in cluster]


def _groups(clusters: Sequence[Sequence[int]]) -> list[tuple[int, int]]:
    """Index ranges a worker may rewrite, joined only where two touch.

    A range is bounded by the lines just outside it, so those must belong to no
    other range. Context rows further out may be another worker's, which is
    harmless because they are only read.
    """
    ranges: list[tuple[int, int]] = []
    for cluster in clusters:
        first, last = cluster[0], cluster[-1]
        if ranges and first - ranges[-1][1] <= 1:
            ranges[-1] = (ranges[-1][0], last)
        else:
            ranges.append((first, last))
    return ranges


def _fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _state(job: Path) -> dict[str, Any]:
    """Where post-check stands, reset whenever final.json has been rebuilt since."""
    final = job / "cues" / "final.json"
    if not final.is_file():
        raise RuntimeError(f"{final} does not exist yet. Run coherence, render and verify first.")
    path = job / "cues" / "postcheck.json"
    state = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    fingerprint = _fingerprint(final)
    if state.get("final_sha256") != fingerprint:
        for name in ("checked.json", "delivered.json"):
            (job / "cues" / name).unlink(missing_ok=True)
        state = {"final_sha256": fingerprint, "round": 0, "remaining": []}
    return state


def _current(job: Path) -> list[dict[str, Any]]:
    checked = job / "cues" / "checked.json"
    name = "checked.json" if checked.is_file() else "final.json"
    return sorted(pipeline._read(job, "cues", name), key=_time_order)


def _words_row(cue: Mapping[str, Any], words: Sequence[Mapping[str, Any]] | None) -> list[list[Any]]:
    if not words or cue.get("start_word") is None or cue.get("end_word") is None:
        return []
    selected = words[int(cue["start_word"]) : int(cue["end_word"]) + 1]
    return [[str(w.get("text", "")), round(float(w["start"]), 3), round(float(w["end"]), 3)]
            for w in speech_span(selected)]


def _row(cue: Mapping[str, Any], group: int, words=None, *, context: bool = False) -> dict[str, Any]:
    row: dict[str, Any] = {"group": group, "id": int(cue["id"]),
                           "start": round(float(cue["start"]), 3), "end": round(float(cue["end"]), 3),
                           "source": cue["source"], "translation": cue["translation"]}
    if context:
        row["context"] = True
    else:
        row["words"] = _words_row(cue, words)
    return row


def plan_postcheck(
    job: Path,
    *,
    source_language: str,
    target_language: str,
    size: int | None = None,
    concurrency: int | None = None,
) -> dict[str, Any]:
    state = _state(job)
    cues = _current(job)
    ranges = _groups(overlap_clusters(cues))
    if not ranges:
        tasks.reset_stage(job, "postcheck")
        pipeline._write(job, cues, "cues", "checked.json")
        pipeline._write(job, {**state, "remaining": [], "planned": None}, "cues", "postcheck.json")
        return {"stage": "postcheck", "round": state["round"], "overlapping_cues": 0,
                "dispatch": [], "next": f"vstudio postprocess {job}"}
    remaining = overlapping_ids(cues)
    if state["round"] >= POSTCHECK_MAX_ROUNDS:
        pipeline._write(job, cues, "cues", "checked.json")
        pipeline._write(job, {**state, "remaining": remaining}, "cues", "postcheck.json")
        raise RuntimeError(
            f"post-check ran {state['round']} rounds and {len(remaining)} cue(s) still overlap: "
            f"{remaining[:20]}. Report them, then run `vstudio postprocess {job}`, which "
            f"leaves those lines as they are."
        )
    words_path = job / "asr" / "words.json"
    words = (json.loads(words_path.read_text(encoding="utf-8"))["words"]
             if words_path.is_file() else None)
    duration = _duration(job)
    reach = list(accumulate((float(c["end"]) for c in cues), max))
    groups = []
    for number, (first, last) in enumerate(ranges):
        before = range(max(0, first - POSTCHECK_CONTEXT_CUES), first)
        after = range(last + 1, min(len(cues), last + 1 + POSTCHECK_CONTEXT_CUES))
        rows = [_row(cues[i], number, context=True) for i in before]
        rows += [_row(cues[i], number, words) for i in range(first, last + 1)]
        rows += [_row(cues[i], number, context=True) for i in after]
        groups.append({
            "group": number,
            "ids": [int(cues[i]["id"]) for i in range(first, last + 1)],
            "lower": round(reach[first - 1], 3) if first > 0 else 0.0,
            "upper": (round(float(cues[last + 1]["start"]), 3) if last + 1 < len(cues)
                      else round(max(duration or 0.0, *(float(c["end"]) for c in cues[first:])), 3)),
            "rows": rows,
        })
    tasks.reset_stage(job, "postcheck")
    limit = pipeline._concurrency(concurrency)
    size = size or pipeline.batch_size(len(groups), POSTCHECK_BATCH, limit)
    body = prompts.postcheck(source_language=source_language, target_language=target_language)
    batches = pipeline.batch_items(groups, size)
    directories = [
        tasks.write_task(
            job, "postcheck", index, len(batches),
            instructions=body,
            payload=[row for group in batch for row in group["rows"]],
            meta={"groups": [{k: v for k, v in g.items() if k != "rows"} for g in batch]},
        )
        for index, batch in enumerate(batches)
    ]
    planned = tasks.plan(job, "postcheck", directories, round=state["round"] + 1,
                         max_rounds=POSTCHECK_MAX_ROUNDS, overlapping_cues=len(remaining),
                         groups=len(groups), groups_per_task=size,
                         **pipeline._wave_plan(len(batches), limit))
    pipeline._write(job, {**state, "remaining": remaining, "planned": _plan_stamp(job)},
                    "cues", "postcheck.json")
    return planned


def _plan_stamp(job: Path) -> str:
    return (tasks.stage_root(job, "postcheck") / "planned_at").read_text(encoding="utf-8").strip()


def _duration(job: Path) -> float | None:
    manifest = pipeline._read(job, "manifest.json")
    value = manifest.get("duration")
    try:
        return float(value) if value else None
    except (TypeError, ValueError):
        return None


def _units(text: str) -> Counter:
    return Counter(unit.casefold() for unit in split_units(strip_punctuation(text)) if unit_weight(unit))


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def resolve_group(
    answer: Mapping[str, Any] | None,
    group: Mapping[str, Any],
    by_id: Mapping[int, Mapping[str, Any]],
    *,
    round_number: int,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Check one worker's replacement for a group; return the new cues or why it was refused."""
    if answer is None:
        return None, "no answer for this group"
    lines = answer.get("lines")
    if not isinstance(lines, list) or not lines:
        return None, "lines is missing or empty"
    assigned = [int(i) for i in group["ids"]]
    covered: list[int] = []
    out: list[dict[str, Any]] = []
    for position, line in enumerate(lines):
        if not isinstance(line, dict):
            return None, f"line {position} is not an object"
        raw = [_cue_id(i) for i in line.get("from")] if isinstance(line.get("from"), list) else None
        if raw is None or None in raw:
            return None, f"line {position} needs from as a list of cue ids"
        if not raw:
            return None, f"line {position} has an empty from"
        if len(set(raw)) != len(raw):
            return None, f"line {position} repeats an id in from: {raw}"
        if any(i not in assigned for i in raw):
            return None, f"line {position} takes ids outside the group: {raw}"
        sources = sorted(raw, key=assigned.index)
        start, end = _number(line.get("start")), _number(line.get("end"))
        if start is None or end is None or round(end, 3) <= round(start, 3):
            return None, f"line {position} needs numeric start before end"
        start, end = round(start, 3), round(end, 3)
        spoken_from = min(_spoken(by_id[i])[0] for i in sources)
        spoken_to = max(_spoken(by_id[i])[1] for i in sources)
        if end <= spoken_from + EPSILON or start >= spoken_to - EPSILON:
            return None, (f"line {position} at {start}-{end} no longer covers its speech at "
                          f"{round(spoken_from, 3)}-{round(spoken_to, 3)}")
        for i in sources:
            heard_from, heard_to = _spoken(by_id[i])
            if end <= heard_from + EPSILON or start >= heard_to - EPSILON:
                return None, (f"line {position} at {start}-{end} takes in cue {i}, spoken at "
                              f"{round(heard_from, 3)}-{round(heard_to, 3)}, without covering it")
        source = strip_punctuation(str(line.get("source") or ""))
        translation = strip_punctuation(str(line.get("translation") or ""))
        if not source or not translation:
            return None, f"line {position} has an empty source or translation"
        heard = sum((_units(str(by_id[i]["source"])) for i in sources), Counter())
        added = _units(source) - heard
        if added:
            return None, f"line {position} adds words its cues never had: {sorted(added)[:5]}"
        unchanged = [by_id[i] for i in sources if by_id[i]["source"] == source]
        if unchanged and translation not in {c["translation"] for c in unchanged}:
            return None, f"line {position} keeps cue {unchanged[0]['id']}'s source but rewrites its translation"
        covered += sources
        base = copy.deepcopy(dict(by_id[sources[0]]))
        origins = sorted({o for i in sources for o in by_id[i].get("postcheck", {}).get("from", [i])})
        base.update({
            "start": start, "end": end,
            "source": source, "translation": translation,
            "source_exact": join_units([exact for i in sources
                                        if (exact := str(by_id[i].get("source_exact", by_id[i]["source"])))]),
            "postcheck": {"from": origins, "round": round_number,
                          "spoken": [round(spoken_from, 3), round(spoken_to, 3)]},
        })
        word_starts = [by_id[i]["start_word"] for i in sources if by_id[i].get("start_word") is not None]
        word_ends = [by_id[i]["end_word"] for i in sources if by_id[i].get("end_word") is not None]
        if word_starts and word_ends:
            base["start_word"], base["end_word"] = min(word_starts), max(word_ends)
        out.append(base)
    if sorted(covered) != sorted(assigned):
        missing = sorted(set(assigned) - set(covered))
        doubled = sorted(i for i, n in Counter(covered).items() if n > 1)
        return None, f"every id must appear in exactly one from; missing {missing}, repeated {doubled}"
    out.sort(key=_time_order)
    lower, upper = float(group["lower"]), group.get("upper")
    if out[0]["start"] < lower - EPSILON or out[0]["start"] < 0:
        return None, f"the first line starts at {out[0]['start']} before the previous line ends at {lower}"
    if upper is not None and out[-1]["end"] > float(upper) + EPSILON:
        return None, f"the last line ends at {out[-1]['end']} after the next line starts at {upper}"
    for earlier, later in zip(out, out[1:]):
        if later["start"] < earlier["end"] - EPSILON:
            return None, f"lines still overlap at {later['start']}"
    return out, None


def _cue_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _spoken(cue: Mapping[str, Any]) -> tuple[float, float]:
    """When a cue's speech was heard, which a retimed line keeps from round to round."""
    spoken = cue.get("postcheck", {}).get("spoken")
    return (float(spoken[0]), float(spoken[1])) if spoken else (float(cue["start"]), float(cue["end"]))


def apply_postcheck(job: Path) -> dict[str, Any]:
    state = _state(job)
    if (not tasks.stage_root(job, "postcheck").is_dir() and (job / "cues" / "checked.json").is_file()
            and not overlapping_ids(_current(job))):
        return {"round": state["round"], "overlapping_cues": 0, "rejected": [],
                "nothing_to_apply": True, "next": f"vstudio postprocess {job}"}
    directories = tasks.task_dirs(job, "postcheck")
    stamp = _plan_stamp(job)
    if state.get("planned") != stamp:
        raise RuntimeError(
            f"final.json changed after these post-check tasks were planned. "
            f"Run `vstudio plan-postcheck {job}` again."
        )
    if state.get("applied") == stamp:
        raise RuntimeError(
            f"this post-check round is already applied. Run `vstudio plan-postcheck {job}` "
            f"for the next one."
        )
    cues = _current(job)
    by_id = {int(c["id"]): c for c in cues}
    round_number = state["round"] + 1
    replacements: dict[int, list[dict[str, Any]]] = {}
    rejected: list[dict[str, Any]] = []
    for directory in directories:
        meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
        if any(int(i) not in by_id for g in meta["groups"] for i in g["ids"]):
            raise RuntimeError(
                f"{directory} was planned against a different cue set. "
                f"Run `vstudio plan-postcheck {job}` again."
            )
        answers: dict[int, Mapping[str, Any]] = {}
        only = meta["groups"][0]["group"] if len(meta["groups"]) == 1 else None
        result = tasks.read_result(directory)
        if isinstance(result, dict) and "groups" not in result and "lines" in result:
            result = [result]
        if (only is not None and isinstance(result, list) and result
                and all(isinstance(r, dict) and "from" in r for r in result)):
            result = [{"group": only, "lines": result}]
        try:
            rows = tasks.rows(result, "groups")
        except RuntimeError as exc:
            raise RuntimeError(f"{directory}: {exc}. Re-dispatch that task.") from None
        for row in rows:
            group_number = row.get("group", only)
            try:
                answers[int(group_number)] = row
            except (TypeError, ValueError):
                continue
        for group in meta["groups"]:
            lines, reason = resolve_group(answers.get(int(group["group"])), group, by_id,
                                          round_number=round_number)
            if lines is None:
                rejected.append({"task": directory.name, "group": group["group"],
                                 "ids": group["ids"], "reason": reason})
            else:
                replacements[int(group["ids"][0])] = lines
                for cue_id in group["ids"][1:]:
                    replacements[int(cue_id)] = []
    rewritten: list[dict[str, Any]] = []
    for cue in cues:
        rewritten.extend(replacements.get(int(cue["id"]), [cue]))
    remaining = overlapping_ids(rewritten)
    long_lines = [int(c["id"]) for lines in replacements.values() for c in lines
                  if len(c["postcheck"]["from"]) > 1 and c["end"] - c["start"] > LONG_LINE_SECONDS]
    pipeline._write(job, rewritten, "cues", "checked.json")
    (job / "cues" / "delivered.json").unlink(missing_ok=True)
    pipeline._write(job, {**state, "round": round_number, "remaining": remaining, "applied": stamp},
                    "cues", "postcheck.json")
    if not remaining:
        follow = f"vstudio postprocess {job}"
    elif round_number < POSTCHECK_MAX_ROUNDS:
        follow = f"vstudio plan-postcheck {job}"
    else:
        follow = f"report the cues that still overlap, then vstudio postprocess {job}"
    return {"round": round_number, "groups_resolved": sum(1 for v in replacements.values() if v),
            "lines_before": len(cues), "lines_after": len(rewritten),
            "rejected": rejected, "overlapping_cues": len(remaining),
            "long_merged_lines": long_lines,
            "still_overlapping": remaining[:20], "next": follow}


# --- post-process ---


def _milliseconds(env: str, default: int) -> int:
    value = os.environ.get(env)
    if not value:
        return default
    try:
        number = int(value)
    except ValueError:
        raise RuntimeError(f"{env}={value!r} is not a whole number of milliseconds") from None
    check_milliseconds(**{env: number})
    return number


def check_milliseconds(**values: int | None) -> None:
    for name, value in values.items():
        if value is not None and value < 0:
            raise RuntimeError(f"{name} must not be negative")


def postprocess_settings(
    *, enabled: bool | None = None, snap_gap_ms: int | None = None,
    lead_ms: int | None = None, lag_ms: int | None = None,
) -> dict[str, Any]:
    """Command-line values win, then the configured ones, then the defaults."""
    if enabled is None:
        enabled = os.environ.get(POSTPROCESS_ENV, "on").strip().lower() not in {"0", "off", "false", "no"}
    check_milliseconds(snap_gap_ms=snap_gap_ms, lead_ms=lead_ms, lag_ms=lag_ms)
    return {
        "enabled": enabled,
        "snap_gap_ms": snap_gap_ms if snap_gap_ms is not None else _milliseconds(SNAP_GAP_ENV, DEFAULT_SNAP_GAP_MS),
        "lead_ms": lead_ms if lead_ms is not None else _milliseconds(LEAD_ENV, DEFAULT_LEAD_MS),
        "lag_ms": lag_ms if lag_ms is not None else _milliseconds(LAG_ENV, DEFAULT_LAG_MS),
    }


def snap_and_pad(
    cues: Sequence[Mapping[str, Any]],
    *,
    snap_gap_ms: int,
    lead_ms: int,
    lag_ms: int,
    duration: float | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Join neighbours closer than the snap gap at its midpoint, then pad each group.

    A group's first line starts `lead_ms` early and its last line ends `lag_ms`
    late. Groups are at least the snap gap apart, so with the default numbers
    the padding always fits; when it would not, both sides shrink in proportion
    and meet. A line left overlapping another is neither snapped nor padded, but
    the line that ends last in such a run still takes the gap after it.
    """
    out = sorted(_checked_times(cues), key=_time_order)
    if not out:
        return out, {"groups": 0, "gaps_snapped": 0, "padding_shortened": 0, "overlapping_cues_left": 0}
    starts = [float(c["start"]) for c in out]
    ends = [float(c["end"]) for c in out]
    new_starts, new_ends = list(starts), list(ends)
    lead, lag = lead_ms / 1000, lag_ms / 1000
    # A gap runs from the latest end so far, not the previous line's end, so a
    # line left overlapping a long one is never padded or snapped into it.
    reach = list(accumulate(ends, max))
    # The line that ends last so far, which is the one a gap is closed against.
    holder = list(accumulate(range(len(ends)), lambda h, i: i if ends[i] >= ends[h] else h))
    gaps = [starts[i] - reach[i - 1] for i in range(1, len(out))]
    group_of = [0]
    snapped = 0
    for i in range(1, len(out)):
        gap, owner = gaps[i - 1], holder[i - 1]
        middle = (reach[i - 1] + starts[i]) / 2
        # The threshold compares whole milliseconds, since 2.3 - 2.0 is 0.29999
        # in floating point. An overlap under EPSILON counts as touching.
        if (-EPSILON < gap and (gap < 0 or round(gap * 1000) < snap_gap_ms)
                and new_starts[owner] < middle < ends[i]):
            new_starts[i] = new_ends[owner] = middle
            snapped += gap > EPSILON
            group_of.append(group_of[-1])
        else:
            group_of.append(group_of[-1] + (gap >= 0))
    shortened = 0
    new_starts[0] = max(0.0, starts[0] - lead)
    for i in range(1, len(out)):
        gap, owner = gaps[i - 1], holder[i - 1]
        if group_of[i] == group_of[i - 1] or gap < 0:
            continue
        if gap - (lead + lag) > EPSILON:
            new_ends[owner], new_starts[i] = reach[i - 1] + lag, starts[i] - lead
        else:
            # Both sides take the same value, so rounding cannot leave them overlapping.
            meet = reach[i - 1] + (gap * lag / (lead + lag) if lead + lag else 0.0)
            new_ends[owner] = new_starts[i] = meet
            shortened += gap < lead + lag - EPSILON
    last = holder[-1]
    new_ends[last] = reach[-1] + lag
    if duration:
        new_ends[last] = max(reach[-1], min(new_ends[last], duration))
    for cue, begin, finish in zip(out, new_starts, new_ends):
        cue["start"], cue["end"] = round(begin, 3), round(finish, 3)
    sizes = Counter(group_of)
    report = {"groups": len(sizes),
              "lines_in_joined_groups": sum(n for n in sizes.values() if n > 1),
              "gaps_snapped": snapped, "padding_shortened": shortened,
              "overlapping_cues_left": len(overlapping_ids(out))}
    return out, report


def _time_order(cue: Mapping[str, Any]) -> tuple[float, int]:
    return float(cue["start"]), int(cue["id"])


def _checked_times(cues: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(cue, checked_start=float(cue["start"]), checked_end=float(cue["end"])) for cue in cues]


def postprocess(job: Path, *, settings: Mapping[str, Any], subtitle_format: str,
                font: str | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Write the delivered cues and subtitle file; return the report and the cues."""
    _state_checked(job)
    cues = pipeline._read(job, "cues", "checked.json")
    if settings["enabled"]:
        delivered, report = snap_and_pad(
            cues, snap_gap_ms=settings["snap_gap_ms"], lead_ms=settings["lead_ms"],
            lag_ms=settings["lag_ms"], duration=_duration(job))
    else:
        delivered = _checked_times(cues)
        report = {"skipped": "post-process is off, so the post-checked timing is delivered unchanged",
                  "overlapping_cues_left": len(overlapping_ids(delivered))}
    pipeline._write(job, delivered, "cues", "delivered.json")
    output = pipeline.render(job, subtitle_format=subtitle_format, font=font, cues=delivered)
    return ({"subtitles": str(output), "subtitle_format": subtitle_format, "lines": len(delivered),
             "settings": dict(settings), **report}, delivered)


def delivered_fingerprint(job: Path) -> str | None:
    path = job / "cues" / "delivered.json"
    return _fingerprint(path) if path.is_file() else None


def refresh_other_formats(job: Path, output: Path, subtitle_format: str,
                          delivered: Sequence[Mapping[str, Any]], font: str | None = None) -> list[str]:
    """Re-render any other format already in out/ from the delivered cues, so none keeps old timing."""
    refreshed = []
    for other in pipeline.SUBTITLE_FORMATS:
        path = output.with_suffix(f".{other}")
        if other != subtitle_format and path.is_file():
            pipeline.render(job, subtitle_format=other, font=font, output=path, cues=delivered)
            refreshed.append(str(path))
    return refreshed


def built_from_current_final(job: Path) -> bool:
    """Whether checked.json and delivered.json come from final.json as it is now."""
    final, path = job / "cues" / "final.json", job / "cues" / "postcheck.json"
    if not final.is_file() or not path.is_file():
        return False
    return json.loads(path.read_text(encoding="utf-8")).get("final_sha256") == _fingerprint(final)


def _state_checked(job: Path) -> None:
    # _state deletes checked.json when final.json has changed since post-check.
    state = _state(job)
    if not (job / "cues" / "checked.json").is_file():
        raise RuntimeError(
            f"post-check has not run on the current final.json. Run `vstudio plan-postcheck {job}` first."
        )
    if state.get("planned") and state.get("planned") != state.get("applied"):
        raise RuntimeError(f"a post-check round is planned but not applied. Run `vstudio apply-postcheck {job}`.")
    if state.get("remaining") and state["round"] < POSTCHECK_MAX_ROUNDS:
        raise RuntimeError(f"cues still overlap. Run `vstudio plan-postcheck {job}` for the next round.")
