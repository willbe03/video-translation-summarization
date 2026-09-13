# The orchestrator's playbook

## Contents

- How to run a command
- The rhythm of every stage
- Steps 1 and 2: acquire and transcribe, both branches
- Subtitles: segment, translate, proofread, render, verify
- Summary: notes, synthesis, coverage
- Finishing
- When a stage fails
- Options worth knowing

## How to run a command

Every command below is run from the skill directory:

```bash
uv run --project scripts/vstudio vstudio <command> ...
```

Set `VS="uv run --project scripts/vstudio vstudio"` once and use `$VS` after
that. Every command prints JSON on stdout. Read that JSON; do not go looking in
the job directory for the same answer.

Keep a todo list with one entry per step so a long job does not lose its place.

## The rhythm of every stage

A model stage is always three moves.

1. `plan-<stage>` slices the work into task directories and prints the agent to
   launch and a `dispatch` list of brief paths.
2. Launch one worker per brief path, all at once, the way
   your harness file describes. Give each worker its brief path and nothing
   else, with the worker instructions above it when it has no definition.
3. `apply-<stage>` reads every result, checks it, and writes the next artifact.
   If it exits non-zero, read the message: it names the task that broke.

Never open a payload or a result yourself. They hold the whole transcript, and
reading one puts it in your context for no gain. The checking is already done
in code.

## Steps 1 and 2: acquire and transcribe, both branches

```bash
$VS new "<source>" --title "<title if the user gave one>"
```

It prints the job directory. Keep it in a variable; every later command takes
it. A URL is downloaded into the job; a local file is used where it sits and is
never copied or deleted.

```bash
$VS asr "$JOB"
```

This is the longest step and it has no model in it. The provider chosen at
install is used unless you pass `--provider`. It prints the word count, the
detected language, and its own internal timings.

If the source language matters and the user told you, pass a locale:
`--language en-US`. Otherwise let it detect.

## Subtitles: segment, translate, proofread, render, verify

```bash
$VS plan-segment "$JOB"          # then dispatch, then:
$VS apply-segment "$JOB"
```

Segmenting cuts the word stream into cues. A worker writes the transcript back
with a line break where each cue ends and changes nothing else, so it never
touches an index or a timestamp. `apply-segment` aligns what it wrote against
what the recogniser heard, which is how a dropped or invented word is reported
with a position instead of silently shifting every cue after it.

The tasks run at the same time and cannot collide. Each one owns a contiguous
slice of the word list, sees that slice renumbered from zero, and writes only
its own result file, so a worker cannot name a word another worker owns. A
slice boundary is chosen at a sentence end wherever one is available, which is
why a cue never has to span two tasks. `apply-segment` reports readability
warnings, which are advisory, and any boundary it had to repair.

```bash
$VS plan-translate "$JOB" --target-language "<language>" --style "<instructions>"
$VS apply-translate "$JOB"
```

Pass the user's tone and terminology instructions to `--style` verbatim. There
is no flag for a style of translation; the instruction is the interface.
Translations come back one line per cue as an id and the text, which is checked
against the ids that went out.

```bash
$VS plan-proofread "$JOB"
$VS apply-proofread "$JOB"
```

Proofreading repairs ASR mishearings and polishes the translation. Workers see
their own cues plus a few neighbours marked as context, which they may read but
never return.

```bash
$VS render "$JOB"               # add --burn to write a subtitled video too
$VS verify "$JOB"
```

`verify` exits non-zero on anything mechanically wrong: a moved timestamp, a
cue that overlaps its neighbour, a comma that survived, an ASS file with the
wrong number of lines, or a source edit that added words the speaker did not
say. Its `adjudicate` list is every place the displayed source diverged from
what the recogniser heard. Read that list yourself. Each entry should be a
mishearing, a spelling, a proper name, or filler removal. Anything else is the
proofreader editing the speaker, and those cues get a targeted rerun:

```bash
$VS plan-proofread "$JOB" --only 41 88 209
```

## Summary: notes, synthesis, coverage

```bash
$VS plan-summarize "$JOB" --style "<instructions>"
$VS apply-summarize "$JOB"
$VS plan-synthesize "$JOB"
$VS apply-synthesize "$JOB"
$VS check-summary "$JOB"
```

The video is split into stretches of about fifteen minutes and one worker takes
notes on each, in parallel. A second stage assembles them into one summary,
which is where the chronology and the repetition across stretches get sorted
out. `apply-synthesize` turns the cited timestamps into deep links when the
source was a URL.

`check-summary` fails when the summary cites a moment past the end of the video
and warns when the last citation lands in the first three quarters, which
almost always means only the opening was covered. On that warning, rerun
`plan-summarize` for the parts that were thin rather than regenerating all of
it.

If the user wants both branches, run the subtitle steps first. The summary
stage uses the cues when they exist, which gives it better sentence boundaries
than raw ASR windows.

## Finishing

```bash
$VS timing "$JOB"
```

The dispatch rows are wall clock, not a sum: workers run together, so a stage
costs what its slowest worker costs. Each row also carries `first_done` and
`last_done`, taken from when the result files appeared. `last_done` is when the
thinking finished; the gap between it and `seconds` is time you spent launching
and noticing rather than time anyone spent working. On a measured run that gap
was 18 percent of the job, almost all of it from issuing launch calls one after
another instead of together.

`apply-<stage>` is the checking, and it rounds to hundredths of a second.

Report the output paths, that table, and anything that failed or needed a
rerun. If the user asked for a summary and the media was downloaded, offer
`$VS cleanup "$JOB"`, which deletes downloaded media and scratch files and
refuses to touch a file the user supplied.

To answer a question about something on screen, grab a still:
`$VS frame "$JOB" --at 00:12:30`.

## When a stage fails

`apply-*` names the task directory that broke. Re-dispatch that one worker with
the same brief path; the brief and payload are still there and re-running the
plan command would throw away every other worker's answer.

A worker that reports it could not do the work writes `{"error": ...}` to its
result file. Read that one file, fix what it names if you can, and re-dispatch.

If `$VS new` fails on a URL, the download was blocked, not the skill. Say so
and ask the user for a file instead of retrying variations.

## Options worth knowing

- `--size` on any plan command changes how many rows a worker gets. Smaller
  means more workers and more parallelism but less context for continuity.
- `--split-seconds` on `asr` cuts the audio for a provider that would otherwise
  take the whole file. It is faster on long media and slightly worse at the
  seams, so use it when the user cares more about wall clock than the last
  percent of accuracy.
- `--no-page-text` on the translate and proofread plans drops the video page
  description, which is otherwise offered to workers as a spelling reference
  for names.
- `$VS status "$JOB"` says which stage a job reached, which is how you resume
  one you did not start.
- `$VS doctor --live` spends one short request per cloud provider proving the
  key still works, which is worth doing when a job fails at the asr step.

Rerunning a plan command deletes every result for that stage, including ones
that were fine. Use it to change a parameter, never to retry one worker.
