"""Worker definitions, and how to write them out for each harness.

Every harness stores agent definitions in its own place and format, so the
definitions live here as data and are rendered on install. Keeping one source
means the three copies cannot drift apart, and adding a harness means adding
one renderer.

Workers get Read and Write and nothing else where the harness can enforce it.
The instructions repeat the rule anyway, because Hermes derives a child's
capabilities from its depth and cannot take a tool away per task. Observed
behaviour when a worker keeps its shell: it reads the payload with `cat`,
writes the result with a heredoc, and drafts to a temporary file somewhere
outside the job. The heredoc is the dangerous one, since subtitle text is full
of quotes and the failure is silent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

SYSTEM_PROMPT = """You do one bounded piece of work on a video subtitle job.

Your instructions arrive as the path of a brief file. Read that brief, read the
payload file it names, write the result file it names, and reply with one line
saying how many rows you wrote.

The brief and its payload are complete. Nothing is missing and there is no
wider context to find.

Do not read, search for, or ask for any other file.
Write nothing anywhere except the one result file the brief names. No drafts,
no scratch copies, no temporary directories.
Use your file reading and file writing tools. Do not use a shell for either,
even if you have one: a heredoc breaks on the first quote or backtick in the
text you are writing.
Do not check your own answer by running code. It is checked in code the moment
you return, and checking it yourself only spends time.
Do not paste your answer into your reply. The file you write is the answer.
Where the brief and the payload disagree, follow the brief.
If you cannot do the work, write {"error": "one sentence saying why"} to the
result file and say so in your reply."""


@dataclass(frozen=True)
class WorkerDefinition:
    name: str
    effort: str
    model: str
    description: str


NAME_PREFIX = "vstudio-worker-"

# What each tier means, unless configured otherwise. These are measured rather
# than guessed: on a real job the light tier at low effort cut segmentation
# wall clock and tokens by about three quarters with no loss in cue quality,
# and Sonnet is the floor because the cheap stage is cheap by thinking less,
# not by knowing less. Change them deliberately.
TIER_DEFAULTS: dict[str, tuple[str, str]] = {
    "light": ("sonnet", "low"),
    "standard": ("sonnet", "high"),
    "deep": ("opus", "high"),
}

TIER_ENV_PREFIX = "VSTUDIO_TIER_"

_DESCRIPTIONS = {
    "light": "Splits a transcript into subtitle cues from a brief file. "
             "Launched by the video subtitling orchestrator, not directly.",
    "standard": "Translates cues or takes notes on a video from a brief file. "
                "Launched by the video subtitling orchestrator, not directly.",
    "deep": "Proofreads subtitles, builds a glossary or assembles a summary from a "
            "brief file. Launched by the video subtitling orchestrator, not directly.",
}


def tier_env_name(tier: str) -> str:
    return f"{TIER_ENV_PREFIX}{tier.upper()}"


def parse_tier(value: str) -> tuple[str, str]:
    """Read a `model/effort` setting, rejecting anything unusable.

    A typo here silently changes which model does the work, so this refuses
    rather than falling back: a job running on the wrong tier is expensive and
    the only symptom is a bill.
    """
    model, _, effort = str(value).strip().partition("/")
    model, effort = model.strip().lower(), effort.strip().lower()
    if not model or not effort:
        raise RuntimeError(f"{value!r} is not a tier; write it as model/effort, e.g. sonnet/low")
    if effort not in {"low", "medium", "high", "xhigh", "max"}:
        raise RuntimeError(f"{effort!r} is not an effort; use low, medium, high, xhigh or max")
    return model, effort


def tier(name: str) -> tuple[str, str]:
    """The model and effort for one tier, from the environment or the default."""
    if name not in TIER_DEFAULTS:
        raise RuntimeError(f"unknown tier {name!r}; known: {', '.join(sorted(TIER_DEFAULTS))}")
    configured = os.environ.get(tier_env_name(name))
    return parse_tier(configured) if configured else TIER_DEFAULTS[name]


def tier_of(worker_name: str) -> str:
    return worker_name[len(NAME_PREFIX):] if worker_name.startswith(NAME_PREFIX) else worker_name


def worker(tier_name: str) -> WorkerDefinition:
    model, effort = tier(tier_name)
    return WorkerDefinition(
        f"{NAME_PREFIX}{tier_name}", effort, model, _DESCRIPTIONS[tier_name]
    )


def workers() -> tuple[WorkerDefinition, ...]:
    """The shipped workers as currently configured.

    A function rather than a constant because the tiers are settings now, and
    a module-level tuple would freeze whatever the environment held at import.
    """
    return tuple(worker(name) for name in TIER_DEFAULTS)


ORCHESTRATOR = WorkerDefinition(
    "vstudio-orchestrator", "medium", "inherit",
    "Runs a video subtitling or summary job end to end, calling vstudio for the "
    "deterministic steps and delegating every model step to worker subagents.",
)


# Named so the ban survives a future release that widens what `tools` implies.
# Nothing a worker does needs a command run, so every way of running one goes.
BANNED_TOOLS = "Bash, BashOutput, KillShell, Task, Agent, NotebookEdit, WebFetch, WebSearch"


def claude_worker(worker: WorkerDefinition) -> str:
    return f"""---
