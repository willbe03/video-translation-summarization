"""Command line for the deterministic half of the pipeline.

Every command takes a job directory, does one thing, and prints JSON on stdout
so the calling agent can act on the result without reading an artifact. Errors
print one sentence saying what to do next and exit 1, because an agent reading
a traceback learns nothing it can act on.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from vstudio import agents, asr, jobs, pipeline, tasks
from vstudio.acquire import acquire, describe_source
from vstudio.media import cleanup, media_resolution, resolve_ass_ffmpeg, run, take_frame
from vstudio.summary import check_summary
from vstudio.timestamps import linkify_summary
from vstudio.verify import verify_translation


def apply_stage(job: Path, stage: str, work) -> dict[str, Any]:
    """Run an apply step, recording the worker round and the checking separately."""
    dispatch = tasks.dispatch_timing(job, stage)
    with jobs.timed(job, f"apply-{stage}") as extra:
        result = work()
        extra.update({k: v for k, v in result.items() if not isinstance(v, list)})
    if dispatch is not None:
        seconds = dispatch.pop("seconds")
        jobs.record_timing(job, f"dispatch-{stage}", seconds,
                           tasks=len(tasks.task_dirs(job, stage)), **dispatch)
    return result


def emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def job_arg(value: str) -> Path:
    return Path(value).expanduser().resolve()


# --- commands ---


def cmd_configure(args: argparse.Namespace) -> int:
    values: dict[str, str] = {}
    if args.artifacts:
        root = Path(args.artifacts).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        values[jobs.ARTIFACTS_ENV] = str(root)
    if args.provider:
        values[jobs.PROVIDER_ENV] = args.provider
    path = jobs.write_config(values)
    emit({"config": str(path), "settings": jobs.load_config()})
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    report: dict[str, Any] = {
        "config": str(jobs.config_path()),
        "settings": jobs.load_config(),
        "artifacts_root": str(jobs.artifacts_root()),
        "skill_dir": str(_skill_dir()),
        "providers": {},
    }
    for name in ("ffmpeg", "ffprobe"):
        found = subprocess.run(["which", name], capture_output=True, text=True).stdout.strip()
        report[name] = found or None
    try:
        report["ffmpeg_with_ass"] = resolve_ass_ffmpeg()
    except RuntimeError as exc:
        report["ffmpeg_with_ass"] = None
        report["ffmpeg_with_ass_error"] = str(exc)
    for name in asr.backend_names():
        entry: dict[str, Any] = {}
        try:
            backend = asr.load_backend(name)
            entry["requirements"] = backend.capabilities.requirements
            backend.preflight()
            entry["ready"] = True
            describe = getattr(backend, "report", None)
            if describe is not None:
                entry.update(describe(live=args.live))
        except RuntimeError as exc:
            entry["ready"] = False
            entry["reason"] = str(exc)
        report["providers"][name] = entry
    report["artifacts_root_writable"] = os.access(
        jobs.artifacts_root().parent if not jobs.artifacts_root().exists() else jobs.artifacts_root(),
        os.W_OK,
    )
    emit(report)
    return 0


def cmd_worker_prompt(args: argparse.Namespace) -> int:
    """The instructions every worker gets, for a harness with no definition file
    and for the fallback when a definition has not loaded yet."""
    print(agents.SYSTEM_PROMPT)
    return 0


def cmd_install_agents(args: argparse.Namespace) -> int:
    skill_dir = Path(args.skill_dir).expanduser().resolve() if args.skill_dir else _skill_dir()
    dest = Path(args.dest).expanduser() if args.dest else agents.default_destination(args.harness)
    if args.remove:
        emit({"harness": args.harness, "destination": str(dest),
              "removed": [str(p) for p in agents.remove(dest, harness=args.harness)]})
        return 0
    written = agents.install(dest, harness=args.harness, skill_dir=skill_dir)
    emit({
        "harness": args.harness,
        "destination": str(dest),
        "written": [str(p) for p in written],
        "note": (
            "hermes needs no files; delegate_task carries the prompt inline"
            if args.harness == "hermes"
            else "Claude Code reads this directory at startup, so these take effect "
                 "in the next session. Until then workers fall back to the generic "
                 "subagent with `vstudio worker-prompt`."
            if args.harness == "claude" else None
        ),
    })
    return 0


def _skill_dir() -> Path:
    """The skill root: .../<skill>/scripts/vstudio/src/vstudio/cli.py."""
    return Path(__file__).resolve().parents[4]


def cmd_new(args: argparse.Namespace) -> int:
    described = describe_source(args.source)
    root = Path(args.job_root).expanduser() if args.job_root else None
    job = jobs.new_job_dir(args.title or described.get("title") or "video", root=root)
    jobs.write_manifest(job, {"job": str(job), "source": args.source, "stage": "acquiring"})
    with jobs.timed(job, "acquire", source_kind=described["kind"]):
        facts = acquire(args.source, job, cookies=Path(args.cookies) if args.cookies else None,
                        max_height=args.max_height)
    manifest = jobs.read_manifest(job)
    manifest.update(facts)
    manifest["stage"] = "acquired"
    if args.title:
        manifest["title"] = args.title
    jobs.write_manifest(job, manifest)
    job = jobs.rename_job(job, manifest["title"])
    manifest = jobs.read_manifest(job)
    emit({"job": str(job), "title": manifest["title"], "duration": manifest.get("duration"),
          "media": manifest["media"], "source_kind": manifest["source_kind"],
          "has_video": manifest.get("has_video"),
          "page_description": bool((manifest.get("page") or {}).get("description"))})
    return 0


def cmd_asr(args: argparse.Namespace) -> int:
    job = args.job
    manifest = jobs.read_manifest(job)
    provider = args.provider or os.environ.get("VSTUDIO_ASR_PROVIDER") or "mai"
    context = args.context if args.context is not None else str(manifest.get("asr_context") or "")
    with jobs.timed(job, "asr", provider=provider) as extra:
        payload = asr.transcribe(
            Path(manifest["media"]),
            job / "asr",
            provider=provider,
            title=str(manifest.get("title") or job.name),
            context=context,
            language=args.language,
            concurrency=args.concurrency,
            split_seconds=args.split_seconds,
        )
        extra["words"] = payload["word_count"]
    jobs.update_manifest(
        job,
        stage="transcribed",
        asr_provider=provider,
        source_language=payload["language"] or manifest.get("source_language") or "",
        word_count=payload["word_count"],
    )
    emit({"job": str(job), "provider": provider, "words": payload["word_count"],
          "language": payload["language"], "windows": len(payload["windows"]),
          "timings": payload["timings"], "transcript": str(job / "asr" / "transcript.md")})
    return 0


def _page_text(manifest: dict[str, Any]) -> str:
    page = manifest.get("page") or {}
    return " ".join(str(page.get(k) or "") for k in ("uploader", "description")).strip()


def cmd_plan_segment(args: argparse.Namespace) -> int:
    manifest = jobs.read_manifest(args.job)
    language = args.language or manifest.get("source_language") or "English"
    emit(pipeline.plan_segment(args.job, language=language, size=args.size))
    return 0


def cmd_apply_segment(args: argparse.Namespace) -> int:
    result = apply_stage(args.job, "segment", lambda: pipeline.apply_segment(args.job))
    jobs.update_manifest(args.job, stage="segmented", cue_count=result["cues"])
    emit(result)
    return 0


def cmd_plan_translate(args: argparse.Namespace) -> int:
    manifest = jobs.read_manifest(args.job)
    jobs.update_manifest(
        args.job,
        source_language=args.source_language or manifest.get("source_language") or "English",
        target_language=args.target_language,
        style=args.style or manifest.get("style") or "",
    )
    manifest = jobs.read_manifest(args.job)
    emit(pipeline.plan_translate(
        args.job,
        source_language=manifest["source_language"],
        target_language=manifest["target_language"],
        instructions=manifest.get("style") or "",
        page_text="" if args.no_page_text else _page_text(manifest),
        size=args.size,
    ))
    return 0


def cmd_apply_translate(args: argparse.Namespace) -> int:
    result = apply_stage(args.job, "translate", lambda: pipeline.apply_translate(args.job))
    jobs.update_manifest(args.job, stage="translated")
    emit(result)
    return 0


def cmd_plan_proofread(args: argparse.Namespace) -> int:
    manifest = jobs.read_manifest(args.job)
    emit(pipeline.plan_proofread(
        args.job,
        source_language=manifest.get("source_language") or "English",
        target_language=manifest.get("target_language") or "",
        instructions=manifest.get("style") or "",
        page_text="" if args.no_page_text else _page_text(manifest),
        size=args.size,
        only=args.only,
    ))
    return 0


def cmd_apply_proofread(args: argparse.Namespace) -> int:
    result = apply_stage(args.job, "proofread", lambda: pipeline.apply_proofread(args.job))
    jobs.update_manifest(args.job, stage="proofread")
    emit(result)
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    with jobs.timed(args.job, "render"):
        output = pipeline.render(args.job, font=args.font)
    result: dict[str, Any] = {"subtitles": str(output)}
    if args.burn:
        manifest = jobs.read_manifest(args.job)
        media = Path(manifest["media"])
        burned = args.job / "out" / f"{args.job.name}-subtitled.mp4"
        with jobs.timed(args.job, "burn"):
            size = media_resolution(media)
            if not size:
                raise RuntimeError(f"{media} has no video stream to burn subtitles into")
            run([resolve_ass_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                 "-i", str(media), "-vf", f"ass={_filter_path(output)}",
                 "-c:a", "copy", str(burned)])
        result["video"] = str(burned)
    jobs.update_manifest(args.job, stage="rendered", **result)
    emit(result)
    return 0


def _filter_path(path: Path) -> str:
    """Escape a path for use inside an ffmpeg filter argument.

    A colon separates filter options and a backslash escapes, so a job whose
    directory holds either would silently build the wrong filter graph.
    """
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def cmd_verify(args: argparse.Namespace) -> int:
    job = args.job
    manifest = jobs.read_manifest(job)
    final_path = job / "cues" / "final.json"
    if not final_path.is_file():
        final_path = job / "cues" / "translated.json"
    final = json.loads(final_path.read_text(encoding="utf-8"))
    draft_path = job / "cues" / "translated.json"
    draft = json.loads(draft_path.read_text(encoding="utf-8")) if draft_path.is_file() else None
    subs = manifest.get("subtitles")
    dialogue = codec = None
    if subs and Path(subs).is_file():
        text = Path(subs).read_text(encoding="utf-8-sig")
        dialogue = sum(1 for line in text.splitlines() if line.startswith("Dialogue:"))
        codec = "ass" if "[V4+ Styles]" in text else "unknown"
    words_path = job / "asr" / "words.json"
    words = (json.loads(words_path.read_text(encoding="utf-8"))["words"]
             if words_path.is_file() else None)
    report = verify_translation(manifest, final, draft, words,
                                ass_dialogue_count=dialogue, ass_codec=codec)
    emit(report)
    return 0 if report["ok"] else 1


def cmd_plan_summarize(args: argparse.Namespace) -> int:
    manifest = jobs.read_manifest(args.job)
    emit(pipeline.plan_summarize(
        args.job,
        language=args.language or manifest.get("source_language") or "English",
        instructions=args.style or "",
        seconds=args.seconds,
    ))
    return 0


def cmd_apply_summarize(args: argparse.Namespace) -> int:
    result = apply_stage(args.job, "summarize", lambda: pipeline.apply_summarize(args.job))
    emit(result)
    return 0


def cmd_plan_synthesize(args: argparse.Namespace) -> int:
    manifest = jobs.read_manifest(args.job)
    emit(pipeline.plan_synthesize(
        args.job,
        language=args.language or manifest.get("source_language") or "English",
        instructions=args.style or "",
    ))
    return 0


def cmd_apply_synthesize(args: argparse.Namespace) -> int:
    dispatch = tasks.dispatch_timing(args.job, "synthesize")
    with jobs.timed(args.job, "apply-synthesize"):
        output = pipeline.apply_synthesize(args.job)
    if dispatch is not None:
        jobs.record_timing(args.job, "dispatch-synthesize", dispatch.pop("seconds"),
                           tasks=1, **dispatch)
    manifest = jobs.read_manifest(args.job)
    text = output.read_text(encoding="utf-8")
    url, site = str(manifest.get("url") or ""), str(manifest.get("site") or "")
    linked = 0
    if url:
        text, linked = linkify_summary(text, url, site)
        output.write_text(text, encoding="utf-8")
    jobs.update_manifest(args.job, stage="summarized", summary=str(output))
    emit({"summary": str(output), "timestamps_linked": linked})
    return 0


def cmd_check_summary(args: argparse.Namespace) -> int:
    job = args.job
    manifest = jobs.read_manifest(job)
    asr_payload = json.loads((job / "asr" / "words.json").read_text(encoding="utf-8"))
    # The gap check compares where speech stops to how long the video runs, so
    # it has to read the words. A whole-file provider reports one window that
    # spans the video whether or not it transcribed any of it.
    transcript = [{"start": w["start"], "end": w["end"]} for w in asr_payload["words"]]
    summary_path = job / "out" / "summary.md"
    report = check_summary(
        manifest,
        transcript,
        summary_path.read_text(encoding="utf-8") if summary_path.is_file() else None,
    )
    emit(report)
    return 0 if report["ok"] else 1


def cmd_frame(args: argparse.Namespace) -> int:
    emit({"frame": str(take_frame(jobs.manifest_path(args.job), args.at))})
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    removed = cleanup(jobs.manifest_path(args.job), keep_video=args.keep_video)
    emit({"removed": [str(p) for p in removed]})
    return 0


def cmd_timing(args: argparse.Namespace) -> int:
    print(jobs.timing_report(args.job))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    manifest = jobs.read_manifest(args.job)
    present = {
        name: (args.job / rel).is_file()
        for name, rel in (
            ("transcript", "asr/transcript.md"),
            ("cues", "cues/cues.json"),
            ("translated", "cues/translated.json"),
            ("final", "cues/final.json"),
            ("summary", "out/summary.md"),
        )
    }
    emit({"job": str(args.job), "stage": manifest.get("stage"), "title": manifest.get("title"),
          "duration": manifest.get("duration"), "artifacts": present})
    return 0


# --- parser ---


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vstudio", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def job_command(name: str, handler, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("job", type=job_arg)
        p.set_defaults(handler=handler)
        return p

    p = sub.add_parser("doctor", help="report what is installed and which ASR providers are ready")
    p.add_argument("--live", action="store_true",
                   help="also send a one second clip to each cloud provider to prove the key works")
    p.set_defaults(handler=cmd_doctor)

    p = sub.add_parser("configure", help="record the artifacts directory and default ASR provider")
    p.add_argument("--artifacts", help="directory jobs are created in")
    p.add_argument("--provider", choices=asr.backend_names(), help="default ASR provider")
    p.set_defaults(handler=cmd_configure)

    p = sub.add_parser("install-agents", help="write the worker definitions for a harness")
    p.add_argument("--harness", required=True, choices=["claude", "codex", "hermes"])
    p.add_argument("--dest", help="defaults to the harness's own agent directory")
    p.add_argument("--skill-dir", help="defaults to the skill this package ships in")
    p.add_argument("--remove", action="store_true", help="delete the definitions install wrote")
    p.set_defaults(handler=cmd_install_agents)

    p = sub.add_parser("worker-prompt",
                       help="print the instructions a worker needs when no definition is loaded")
    p.set_defaults(handler=cmd_worker_prompt)

    p = sub.add_parser("new", help="create a job from a URL or a local file")
    p.add_argument("source")
    p.add_argument("--job-root", help=f"defaults to ${jobs.ARTIFACTS_ENV} or ./runs")
    p.add_argument("--title")
    p.add_argument("--cookies")
    p.add_argument("--max-height", type=int, default=720)
    p.set_defaults(handler=cmd_new)

    p = job_command("asr", cmd_asr, "transcribe the job's media")
    p.add_argument("--provider", choices=asr.backend_names())
    p.add_argument("--language", help="BCP-47 locale such as en-US; omit to auto-detect")
    p.add_argument("--context", help="ASR hotwords; omit to use the ones mined at acquire time")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--split-seconds", type=int,
                   help="cut the audio even when the provider does not need it; "
                        "faster on long media, slightly worse at the seams")

    p = job_command("plan-segment", cmd_plan_segment, "write cue-splitting tasks")
    p.add_argument("--language")
    p.add_argument("--size", type=int, default=pipeline.WORDS_PER_SEGMENT_TASK)
    job_command("apply-segment", cmd_apply_segment, "build cues from the segment results")

    p = job_command("plan-translate", cmd_plan_translate, "write translation tasks")
    p.add_argument("--target-language", required=True)
    p.add_argument("--source-language")
    p.add_argument("--style", help="tone, localization and terminology instructions")
    p.add_argument("--size", type=int, default=pipeline.CUES_PER_TRANSLATE_TASK)
    p.add_argument("--no-page-text", action="store_true")
    job_command("apply-translate", cmd_apply_translate, "merge the translation results")

    p = job_command("plan-proofread", cmd_plan_proofread, "write proofreading tasks")
    p.add_argument("--size", type=int, default=pipeline.CUES_PER_PROOFREAD_TASK)
    p.add_argument("--only", type=int, nargs="*", help="cue ids to revisit instead of all")
    p.add_argument("--no-page-text", action="store_true")
    job_command("apply-proofread", cmd_apply_proofread, "merge the proofreading results")

    p = job_command("render", cmd_render, "write the bilingual ass file")
    p.add_argument("--font", default="Arial")
    p.add_argument("--burn", action="store_true", help="also burn the subtitles into the video")

    job_command("verify", cmd_verify, "check the finished translation mechanically")

    p = job_command("plan-summarize", cmd_plan_summarize, "write per-part summary tasks")
    p.add_argument("--language")
    p.add_argument("--style")
    p.add_argument("--seconds", type=int, default=pipeline.SUMMARY_SECONDS_PER_TASK)
    job_command("apply-summarize", cmd_apply_summarize, "collect the per-part notes")

    p = job_command("plan-synthesize", cmd_plan_synthesize, "write the assembly task")
    p.add_argument("--language")
    p.add_argument("--style")
    job_command("apply-synthesize", cmd_apply_synthesize, "write summary.md and link its timestamps")
    job_command("check-summary", cmd_check_summary, "check that the summary covers the video")

    p = job_command("frame", cmd_frame, "grab a still for a visual question")
    p.add_argument("--at", required=True, help="timestamp such as 00:12:30")

    p = job_command("cleanup", cmd_cleanup, "delete downloaded media and scratch files")
    p.add_argument("--keep-video", action="store_true")

    job_command("timing", cmd_timing, "print how long each step took")
    job_command("status", cmd_status, "print the job's stage and artifacts")
    return parser


def main(argv: list[str] | None = None) -> int:
    jobs.load_config()
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except RuntimeError as exc:
        print(f"vstudio: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"vstudio: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
