# The orchestrator's playbook

## Contents

- How to run a command
- The rhythm of every stage
- Steps 1 and 2: acquire and transcribe, both branches
- Subtitles: glossary and segment, translate, proofread, cohere, render, verify
- Summary: notes, synthesis, coverage
- Finishing
- When a stage fails
- Long videos
- Options worth knowing

## How to run a command

Run every command from the skill directory:

```bash
uv run --project scripts/vstudio vstudio <command> ...
```

Below, `$VS <command>` is shorthand for that line and `$JOB` for the job
directory. Write both out in full each time rather than storing them in
variables: a shell variable does not reliably survive from one tool call to the
next, and zsh does not split `$VS` into words. Most commands print JSON on stdout;
`timing` prints a text table. Read the command output instead of opening artifacts
for the same answer. Keep any redirected command logs inside the job directory.

Copy the checklist for your branch and tick items off as you go. A long job is
easy to lose your place in, and the step most often skipped is the last one.

```
Subtitles:
- [ ] new           acquire, and check which audio track it picked
- [ ] asr           transcribe
- [ ] plan-glossary, plan-segment -> dispatch both together
- [ ] apply-segment, apply-glossary
- [ ] plan-translate -> dispatch -> apply-translate
- [ ] plan-proofread -> dispatch -> apply-proofread
- [ ] render, verify  read the adjudicate list, rerun cues that need it
- [ ] plan-cohere    -> dispatch -> apply-cohere
- [ ] render, verify  again after coherence
- [ ] timing, glossary-report  report paths and both tables

Summary:
- [ ] new           acquire, and check which audio track it picked
- [ ] asr           transcribe
- [ ] plan-summarize -> dispatch -> apply-summarize
- [ ] plan-synthesize -> dispatch -> apply-synthesize
- [ ] check-summary  act on a coverage warning rather than reporting it
- [ ] timing         report paths and the table
```

## The rhythm of every stage

A model stage is always three moves.

1. `plan-<stage>` slices the work into task directories and prints the agent to
   launch and a `dispatch` list of brief paths.
2. Launch one worker per brief path, all at once, the way your harness file
   describes. Give each worker its brief path and nothing else, with the worker
   instructions above it when it has no definition.

   When the plan reports `waves` above one, it also reports `dispatch_at_once`.
   Send that many, wait for them, send the next group, and only run `apply`
   after the last one. Everything a worker writes stays in its own directory,
   so waves cost nothing but wall clock.
3. `apply-<stage>` reads every result, checks it, and writes the next artifact.
   If it exits non-zero, read the message: it names the task that broke.

Never open a payload or a result yourself. They hold the whole transcript, and
reading one puts it in your context for no gain. The checking is already done
in code.

## Steps 1 and 2: acquire and transcribe, both branches

```bash
$VS new "<source>" --title "<title if the user gave one>"
```

It prints the job directory, which every later command takes as `$JOB`. A
Bilibili `/en/video/` link is treated as the `/video/` link for the same video.
When `new` fails on Bilibili with a hint about `--bilibili-en-fallback`, the
machine is outside mainland China; see INSTALL.md, then run `new` again. A URL
is downloaded into the job; a local file is used where it sits and is
never copied or deleted. When the user supplies a job-specific artifact root,
add `--job-root "<absolute workspace path>"` to `new`; do not run `configure`
to change the global artifact setting.

For a summary-only job from a URL, add `--audio-only`. It fetches just the
audio track (Bilibili's DASH audio, YouTube's best audio format), about half
the bytes; on Bilibili it also skips the second page extraction the video path
makes. On a site with no separate audio it falls back to the full video by
itself, and the output's `audio_only` says which happened. Leave it off when
subtitles will be burned in or the user may ask about what is on screen: an
audio-only job has no picture for `frame` or `render --burn`.

It also picks which audio track to transcribe. One track needs no thought. When
a file carries several, the output lists them all with `audio_track_reason`
saying why that one was chosen: a track in the language you asked for beats the
one the container marks default, and commentary, audio description and karaoke
tracks are never chosen. Read that list. A film with an original and a dub, or
a recording with a separate interpreter channel, is where the automatic choice
can be right by its own rules and wrong for what the user asked for. Pass
`--language` to `new` when you know the spoken language, and override after the
fact with `asr --audio-track N`.

```bash
$VS asr "$JOB"
```

This is the longest step and it has no model in it. The provider chosen at
install is used unless you pass `--provider`. It prints the word count, the
detected language, and its own internal timings.