name: {worker.name}
description: {worker.description}
tools: Read, Write
disallowedTools: {BANNED_TOOLS}
model: {worker.model}
effort: {worker.effort}
maxTurns: 16
permissionMode: acceptEdits
---

{SYSTEM_PROMPT}
"""


def claude_orchestrator(skill_dir: Path) -> str:
    return f"""---
name: {ORCHESTRATOR.name}
description: {ORCHESTRATOR.description}
tools: Bash, Read, Write, Agent, TodoWrite
model: inherit
effort: medium
permissionMode: acceptEdits
---

You run one video subtitling or summary job from start to finish.

Read {skill_dir}/references/pipeline.md and follow it. Read
{skill_dir}/references/harness-shared.md and, because you have an Agent tool,
{skill_dir}/references/harness-claude-code.md. Read no other harness file.

Never do a model step yourself. Translating, proofreading, cutting cues and
writing notes all go to workers. Your own job is to run the vstudio commands,
dispatch the briefs those commands produce, and report at the end.

If a worker type in a plan does not exist, do not stop and do not ask for a
restart. Fall back the way the harness file says, finish the job, and say so at
the end.
"""


def codex_worker(worker: WorkerDefinition) -> str:
    body = SYSTEM_PROMPT.replace('"""', "'''")
    return f'''name = "{worker.name}"
description = "{worker.description}"
model = "{worker.model}"
model_reasoning_effort = "{worker.effort}"
sandbox_mode = "workspace-write"
developer_instructions = """
{body}
"""
'''


def definition_names(harness: str) -> list[str]:
    """What install would write, so remove can find it again."""
    if harness == "claude":
        return [f"{w.name}.md" for w in workers()] + [f"{ORCHESTRATOR.name}.md"]
    if harness == "codex":
        return [f"{w.name}.toml" for w in workers()]
    return []


def remove(dest: Path, *, harness: str) -> list[Path]:
    """Delete the definitions install wrote, and nothing else in that directory."""
    gone: list[Path] = []
    for name in definition_names(harness):
        path = dest / name
        if path.is_file():
            path.unlink()
            gone.append(path)
    return gone


def install(dest: Path, *, harness: str, skill_dir: Path) -> list[Path]:
    """Write the definitions for one harness and return what was written.

    These are an enforcement upgrade, not a requirement. A worker launched
    without them behaves the same as long as it is given SYSTEM_PROMPT; what it
    loses is the guarantee that it cannot ignore the instructions.
    """
    dest.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    if harness == "claude":
        for worker in workers():
            path = dest / f"{worker.name}.md"
            path.write_text(claude_worker(worker), encoding="utf-8")
            written.append(path)
        path = dest / f"{ORCHESTRATOR.name}.md"
        path.write_text(claude_orchestrator(skill_dir), encoding="utf-8")
        written.append(path)
    elif harness == "codex":
        for worker in workers():
            path = dest / f"{worker.name}.toml"
            path.write_text(codex_worker(worker), encoding="utf-8")
            written.append(path)
    elif harness == "hermes":
        # delegate_task carries the goal and context inline, so there is no
        # registry to write into. The orchestrator pastes SYSTEM_PROMPT into
        # each task's context instead.
        return []
    else:
        raise RuntimeError(f"unknown harness {harness!r}; known: claude, codex, hermes")
    return written


def default_destination(harness: str) -> Path:
    if harness == "claude":
        return Path.home() / ".claude" / "agents"
    if harness == "codex":
        return Path.home() / ".codex" / "agents"
    raise RuntimeError(f"{harness} has no agent directory; pass --dest explicitly")
