"""Task directories: the handoff between the orchestrator and its workers.

A task is a directory holding three files. `brief.md` is metadata: what to do,
what shape the answer takes, and nothing else. `payload.json` is the data. The
worker writes `result.json`. The orchestrator passes only the directory path,
so no cue text ever passes through its context.

Every stage follows the same two-command rhythm: `plan-*` writes the task
directories and prints what to dispatch, `apply-*` reads the results back,
validates them, and fails loudly with the task that broke.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

BRIEF = "brief.md"
PAYLOAD = "payload.jsonl"
PAYLOAD_TEXT = "payload.txt"
RESULT_JSON = "result.json"
RESULT_MARKDOWN = "result.md"
RESULT_TEXT = "result.txt"


@dataclass(frozen=True)
class Stage:
    name: str
    # Which shipped worker definition to launch. All three have the same Read
    # and Write access and differ in model and how hard they are asked to think.
    agent: str
    effort: str
    purpose: str
    # A stage returns JSON only when its answer really has fields. Cue breaks
    # and translations are lines of text, and measured on a real job more than
    # half of what a worker wrote in JSON was scaffolding around them.
    result: str = RESULT_JSON


STAGES: dict[str, Stage] = {
    # Splitting a word list at clause boundaries is pattern work, not reasoning.
    "segment": Stage(
        "segment", "vstudio-worker-light", "low", "cut words into cues", RESULT_TEXT
    ),
    "translate": Stage(
        "translate", "vstudio-worker-standard", "high", "translate cues", RESULT_TEXT
    ),
    # The proofreader is the only stage that can silently damage the speaker's
    # words, so it gets the most capable model as well as the effort.
    "proofread": Stage("proofread", "vstudio-worker-deep", "high", "repair ASR and polish"),
    # A name has to read the same everywhere, and only a reader who sees every
    # use can decide how, so one worker reads the whole transcript.
    "glossary": Stage("glossary", "vstudio-worker-deep", "high", "decide names and terms"),
    # The only stage that sees every cue at once, so tone and names can be made
    # consistent across stretches no single proofreader saw.
    "cohere": Stage("cohere", "vstudio-worker-deep", "high", "make the whole set consistent"),
    "summarize": Stage(
        "summarize", "vstudio-worker-standard", "high", "note one part", RESULT_MARKDOWN
    ),
    "synthesize": Stage(
        "synthesize", "vstudio-worker-deep", "high", "assemble the summary", RESULT_MARKDOWN
    ),
}


def stage_root(job: Path, stage: str) -> Path:
    return job / "tasks" / stage


def _frame(
    instructions: str, *, stage: str, index: int, count: int, reading: str, result: str
) -> str:
    return f"""# {stage} task {index + 1} of {count}

{reading}
Write your answer to `{result}` in this directory, in the shape described below
and nothing else.
Reply with one line saying what you wrote. Do not paste the answer into your
reply.

Work only from this brief and its payload. Do not read any other file, do not
run any command, and do not look for further instructions elsewhere. Write
nothing except `{result}` in this directory, and write it with your file
writing tool rather than through a shell.

{instructions}
"""


def write_task(
    job: Path,
    stage: str,
    index: int,
    count: int,
    *,
    instructions: str,
    payload: Any,
    meta: dict[str, Any] | None = None,
) -> Path:
    directory = stage_root(job, stage) / f"{index:03d}"
    directory.mkdir(parents=True, exist_ok=True)
    for name in (PAYLOAD, PAYLOAD_TEXT):
        (directory / name).unlink(missing_ok=True)
    if isinstance(payload, str):
        # A stage that only needs the words gets them as written, without the
        # per-row structure a JSON payload spends tokens on.
        (directory / PAYLOAD_TEXT).write_text(payload, encoding="utf-8")
        reading = f"Read `{PAYLOAD_TEXT}` in this directory. It holds plain text."
    else:
        rows = payload if isinstance(payload, list) else [payload]
        # One object per line rather than indented JSON: a worker reads the whole
        # payload, and indenting it costs roughly four times the tokens.
        (directory / PAYLOAD).write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
        )
        reading = (f"Read `{PAYLOAD}` in this directory. It holds {len(rows)} "
                   f"{'row' if len(rows) == 1 else 'rows'}, one JSON object per line.")
    result = STAGES[stage].result
    (directory / BRIEF).write_text(
        _frame(instructions, stage=stage, index=index, count=count, reading=reading, result=result),
        encoding="utf-8",
    )
    if meta:
        (directory / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    for name in (RESULT_JSON, RESULT_MARKDOWN, RESULT_TEXT):
        (directory / name).unlink(missing_ok=True)
    return directory


def reset_stage(job: Path, stage: str) -> None:
    import shutil

    root = stage_root(job, stage)
    if root.exists():
        shutil.rmtree(root)


def plan(job: Path, stage: str, directories: Sequence[Path], **extra: Any) -> dict[str, Any]:
    """Describe the work to dispatch, and stamp when it was handed out.

    The gap between planning and applying is the only place the wall clock of a
    worker round is visible, since nothing in Python is running while they
    think. `apply` reads the stamp back rather than asking the orchestrator to
    time itself.
    """
    spec = STAGES[stage]
    stage_root(job, stage).mkdir(parents=True, exist_ok=True)
    (stage_root(job, stage) / "planned_at").write_text(
        str(time.time()), encoding="utf-8"
    )
    return {
        "stage": stage,
        "agent": spec.agent,
        "effort": spec.effort,
        "purpose": spec.purpose,
        "parallel": True,
        "dispatch": [str(d / BRIEF) for d in directories],
        "apply": f"vstudio apply-{stage} {job}",
        **extra,
    }


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")
# A worker sometimes leaves the closing tag of its own file write, such as
# </content>, on a line by itself.
_STRAY_TAG = re.compile(r"^(?:<|&lt;)/?[A-Za-z_][\w-]*(?:>|&gt;)$")


def read_lines_result(directory: Path) -> list[str]:
    """One worker's answer as non-blank lines, without fences or stray tags."""
    path = directory / RESULT_TEXT
    if not path.is_file():
        raise RuntimeError(
            f"{path} is missing. The worker for {directory.name} did not write its "
            f"answer. Re-dispatch that one task with the same brief."
        )
    lines = [
        line.strip() for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("```")
        and not _STRAY_TAG.match(line.strip())
    ]
    if not lines:
        raise RuntimeError(f"{path} is empty. Re-dispatch {directory.name}.")
    return lines