Wait for it in the same turn. Nothing wakes an orchestrator that ended its turn
while a command it started outside the harness is still running, so ending the
turn to wait ends the job. Never start it with `&` or `nohup`; your harness file
says how to wait on a long command.

If the source language matters and the user told you, pass a locale:
`--language en-US`. Otherwise let it detect. Base the summary language on the
selected audio and detected speech, not the title or description. A translated
video page does not establish the language spoken in the recording.

What goes to the endpoint is the chosen track. A track already in a codec the
endpoint takes, and under 192 kbps, is copied rather than re-encoded. Anything
richer or in another codec is converted once to 192 kbps Opus. The output
reports `uploaded_format` and `uploaded_bytes`.

## Subtitles: glossary and segment, translate, proofread, cohere, render, verify

```bash
$VS plan-glossary "$JOB" --target-language "<language>" --style "<instructions>"
$VS plan-segment "$JOB"
# dispatch the glossary brief and the segment briefs together, then:
$VS apply-segment "$JOB"
$VS apply-glossary "$JOB"
```

The glossary and segmentation run at the same time. One worker reads the whole
ASR transcript, not the cues, and decides how every name and term is rendered,
following `--style` where it says how names are handled. Plan it before
`plan-segment`, which then leaves it one slot within `--concurrency`.
`apply-glossary` reports how many entries it kept and of which kinds, how many
characters the glossary adds to each brief, and any entry the transcript does
not contain, which usually means an invented name. It also lists `problems`,
such as a short form inside another entry's longer form when the longer
rendering does not contain the shorter one. When there are
problems, re-dispatch the glossary brief once and apply it again; if they
remain, continue and report them. `plan-translate` refuses to
run until the glossary is applied, and every translation and proofreading brief
carries the whole glossary.

Segmenting cuts the word stream into cues, one timed subtitle line each. A
worker receives its stretch of the transcript as plain text, with a line break
wherever the speaker paused, and writes it back
with a line break where each cue ends and changes nothing else, so it never
touches an index or a timestamp. `apply-segment` aligns what it wrote against
what the recogniser heard, which is how a dropped or invented word is reported
with a position instead of silently shifting every cue after it. Chinese and
Japanese are aligned a character at a time, because a worker writes them
without spaces whatever size of word the recogniser returned, and two of their
characters count as one word against the cue cap and the batch size.

