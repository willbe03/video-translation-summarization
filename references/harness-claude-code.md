# Running workers on Claude Code

You are in the right file if you have an `Agent` tool.

## Launching

One `Agent` call per task, all of them in a single message so they run
concurrently. Launched across several messages, the stage waits on each message
in turn.

```
Agent(
  subagent_type: "<the agent field from the plan>",
  description: "translate 3 of 8",
  prompt: "Your brief is /abs/path/tasks/translate/002/brief.md"
)
```

The `agent` and `effort` fields in the plan output name which worker to use.
The shipped definitions carry the effort, so you do not pass it.

## When the worker type is missing

`Agent type not found` means the definitions have not loaded. Claude Code reads
`~/.claude/agents` when a session starts, so a definition written during this
session is invisible until the next one.

Do not stop and do not ask the user to restart. Relaunch with
`subagent_type: "general-purpose"`, the `model` from the table below, and the
output of `vstudio worker-prompt` pasted above the brief path. Note in the
final report that workers ran unrestricted, and suggest a restart so the next
job gets the real ones.

| plan agent | model | effort |
| --- | --- | --- |
| vstudio-worker-light | sonnet | low |
| vstudio-worker-standard | sonnet | high |
| vstudio-worker-deep | opus | high |

The fallback cannot set effort. `Agent` takes `subagent_type`, `prompt`,
`description`, `model` and `isolation`, and effort is not among them: it is a
property of an agent definition, not of a call. A built-in agent therefore runs
at whatever effort the session is using. That costs money on the segment stage,
which is meant to think less, and it is another reason to mention the restart.

## What this harness enforces

A shipped worker has `tools: Read, Write` and names every way of running a
command in `disallowedTools`, so it cannot run one at all. This is the only
harness here where the exec ban is real rather than requested.

`permissionMode: acceptEdits` lets a worker write its result without stopping
to ask. That grants nothing else, because there is nothing else it can do.

## How many at once

Claude Code runs at most 20 subagents at a time and refuses a launch past that.
Plans spread work across `VSTUDIO_CONCURRENCY` workers, so on this harness it
should be 20. If `vstudio doctor` does not list it under `settings`, pass
`--concurrency 20` to every plan command. When a plan reports `waves`, launch
one group in a single message, wait for all of it, then launch the next.

## Waiting on a long command

`asr` can run for many minutes. Run it with the Bash tool in the foreground and
`timeout: 600000`. A command that outlives the timeout is moved to the
background as a tracked task, and you are notified when it exits, so you can
wait for that notification. A process started with `&` or `nohup` is invisible
to the harness: nothing will tell you it finished.