def read_text_result(directory: Path) -> str:
    """One worker's prose answer, as written."""
    path = directory / RESULT_MARKDOWN
    if not path.is_file():
        # A worker told to write Markdown sometimes writes JSON anyway.
        legacy = directory / RESULT_JSON
        if legacy.is_file():
            value = read_result(directory)
            for key in ("notes", "summary", "text"):
                if isinstance(value, dict) and str(value.get(key) or "").strip():
                    return str(value[key]).strip()
        raise RuntimeError(
            f"{path} is missing. The worker for {directory.name} did not write its "
            f"answer. Re-dispatch that one task with the same brief."
        )
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise RuntimeError(f"{path} is empty. Re-dispatch {directory.name}.")
    return text


def read_result(directory: Path) -> Any:
    """Parse one worker's answer, tolerating a code fence or surrounding prose."""
    path = directory / RESULT_JSON
    if not path.is_file():
        raise RuntimeError(
            f"{path} is missing. The worker for {directory.name} did not write its "
            f"answer. Re-dispatch that one task with the same brief."
        )
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        raise RuntimeError(f"{path} is empty. Re-dispatch {directory.name}.")
    for candidate in (raw, _FENCE.sub("", raw)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    start = min((i for i in (raw.find("{"), raw.find("[")) if i >= 0), default=-1)
    end = max(raw.rfind("}"), raw.rfind("]"))
    if start >= 0 and end > start:
        try:
            return json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise RuntimeError(
        f"{path} is not JSON. First 200 characters: {raw[:200]!r}. "
        f"Re-dispatch {directory.name}."
    )


def dispatch_timing(job: Path, stage: str) -> dict[str, Any] | None:
    """When this stage's workers started and finished, or None if never planned.

    `seconds` is the whole gap between planning and applying, so it carries the
    orchestrator's own launch and notice delays as well as the workers. The
    result files say when the work itself ended, which separates the two: a
    `last_done` well below `seconds` means time went on dispatching and
    noticing rather than on thinking.
    """
    stamp = stage_root(job, stage) / "planned_at"
    if not stamp.is_file():
        return None
    try:
        started = float(stamp.read_text(encoding="utf-8"))
    except ValueError:
        return None
    finished = [
        path.stat().st_mtime
        for directory in stage_root(job, stage).iterdir() if directory.is_dir()
        for path in (directory / RESULT_JSON, directory / RESULT_MARKDOWN, directory / RESULT_TEXT)
        if path.is_file() and path.stat().st_mtime >= started
    ]
    timing: dict[str, Any] = {"seconds": round(time.time() - started, 3), "tasks": len(finished)}
    if finished:
        timing["first_done"] = round(min(finished) - started, 3)
        timing["last_done"] = round(max(finished) - started, 3)
    return timing


def task_dirs(job: Path, stage: str) -> list[Path]:
    root = stage_root(job, stage)
    if not root.is_dir():
        raise RuntimeError(f"no {stage} tasks planned yet. Run `vstudio plan-{stage} {job}` first.")
    return sorted(p for p in root.iterdir() if p.is_dir())


def rows(value: Any, key: str) -> list[dict[str, Any]]:
    """Pull a list of rows out of a result, accepting a bare list."""
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        found = value.get(key)
        if isinstance(found, list):
            return [row for row in found if isinstance(row, dict)]
    raise RuntimeError(f"expected a {key!r} list in the result, got {type(value).__name__}")


def missing(expected: Iterable[int], seen: Iterable[int]) -> list[int]:
    return sorted(set(expected) - set(seen))
