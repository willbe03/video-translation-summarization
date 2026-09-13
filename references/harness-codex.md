# Running workers on Codex

You are in the right file if you are running in Codex CLI.

## Launching

Codex spawns subagents from the wording of your own prompt, so ask for them by
name and give it the list of briefs.

```
Spawn one vstudio-worker-standard agent per brief, in parallel. Give each agent
exactly one line, the path of its brief:
/abs/path/tasks/translate/000/brief.md
/abs/path/tasks/translate/001/brief.md
```

Custom agents live in `~/.codex/agents/*.toml` and are written by
`vstudio install-agents --harness codex`. Model and reasoning effort come from
the agent file, so you do not pass them.

## When the worker is not defined

Ask for an ordinary subagent instead and paste the output of
`vstudio worker-prompt` above the brief paths, then note in the final report
that workers ran with the instructions alone.

## What this harness enforces

Not the exec ban. There is no documented way to take a tool away from a Codex
agent, so a worker keeps its shell and the instructions are the only thing
stopping it reading the payload with `cat` and writing the result through a
heredoc. The heredoc is the one that bites: subtitle text is full of quotes and
the failure is silent.

`sandbox_mode` bounds where a worker may write, not what it may run. The
shipped definitions use `workspace-write`, which also means the artifacts
directory has to sit inside the workspace. If a job fails because a worker
cannot write its result, that is why: either move the artifacts directory with
`vstudio configure --artifacts`, or pass `--job-root` inside the workspace for
this job.
