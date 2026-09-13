# Video subtitles and summaries

An agent skill that transcribes a video or audio file with word-level
timestamps, then either translates it into bilingual ASS subtitles or writes a
cited summary. The source can be a YouTube or Bilibili link or any file on disk
that ffmpeg can read.

It runs on Claude Code, Codex and Hermes. There is no API key for translation:
the work is done by whatever agents the surrounding harness can launch.

## What makes it different

Agents are in charge and Python is the helper, which is the opposite of how
this kind of pipeline is usually built.

When a job starts, the session hands the whole thing to one orchestrator
subagent and stays out of the way. The orchestrator runs `vstudio` for
everything with a single right answer, and delegates everything that needs
judgement. Python downloads media, calls the ASR endpoint, cuts work into task
directories, checks what comes back, and renders the subtitles. It never calls
a model.

Each worker is handed one line: the path of a brief. The brief holds the
instructions, the payload file beside it holds the data, and the worker writes
its answer to a third file. Cue text never passes through the orchestrator's
context, and a worker never sees the skill, the job, or another worker's task.
Workers get file read and file write and, where the harness can enforce it,
nothing else.

## Installing

Needs `ffmpeg`, `ffprobe` and `uv`. Follow `references/INSTALL.md`, which takes
a few minutes and asks one question about whether to run ASR locally.

```bash
uv sync --project scripts/vstudio
uv run --project scripts/vstudio vstudio configure --artifacts ~/where/jobs/go
uv run --project scripts/vstudio vstudio doctor
```

As an agent skill, copy this directory into your harness's skills directory and
invoke it by asking for a video to be subtitled or summarized.

## Transcription backends

`mai` sends audio to MAI-Transcribe-2 on Azure Speech. It needs a key, it is
fast, and the audio leaves the machine. Only the chosen audio track goes up,
copied when it is already Opus, MP3 or FLAC under 192 kbps and converted to
192 kbps Opus otherwise.

`qwen-local` runs Qwen3-ASR and a forced aligner on your own machine. Nothing
leaves it, and it costs about 5.5 GB of weights plus several gigabytes of
PyTorch, which is why it is an optional extra rather than a dependency.

A third backend is one class with four methods. See
`scripts/vstudio/src/vstudio/asr/base.py`.

## Layout

- `SKILL.md` what the session reads, which mostly says to delegate
- `references/pipeline.md` the orchestrator's playbook, both branches
- `references/harness-shared.md` the worker contract, plus one file per harness
- `references/INSTALL.md` setup
- `scripts/vstudio` the deterministic half, with its own tests
- `tests/test_skill.py` invariants on the skill itself

## Tests

```bash
uv run --project scripts/vstudio python -m pytest tests scripts/vstudio/tests -q
```

## License

MIT. Parts derive from the Hermes Agent optional skills, whose copyright notice
is kept in `LICENSE`.
