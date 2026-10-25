---
name: video-subtitles-and-summaries
description: Transcribes, translates and summarizes local or online video into bilingual SRT or ASS subtitles or a cited summary. Use when the user asks to subtitle, translate, transcribe or summarize a video file or a YouTube or Bilibili link.
version: 1.0.0
author: willbe, Hermes Agent
license: MIT
platforms: [macos, linux]
prerequisites:
  commands: [ffmpeg, ffprobe, uv]
metadata:
  hermes:
    tags: [Video, Transcription, Translation, Subtitles, ASR, Summary]
    related_skills: []
---

# Video subtitles and summaries

Use this when the user asks to subtitle, translate, transcribe or summarize a
video, wants ASR output rather than the platform's own captions, or wants
subtitles burned into a video. The source can be a YouTube or Bilibili link or
any video or audio file on disk that ffmpeg can read.

Do not run this job in the current session. Launch one orchestrator subagent
and let it drive. This session's only work is to collect the parameters, launch
that subagent, and report what comes back.

The job is long and fans out to many workers, and every artifact it produces is
a file, so none of its intermediate content needs to pass through this session.

## Job scope and delivery

Use the video's spoken language unless a different output language is requested.
Use the selected audio and ASR detection, not the title or page language, to
choose it. Resolve conflicting language evidence before dispatching summaries.

Carry the user's style and length preference into both summary stages. Treat
"concise" as a length override rather than letting the per-minute synthesis
default expand the answer. Apply the user's writing standard to the final
report, through a `writing-style` skill where the harness has one.
Attribute opinions and reported events to the speaker; do not invent details,
speaker changes, or technical names from titles, metadata, or uncertain ASR.

Preserve the literal source URL and video ID. For a job-specific workspace,
pass `--job-root` to `new` without changing global settings, and keep task logs
and temporary files under that job. Workspace selection does not authorize
cleanup or require indefinite preservation; report cleanup as skipped unless
it was requested or approved.

Deliver the actual summary, or attach its final file, together with absolute
output paths. Include measured download plus transcription, summary writing,
and linking plus cleanup timings when requested. Distinguish per-video-part
worker timings from whole-job stages and shared download or ASR time; do not
invent a per-part allocation. Separate failed attempts and recovery overhead
from useful work, and mark unmeasured durations instead of reporting zero.

Require valid worker results, successful `apply-*` commands, and final coverage
and link checks before reporting completion. A worker process can exit zero
while writing an error result. Keep synthesis in a worker during recovery;
neither a small payload nor a failing reader permits the orchestrator to write
an unchecked replacement. After a correction, obtain an independent validation
of grounding, style and links from a reviewer on the deep tier's model before
declaring the output finished (on Hermes, the route `harness-hermes.md` names).

## Before the first job

If `references/INSTALL.md` has not been followed on this machine, follow it
now. It takes a few minutes, it asks the user one question about local ASR, and
nothing here works without it. To check: run

```bash
uv run --project scripts/vstudio vstudio doctor
```

from this skill's directory. If `settings` is empty or no provider is `ready`,
go to `references/INSTALL.md`.

## Launching the job

Collect these from what the user already said. Ask only about something that
changes the output and that you cannot reasonably assume.

- The source: a URL or a path to a local file.
- The task: bilingual subtitles, a summary, or both.
- For subtitles, the target language.
- For subtitles, the file format: SRT unless the user asks for ASS.
- Any instruction about tone, localization or terminology.
- Whether to burn the subtitles into the video, which is off unless asked.

Then launch a single subagent using the harness's own delegation mechanism,
which is `Agent` on Claude Code, `delegate_task` on Hermes, and a request for a
subagent in your prompt on Codex. Give it exactly this, with the placeholders
filled in and nothing else added:

```
Run a video job with the video-subtitles-and-summaries skill.

Skill directory: <absolute path of the directory holding this SKILL.md>
Source: <url or absolute file path>
Task: <subtitles | summary | both>
Target language: <language, or "n/a">
Subtitle format: <srt | ass, or "n/a">
Style: <tone, localization and terminology instructions, or "none">
Burn in: <yes | no>

Read <skill directory>/references/pipeline.md and follow it end to end.
Read <skill directory>/references/harness-shared.md and the one file for this
harness: harness-claude-code.md, harness-codex.md or harness-hermes.md.
Report the output paths, the timing table, and anything that failed.
```

Name the harness for it rather than making it guess. On Claude Code you can
instead launch the shipped `vstudio-orchestrator` agent, which already carries
those instructions; you only supply the parameters. If that agent type does not
exist yet, launch a general subagent with the message above and carry on: the
definitions are an enforcement upgrade, not a prerequisite.

While it runs, stay out of its way. Do not transcribe, translate or summarize
anything yourself, and do not read its intermediate artifacts. When it returns,
check the returned verification results and final output handle, then relay the
summary or attach the final file with the output paths, timings and failures.
A completion notification for a process is not proof that its model stage passed.
If the orchestrator reports that it patched this skill's own scripts (for
example a downloader workaround), deliver the summary first, then have an
independent reviewer on the deep tier's model check that code change before
treating the patch as kept; do not leave it as an unreviewed note in the report.

The one exception is an explicit instruction from the user to run the pipeline
in this session. Then follow `references/pipeline.md` yourself, and still send
every translation, proofreading, segmentation and summary brief to a worker.

## Reference files

- `references/pipeline.md`: the orchestrator's playbook, both branches, with
  the commands in order and what to do when a stage fails.
- `references/harness-shared.md`: the worker contract, which is the same
  everywhere. Read it with one of the next three.
- `references/harness-claude-code.md`: launching workers where `Agent` exists.
- `references/harness-codex.md`: launching workers in Codex CLI.
- `references/harness-hermes.md`: launching workers where `delegate_task` exists.
- `references/INSTALL.md`: setup, the local ASR question, credentials, and
  installing the worker definitions.

An orchestrator reads the shared file and its own harness file, never all four.
