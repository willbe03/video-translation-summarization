# vstudio

The deterministic half of the `video-subtitles-and-summaries` skill. It
downloads and probes media, calls ASR endpoints, cuts work into task
directories for worker agents, checks what they return, and renders subtitles.

It never calls a language model. Every model step is a task directory an agent
picks up: `brief.md` for instructions, `payload.json` for data, `result.json`
for the answer.

```bash
uv sync                      # add --extra local-asr for on-device ASR
uv run vstudio doctor
uv run pytest tests -q
```

Layout:

- `cli.py` commands, one per pipeline step
- `jobs.py` job directories, the config file, stage timing
- `acquire.py` URL or local file to media plus metadata
- `media.py` ffmpeg, ffprobe, downloads, frames, cleanup
- `asr/` the backend contract, the registry, and the scheduler that runs them
- `pipeline.py` plan and apply for every model stage
- `prompts.py` what each worker is told
- `tasks.py` the brief, payload and result handoff
- `cues.py` words to cues, and the ASS renderer
- `verify.py` mechanical checks on a finished translation
- `summary.py` coverage checks on a finished summary
- `agents.py` worker definitions, rendered per harness
