# Running workers on Hermes

You are in the right file if you have a `delegate_task` tool.

## Launching

`delegate_task` carries every task inline, so there is nothing to install and
one call runs the whole stage.

```
delegate_task(tasks=[
  {"goal": "Read the brief at /abs/path/tasks/translate/000/brief.md and do exactly what it says.",
   "context": "<the full output of `vstudio worker-prompt`>"},
  {"goal": "Read the brief at /abs/path/tasks/translate/001/brief.md and do exactly what it says.",
   "context": "<the same worker instructions again>"},
])
```

Each child sees only its own `context`, so the instructions are repeated in
every task. That is the documented behaviour, not waste.

Put every task for a stage in one call. A child spawned from a subagent returns
its result inside your own turn, which is what makes the orchestrator able to
wait for a whole stage.

`delegate_task` refuses a call carrying more tasks than
`delegation.max_concurrent_children`, which defaults to ten. Set that number at
install so the planner sizes its batches to fit:

```bash
uv run --project scripts/vstudio vstudio configure --concurrency 10
```

After that a plan either fits in one call or tells you how many to send per
group. A ninety minute video plans ten translation tasks of 142 cues rather
than thirty six of forty, which also reads better across cue boundaries.

## Model and effort are one setting for every worker

`delegate_task` takes a goal, a context and an output schema. It takes no model
and no reasoning effort, and there is no agent file here to put them in. Both
come from `delegation.model` and `delegation.reasoning_effort` in config, which
apply to every child equally, so the tiering in a plan cannot be honoured: the
cheap segmentation worker and the proofreader run identically.

Ignore the `agent` and `effort` fields in the plan output. Nothing you can pass
will change either, and there is no fallback that recovers them the way there
is on Claude Code. What survives is the wording, since the segmentation brief
tells the worker the pass is mechanical, which is why that line exists.

Pick the config once for the whole job rather than per stage. Proofreading,
glossary and coherence are the stages that decide what a person meant, so a
setting good enough for them is the one to use, and segmentation costs more
than it should as a consequence. Say so in the final report, because the timing
table will show segmentation taking longer than its share elsewhere.

## What this harness enforces

Not the exec ban, and there is no definition file that could. Hermes derives a
child's capabilities from its depth rather than from the call, so a worker
keeps whatever toolset its depth grants and the instructions in `context` are
the only thing in the way. Paste them in full, every time.

Observed behaviour when a worker keeps its shell: it reads the payload with
`cat`, writes the result through a heredoc, and drafts to a temporary file
outside the job. The heredoc is the dangerous one, since cue text is full
of quotes and the failure is silent.

What Hermes does give you for free is context isolation. Children start with no
memory and no context files loaded, so a worker cannot reach this skill even if
it goes looking.
