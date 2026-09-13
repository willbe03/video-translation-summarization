---
name: video-subtitles-and-summaries
description: Subtitles, translates and summarizes local or online video.
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

The job is long, it fans out to many workers, and every artifact it produces is
a file. Keeping it out of the main session is what makes the transcript of a
90 minute video cost you nothing.

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
pass on the output paths and the timing table.

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

## What the parts do

`scripts/vstudio` is a Python package that does everything with one right
answer: downloading, ffmpeg, calling the ASR endpoint, slicing work into task
directories, checking what came back, and rendering the ASS file. It never
calls a model.

Agents do everything with judgement: where a cue should break, how a line
should read in the target language, what an ASR mishearing was meant to be,
what a stretch of video is about.

Each worker is handed the path of a brief. The brief holds the instructions,
the payload file beside it holds the data, and the worker writes its answer to
a third file. Cue text never passes through the orchestrator's context, and a
worker never sees this skill, the job, or any other worker's task.
