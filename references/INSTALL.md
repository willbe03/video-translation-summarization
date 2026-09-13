# Installing

## Contents

- What has to be on the machine already
- Step 1: the Python environment
- Step 2: where jobs are written
- Step 3: the ASR provider, and the one question to ask
- Step 4: the worker definitions
- Step 5: check
- Moving the skill to another machine

Run every command from this skill's directory.

## What has to be on the machine already

`ffmpeg`, `ffprobe` and `uv`. Burning subtitles into a video also needs an
ffmpeg built with libass; `vstudio doctor` reports whether it found one, and
everything except `--burn` works without it.

## Step 1: the Python environment

```bash
uv sync --project scripts/vstudio
```

This installs a few megabytes. The virtual environment lives at
`scripts/vstudio/.venv`, inside the skill, so removing the skill removes it.

## Step 2: where jobs are written

Jobs hold the downloaded video, the audio, the transcript and every task file,
so a long video is a gigabyte or two. Ask the user where those should live, and
suggest a directory they already keep large working files in.

```bash
uv run --project scripts/vstudio vstudio configure --artifacts ~/<their choice>
```

Record how many workers this harness runs at once in the same command. Every
plan spreads its work across that many, so the number is what the skill uses,
not a limit it stays under. Hermes refuses a delegation carrying more tasks than
`delegation.max_concurrent_children`, ten by default. Claude Code and Codex
document no such limit, so pick what the machine and the account can carry.

```bash
uv run --project scripts/vstudio vstudio configure --concurrency 10
```

Left unset, batches fall back to a size that suits a short video, which on a
long one means many more tasks than some harnesses accept. Claude Code runs at
most 20 subagents at once, so use `--concurrency 20` there.

This writes `~/.config/vstudio/config.env`. A real environment variable always
beats the file, and `--job-root` on `vstudio new` beats both, so a single job
can be put somewhere else without changing the setting.

## Step 3: the ASR provider, and the one question to ask

Two backends ship. Ask the user which they want, and say what each costs:

- `mai` sends the audio to the MAI-Transcribe-2 endpoint on Azure Speech. It
  needs a key, it is fast, and the audio leaves the machine. Only the chosen
  audio track is sent, copied when it is already Opus, MP3 or FLAC under
  192 kbps and converted to 192 kbps Opus otherwise.
- `qwen-local` runs Qwen3-ASR and Qwen3-ForcedAligner on this machine. Nothing
  leaves it, and it costs about 5.5 GB of model weights, several gigabytes of
  PyTorch, and considerably more time per video.

For `mai`, put the credentials in the environment the skill will run in:

```
MAI_SPEECH_KEY=<key>
MAI_SPEECH_REGION=<region, for example westus>
```

`AZURE_SPEECH_KEY` and `AZURE_SPEECH_REGION` are read as fallbacks. Do not put
the key in `config.env`; that file is for settings, not secrets.

For `qwen-local`, install the extra. This is the only slow part of setup and
the reason it is opt-in:

```bash
uv sync --project scripts/vstudio --extra local-asr
```

The model weights download on the first run, not now.

Either way, record the choice:

```bash
uv run --project scripts/vstudio vstudio configure --provider mai
```

A third backend is a class with four methods; see `scripts/vstudio/src/vstudio/asr/base.py`.

## Step 4: the worker definitions

This step is optional. The skill runs without it, because a worker's
instructions travel in its launch message. What it buys is enforcement of those
instructions where the harness can manage it, which today means Claude Code.

```bash
uv run --project scripts/vstudio vstudio install-agents --harness claude
```

Use `--harness codex` on Codex. Hermes needs no files and the command says so.
`--remove` deletes exactly what install wrote and nothing else in that
directory.

On Claude Code this writes four definitions to `~/.claude/agents/`: three
workers and one orchestrator. A worker gets `tools: Read, Write`, every way of
running a command listed in `disallowedTools`, and `permissionMode:
acceptEdits`, which together mean it can write its answer without stopping to
ask and can do nothing else at all.

The three workers differ in model and in how hard they think. Cutting a
transcript into cues runs on Sonnet at low effort, translating and note-taking
on Sonnet at high effort, and proofreading and assembling a summary on Opus at
high effort. Sonnet is the floor: the cheap stage is cheap because it thinks
less, not because it knows less.

Rerun this command after updating the skill, since the orchestrator definition
carries the skill's path.

Claude Code reads `~/.claude/agents` when a session starts, so a session that
was already open when you ran this reports `Agent type not found`. That does
not block anything: the orchestrator falls back to a general subagent carrying
the same instructions and says so in its report. Start a new session when
convenient and the restricted workers take over.

## Step 5: check

```bash
uv run --project scripts/vstudio vstudio doctor
```

Every provider the user chose should report `ready: true`. A provider they did
not choose reporting `ready: false` is expected and fine.

`vstudio worker-prompt` prints the instructions every worker gets. Nothing
needs to be done with it at install time; the orchestrator uses it when it has
to launch a worker without a definition.

## Moving the skill to another machine

Copy the skill directory without `scripts/vstudio/.venv`, then repeat steps 1
through 5. Nothing in the skill or the code stores a path to a particular
machine: the artifacts directory comes from the config file, the skill's own
location is worked out at run time, and job directories record absolute paths
so a finished job can be moved anywhere.

Existing job directories are self-contained and can be copied across as well.