The tasks run at the same time and cannot collide. Each one owns a contiguous
slice of the word list, sees only that slice's text, and writes only
its own result file, so a worker cannot name a word another worker owns. A
slice boundary is chosen at a sentence end wherever one is available, which is
why a cue never has to span two tasks. The pause threshold is the video's own
90th percentile gap between words, held between 0.3 and 1 second, and the plan
reports it with the number of breaks. `apply-segment` reports readability
warnings, which are advisory, any boundary it had to repair, and
`hanging_cues`: cues that stay on screen through a second or more of silence.
It also splits any cue at a silence of two seconds or more inside it, because a
line held that long with nobody speaking reads as left behind, and lists each
split among the boundary repairs.

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
$VS render "$JOB" --format srt  # srt or ass, as the launch message says; add --burn for a subtitled video
$VS verify "$JOB"
```

`verify` exits non-zero on anything mechanically wrong: a moved timestamp, a
cue that does not run from the earliest start to the latest end of the ASR words
it covers, a subtitle entry not timed like its cue, a comma that survived,
a subtitle file with the wrong number of entries, or a source edit that made a cue
longer than what the speaker said. Cues may
overlap: where two people talk at once the transcript overlaps, the cues keep
that timing, and `verify` only counts them in `overlapping_cues`.

Its `adjudicate` list is every place the displayed source diverged from
what the recogniser heard. Read that list yourself. Each entry should be a
mishearing, a spelling, a proper name, or filler removal. Anything else is the
proofreader editing the speaker, and those cues get a targeted rerun:

```bash
$VS plan-proofread "$JOB" --only 41 88 209
```

A targeted rerun adds its tasks beside the full pass rather than replacing it.
Dispatch only the briefs it prints, then run `apply-proofread`, `render` and
`verify` again. For each cue it covers, the rerun's answer replaces the full
pass's, and every other cue keeps its full pass correction.

```bash
$VS plan-cohere "$JOB"
$VS apply-cohere "$JOB"
```

Coherence runs once, after proofreading and its targeted reruns. One worker
reads every cue with the glossary and changes translations only, where a name, a
speaker's register or the tone differs from the rest of the video.
`apply-cohere` writes `cues/final.json` from the proofread set and reports how
many translations changed and how many of those only in typography: width,
case, spacing or punctuation. Then run `render` and `verify` again. A proofread
rerun after this point rebuilds `cues/final.json`, and `apply-proofread` then
reports `cohere_needs_rerun`, so plan and apply coherence again before
rendering.

## Summary: notes, synthesis, coverage

```bash
$VS plan-summarize "$JOB" --style "<instructions>"
$VS apply-summarize "$JOB"
$VS plan-synthesize "$JOB" --style "<instructions>"
$VS apply-synthesize "$JOB"
$VS check-summary "$JOB"
```

Pass `--language` to `plan-summarize` when the summary should be in a language
other than the one spoken. `plan-synthesize` then writes in the same language
unless it is given its own `--language`.

The video is split into stretches of about six minutes and one worker takes
notes on each, in parallel. The whole summary branch allows one citation form
only, in the notes and in the summary: a single time in half-width parentheses
with ASCII digits, `(01:38)`, minutes past 59 on a long video, `(75:20)`, and
several moments written as separate citations, `(01:38) (02:09)`. No ranges,
no lists in one bracket, no words or spaces inside the bracket, no other
brackets, no HH:MM:SS, no full-width `（ ）`, `：` or digits. A ratio, score or
time of day that is not a citation goes outside brackets, since a bracketed
clock-like time is refused. Every command reads only that form: the coverage
checks, the link pass and `check-summary`. Do not add lenient parsing of other
forms back into any of them: the user chose a strict, prompt-enforced format
over guessing, so a worker that strays is refused and re-dispatched rather
than interpreted. Summaries written before this rule (full-width or range
citations) now fail `check-summary`; that is expected.

`apply-summarize` refuses a part that cites in any other form, quoting the
offending tokens; `--allow-gaps` does not cover this. It also rejects a part
whose notes leave more than two minutes of transcript rows uncited, naming the
task and the stretch, e.g. `001 (08:46-10:58)`. Either way, re-dispatch just
that task with the same brief and apply again; do not raise `--seconds` to get
past it. If the re-run leaves the same stretch uncited and it is an ad, a song
or other off-topic speech, run `apply-summarize --allow-gaps`, which records
the stretches in its output instead of refusing, and say so in the report.

A second stage assembles the notes into one summary, which is where the
chronology and the repetition across stretches get sorted out.
`apply-synthesize` refuses a summary that cites in any other form, before
writing `out/summary.md`; re-dispatch the synthesize task or have a deep-tier
worker correct those citations in its `result.md`, then apply again.
Otherwise it turns every citation into a deep link when the source was a URL.

Without a length override, the assembly brief asks for about 50 words per minute
of video, never under 200, and `plan-synthesize` reports `target_words`. Pass the
user's instructions to `--style` on both summary plans. When the user asks for a
concise summary without a word count, choose a short target appropriate to the
content and state it in synthesis style; this overrides the default length.
Inspect the generated brief before dispatch to confirm the language and style
survived planning, without opening its transcript payload.

`check-summary` fails when the summary cites a moment past the end of the video
or holds a citation in any form but `(MM:SS)` (which a hand edit after
`apply-synthesize` can bring back), and warns when the last citation lands in
the first three quarters, which almost always means only the opening was
covered. It also warns when speech
ends well before the video does. That is normal for credits or music and is
also what a transcript that stopped early looks like, so report the gap with
what you believe caused it. On that warning, rerun
`plan-summarize` for the parts that were thin rather than regenerating all of
it.

After `apply-synthesize`, verify that every deep link retains the literal
source ID with an in-range seek time, and that `citation_problems` is empty.
Treat `check-summary` as a
coverage gate, not a grounding or writing-style review; have an independent
reviewer check claims, uncertain names, attribution and concision as well.

If the user wants both branches, run the subtitle steps first. The summary
stage uses the cues when they exist, which gives it better sentence boundaries
than raw ASR windows.

## Finishing

```bash
$VS timing "$JOB"
$VS glossary-report "$JOB"
```

`glossary-report` measures the glossary: how many entries and of which kinds,
the characters it adds to every translation brief, entries the transcript never
contains, and for each stage how many cues that mention an entry use its
rendering. A mention is counted on what the recogniser heard, so each entry has
the same mentions at every stage, and a rendering counts as used whatever its
width, case, spacing or punctuation. `coherence` says how many translations the
coherence pass changed, how many only in typography, and how many changed a
glossary rendering.
Report its summary, and name any entry whose adherence drops between stages.

The dispatch rows are wall clock, not a sum: workers run together, so a stage
costs what its slowest worker costs. Each row also carries `first_done` and
`last_done`, taken from when the result files appeared. The gap between
`last_done` and `seconds` is time spent launching and noticing rather than
working, which is why every worker of a stage is launched in one message.

Report the actual summary or attach its final file, the absolute output paths,
the applicable timing and glossary tables, and anything that failed or needed a
rerun. For requested grouped timings, compute download plus ASR from recorded
stages, measure synthesis/review and linking/checking separately, and keep
failed attempts and launch/wait overhead visible. Measure cleanup only when
it runs; otherwise label it skipped. Do not allocate shared acquisition or ASR
time to individual video parts without measurements, and do not present a
dispatch interval containing failed workers as pure writing time.

If the media was downloaded and cleanup was requested or approved, run
`$VS cleanup "$JOB"`; it deletes downloaded media and scratch files and refuses
to touch a file the user supplied. Otherwise retain the media and give this
command as the cleanup option. A custom workspace path alone does not authorize
deleting files. Render timing rows as labeled lines or bullets in Discord,
where Markdown tables are not supported.

To answer a question about something on screen, grab a still:
`$VS frame "$JOB" --at 00:12:30`.

## When a stage fails

`apply-*` names the task directory that broke. Re-dispatch that one worker with
the same brief path; the brief and payload are still there and re-running the
plan command would throw away every other worker's answer.

A worker that reports it could not do the work writes `{"error": ...}` to its
result file. Read that one error file, fix what it names if you can, and
re-dispatch. Check `apply-*` even when the worker process exits zero; an error
result is still a failed stage. If the same error recurs on an unchanged retry,
stop blind re-dispatching and diagnose the named prerequisite. Preserve valid
part notes and resume at the failed stage. Do not read the full payload and
perform synthesis yourself as a workaround; completion still requires a
worker-produced result accepted by the apply and verification commands.

If `$VS new` fails on a URL, the download was blocked, not the skill. Say so
and ask the user for a file instead of retrying variations.

## Long videos

Nothing about the pipeline changes with length, but the task counts grow past
what some harnesses run at once. `--concurrency`, set at install, is how many
workers the harness runs together, and every plan spreads its work across that
many. A batch stops growing at a ceiling, past which one worker writes too much
in a single answer, and stops shrinking at a floor, past which it loses the
speech around what it is working on. When the ceiling still leaves more tasks
than slots, the plan reports `waves`. Follow it rather than working it out from
the task count.

## Options worth knowing

- `--size` on any plan command changes how many rows a worker gets. Smaller
  means more workers and more parallelism but less context for continuity.
- `--concurrency` is how many workers this harness runs at once. It is not a
  cap: the work is spread evenly across that many, so more slots means more and
  smaller tasks, down to the size below which a worker loses the surrounding
  speech it needs. Set once at install, so you rarely pass it.
- `--audio-track N` on `asr` transcribes a different track than the one chosen
  at acquire time.
- `--split-seconds` on `asr` cuts the audio for a provider that would otherwise
  take the whole file. It is faster on long media and slightly worse at the
  seams, so use it when the user cares more about wall clock than the last
  percent of accuracy.
- `--no-page-text` on the translate and proofread plans drops the video page
  description, which is otherwise offered to workers as a spelling reference
  for names.
- `--format` on `render` picks `srt` (the default) or `ass`. A later `render`
  without it keeps the job's last format. `--burn` always draws the styled ASS
  rendering into the video, whichever file format was asked for. Both formats
  put the translation on top and the source line under it, and break long
  lines themselves because players leave Chinese text unwrapped. ASS output
  fills `templates/movie.ass`, whose 1920-wide canvas keeps the type the same
  share of the frame at 4K, 1080p and 720p. `--font` replaces its 思源黑体 CN.
- `$VS status "$JOB"` says which stage a job reached, which is how you resume
  one you did not start.
- `$VS doctor --live` spends one short request per cloud provider proving the
  key still works, which is worth doing when a job fails at the asr step.

Rerunning a plan command deletes every result for that stage, including ones
that were fine. Use it to change a parameter, never to retry one worker.
`plan-proofread --only` is the one exception, described above.
