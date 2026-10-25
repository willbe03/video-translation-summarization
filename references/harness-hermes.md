# Running workers on Hermes

## Contents

- Launching
- When a worker cannot read its whole payload
- Which route each worker takes
- When a worker returns HTTP 401 immediately
- What this harness enforces

You are in the right file if you have a `delegate_task` tool.

## Launching

`delegate_task` carries every task inline, so there is nothing to install and
one call runs the whole stage.

```
delegate_task(tasks=[
  {"goal": "Read the brief at /abs/path/tasks/translate/000/brief.md and do exactly what it says.",
   "context": "<the full output of `vstudio worker-prompt`>",
   "option": "fast"},
  {"goal": "Read the brief at /abs/path/tasks/translate/001/brief.md and do exactly what it says.",
   "context": "<the same worker instructions again>",
   "option": "fast"},
])
```

Every task carries an `option`; see "Which route each worker takes" below.
The orchestrator is itself a subagent, so it has `delegate_task` only when
`delegation.max_spawn_depth` is 2 or more. Without it the orchestrator cannot
launch workers; stop and report that the setting must be raised.

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

## When a worker cannot read its whole payload

When a worker reports a truncated read, do not infer completeness from the
payload's small byte count: a reader can shorten individual long JSONL lines.
Inspect the reported read boundaries without dumping the transcript into the
orchestrator. Repair and verify the worker's input/read path before retrying,
and retain the worker-only synthesis boundary. Do not describe an attempted
input adaptation as a working recovery until a worker and `apply-*` accept it.

### When a synthesis worker keeps failing to read its payload

A worker that reads `payload.jsonl` through its own `read_file` can fail when
the plan writes one part's notes as a single long JSONL line: read_file
truncates a long line on a line boundary, so the worker never sees the whole
notes block and the stage produces nothing (or the orchestrator bypasses it by
hand-writing the summary, which voids the worker contract). Do not bypass the
worker. Regenerate the plan, then programmatically restructure the job-local
payload into short rows, one notes paragraph per row, each row keeping its
`part`, `start`, `end`, ordered by part then source order for chronology. This
is a harness adapter, not a global config change: rewrite only that task's
`payload.jsonl` and its `brief.md` so the brief describes the paragraph rows
and their ordering (e.g. "14 rows, one notes paragraph per row, part 0 then
part 1, in source order"). Then re-dispatch one worker for the regenerated
brief. Verification: run the stage's apply command and check-summary; these
validate accepted output and timestamp coverage, not factual accuracy or the
worker's complete compliance. After a review-driven correction, run the reviewer
again against the exact final artifact and save a new verdict. Retain the earlier
failed review as provenance; a corrected draft is not evidence of a passing
independent review. Count prose words after removing Markdown citation tokens,
and label raw whitespace-token counts separately.

## Which route each worker takes

With the `advanced-delegation` plugin in `model_map` mode, each `delegate_task`
task takes an `option` naming a configured route. Read the plan's `agent` field
and put the matching option on every task of the stage. The route names below
are the ones this skill is maintained with; on another install, use the cheap
and the stronger route its `option` enum offers.

- `vstudio-worker-light` (segment) and `vstudio-worker-standard` (translate,
  summarize notes): `"option": "fast"`, the cheap route.
- `vstudio-worker-deep` (proofread, glossary, cohere, synthesize):
  `"option": "review-claude"`, the stronger route.

The route decides model and effort. Ignore the plan's `model` and `effort`
fields, which name Claude Code models, and never put a provider or model id in
the call. Without `model_map` routes, every child takes `delegation.model` and
`delegation.reasoning_effort` from global config at once, so the tiers
cannot be honoured; do not spend effort trying, and say in the report that
every stage ran on one model. If the option you need is missing from the
`option` enum, stop and report it rather than letting the task fall to the
default route: a deep stage on the cheap route is the failure that costs
quality. Each result carries a `route` field; report the routes each stage
actually ran on.

The independent review after a correction is not a plan stage, but it follows
the same rule: dispatch it with `"option": "review-claude"`. A task with no
`option` runs on the default `fast` route, which does not count as the
independent review; check the result's `route` before reporting a verdict.
Editing citation times in a worker result is a content correction, not a
formatting fix, so it needs that review. Send the correction to a
`review-claude` worker rather than editing it yourself, and have it set each
time from the word timings in `asr/words.json`: `transcript.md` can hold one
timestamp block for the whole video, and times estimated from it drift by a
minute or more.

## When a worker returns HTTP 401 immediately

Every child failing at once, before doing any work, is a credentials problem
rather than anything about the task. In model_map mode each task's route names
a provider and the child loads that provider's own credentials;
`delegation.model` and `delegation.api_key` are not used. Check what the
failing route points at with `hermes delegation config --show`, and whether
that provider is authenticated (for the Claude subscription route, whether
`claude` is logged in).

You cannot fix credentials mid-job. Have the user repair the route's provider.
As a way through in the meantime, run each brief in its own one-shot hermes
process, which authenticates the same way the main session does. Say in the
final report that the stage ran serially and on the main model, because the
timing table will look nothing like a parallel one.

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
