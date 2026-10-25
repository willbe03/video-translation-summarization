"""The whole chain through the command line, with a fake recogniser.

Nothing here touches the network or a model. It exists so a refactor that
breaks the wiring between two commands fails in a second rather than in a
forty minute job.
"""

import json
import subprocess
from pathlib import Path

import pytest

from conftest import cue_lines, write_lines, write_result
from vstudio import asr, cli, jobs, tasks
from vstudio.asr.base import Capabilities, Window, Word

SPOKEN = (
    "Paper books feel different because the page does not glow. "
    "An e-reader is convenient but it is not the same thing at all. "
    "I have owned three of them and I still buy paperbacks."
).split()


class Recorder:
    capabilities = Capabilities(
        name="recorded", needs_segmentation=False, segment_seconds=None, max_parallel=1,
        returns_word_times=True, requirements="nothing", prefers_whole_file=True,
    )

    def preflight(self):
        return None

    def transcribe(self, window, *, context, language):
        words = [Word(w, i * 0.4, i * 0.4 + 0.35) for i, w in enumerate(SPOKEN)]
        return " ".join(SPOKEN), words, {"language": "English"}


@pytest.fixture
def media(tmp_path):
    """A second of silence is enough: the recogniser is faked, ffmpeg is not."""
    path = tmp_path / "clip.wav"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
         "-t", "1", str(path)],
        check=True,
    )
    return path


@pytest.fixture
def run(monkeypatch, tmp_path, capsys):
    """Run one command and return the JSON it printed, so nothing accumulates."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv(jobs.ARTIFACTS_ENV, str(tmp_path / "artifacts"))
    asr.register("recorded", Recorder)

    def invoke(*argv, expect=0):
        capsys.readouterr()
        code = cli.main([str(a) for a in argv])
        captured = capsys.readouterr()
        assert code == expect, f"{argv} exited {code}: {captured.err}"
        if code != 0:
            return captured.err
        return json.loads(captured.out) if captured.out.lstrip().startswith(("{", "[")) else captured.out

    return invoke


def test_a_local_file_goes_all_the_way_to_subtitles(run, media, capsys, tmp_path):
    job = Path(run("new", media, "--title", "Paper Books")["job"])
    assert job.name.endswith("-paper-books")

    assert run("asr", job, "--provider", "recorded")["words"] == len(SPOKEN)

    assert run("plan-glossary", job, "--target-language", "Chinese",
               "--style", "keep it casual")["agent"] == "vstudio-worker-deep"
    plan = run("plan-segment", job)
    assert plan["agent"] == "vstudio-worker-light" and plan["parallel"] is True
    assert plan["glossary_slot"] is True
    for directory in tasks.task_dirs(job, "segment"):
        words = (directory / "payload.txt").read_text(encoding="utf-8").split()
        write_lines(directory, cue_lines(words, 10))
    cue_count = run("apply-segment", job)["cues"]
    write_result(tasks.task_dirs(job, "glossary")[0],
                 {"entries": [{"source": "e-reader", "kind": "term", "translation": "电子阅读器"}]})
    assert run("apply-glossary", job)["entries"] == 1

    run("plan-translate", job, "--target-language", "Chinese", "--style", "keep it casual")
    assert "e-reader = 电子阅读器" in (tasks.task_dirs(job, "translate")[0] / "brief.md").read_text()
    for directory in tasks.task_dirs(job, "translate"):
        rows = [json.loads(l) for l in (directory / "payload.jsonl").read_text().splitlines()]
        write_lines(directory, [f"{r['id']}\t译{r['id']}" for r in rows])
    run("apply-translate", job)

    run("plan-proofread", job)
    for directory in tasks.task_dirs(job, "proofread"):
        ids = json.loads((directory / "meta.json").read_text())["ids"]
        write_result(directory, {"checked": len(ids), "cues": []})
    run("apply-proofread", job)

    assert run("plan-cohere", job)["cues"] == cue_count
    write_result(tasks.task_dirs(job, "cohere")[0], {"checked": cue_count, "cues": []})
    run("apply-cohere", job)

    subtitles = Path(run("render", job)["subtitles"])
    assert subtitles.suffix == ".srt"
    assert len(subtitles.read_text(encoding="utf-8").strip().split("\n\n")) == cue_count
    report = run("verify", job)
    assert report["ok"], report["failures"]
    assert report["details"]["subtitle_entries"] == cue_count

    subtitles = Path(run("render", job, "--format", "ass")["subtitles"])
    dialogue = [l for l in subtitles.read_text(encoding="utf-8-sig").splitlines()
                if l.startswith("Dialogue:")]
    assert len(dialogue) == cue_count
    assert Path(run("render", job)["subtitles"]).suffix == ".ass"

    report = run("verify", job)
    assert report["ok"], report["failures"]
    assert run("status", job)["artifacts"]["final"] is True

    assert run("plan-postcheck", job)["dispatch"] == []
    delivered = run("postprocess", job)
    assert delivered["subtitle_format"] == "ass" and delivered["lines"] == cue_count
    assert delivered["settings"] == {"enabled": True, "snap_gap_ms": 300, "lead_ms": 100, "lag_ms": 200}
    assert run("status", job)["artifacts"]["delivered"] is True
    measured = run("glossary-report", job)
    assert measured["entries"] == 1 and set(measured["adherence"]) == {"translated", "proofread", "final"}
    assert measured["coherence"] == {"translations_changed": 0, "typography_only": 0, "touching_glossary": 0}


def test_the_style_instruction_reaches_the_worker_brief(run, media):
    job = Path(run("new", media, "--title", "Paper Books")["job"])
    run("asr", job, "--provider", "recorded")
    run("plan-segment", job)
    for directory in tasks.task_dirs(job, "segment"):
        write_lines(directory, [(directory / "payload.txt").read_text(encoding="utf-8")])
    run("apply-segment", job)
    run("plan-translate", job, "--target-language", "Chinese",
        "--style", "translate gg as gg, never good game")
    brief = (tasks.task_dirs(job, "translate")[0] / "brief.md").read_text()
    assert "never good game" in brief


def test_the_summary_branch_runs_without_cues(run, media):
    job = Path(run("new", media, "--title", "Paper Books")["job"])
    run("asr", job, "--provider", "recorded")

    run("plan-summarize", job)
    for directory in tasks.task_dirs(job, "summarize"):
        (directory / "result.md").write_text("The speaker prefers paper (00:00)", encoding="utf-8")
    run("apply-summarize", job)

    run("plan-synthesize", job)
    for directory in tasks.task_dirs(job, "synthesize"):
        (directory / "result.md").write_text("They prefer paper books (00:00)", encoding="utf-8")
    summary = Path(run("apply-synthesize", job)["summary"])
    assert "paper books" in summary.read_text()

    assert run("check-summary", job)["ok"]


def test_synthesis_writes_in_the_language_the_notes_were_planned_in(run, media):
    job = Path(run("new", media, "--title", "Paper Books")["job"])
    run("asr", job, "--provider", "recorded")
    run("plan-summarize", job, "--language", "Chinese")
    for directory in tasks.task_dirs(job, "summarize"):
        (directory / "result.md").write_text("笔记 (00:00)", encoding="utf-8")
    run("apply-summarize", job)
    run("plan-synthesize", job)
    assert "in Chinese" in (tasks.task_dirs(job, "synthesize")[0] / "brief.md").read_text(encoding="utf-8")


def test_a_missing_result_exits_one_with_a_sentence(run, media):
    job = Path(run("new", media, "--title", "Paper Books")["job"])
    run("asr", job, "--provider", "recorded")
    run("plan-segment", job)
    assert "did not write its answer" in run("apply-segment", job, expect=1)


def test_a_typo_for_a_path_is_not_treated_as_a_url(run):
    assert "neither a readable file nor a URL" in run("new", "~/Moives/typo.mkv", expect=1)


def test_cleanup_refuses_to_delete_the_users_own_file(run, media):
    job = Path(run("new", media, "--title", "Paper Books")["job"])
    run("cleanup", job)
    assert media.exists()
    assert jobs.read_manifest(job)["source_preserved"] is True


def test_the_timing_table_separates_the_worker_round_from_the_checking(run, media):
    job = Path(run("new", media, "--title", "Paper Books")["job"])
    run("asr", job, "--provider", "recorded")
    run("plan-segment", job)
    for directory in tasks.task_dirs(job, "segment"):
        write_lines(directory, [(directory / "payload.txt").read_text(encoding="utf-8")])
    run("apply-segment", job)
    steps = [e["step"] for e in json.loads((job / "timing.json").read_text())]
    assert "asr" in steps and "dispatch-segment" in steps and "apply-segment" in steps
    report = run("timing", job)
    assert "dispatch-segment" in report and "total" in report


def test_a_url_job_links_its_citations_and_counts_them(run, media, capsys, monkeypatch):
    """The summary branch was only ever tested on a local file, which has no
    url, so `if url:` never ran and a call that could not work sat in it."""
    job = Path(run("new", media, "--title", "Paper Books")["job"])
    manifest = jobs.read_manifest(job)
    manifest.update({"url": "https://www.youtube.com/watch?v=abc", "site": "YouTube",
                     "duration": 300.0})
    jobs.write_manifest(job, manifest)
    run("asr", job, "--provider", "recorded")

    run("plan-summarize", job)
    for directory in tasks.task_dirs(job, "summarize"):
        (directory / "result.md").write_text("Paper wins (00:05)", encoding="utf-8")
    run("apply-summarize", job)
    run("plan-synthesize", job)
    for directory in tasks.task_dirs(job, "synthesize"):
        (directory / "result.md").write_text(
            "Opens on paper (00:05) and closes on it (02:30), with one bad cite (99:00)",
            encoding="utf-8",
        )
    result = run("apply-synthesize", job)

    assert result["timestamps_linked"] == 2, "the two citations inside the video"
    assert [p["kind"] for p in result["citation_problems"]] == ["out_of_range"]
    summary = Path(result["summary"]).read_text()
    assert "](https://www.youtube.com/watch?v=abc" in summary
    assert "(99:00)" in summary, "a citation past the end is reported, not linked"


def test_linking_a_summary_twice_changes_nothing(run, media):
    """apply-synthesize is re-run after a redispatch, so the pass has to be
    safe to repeat."""
    job = Path(run("new", media, "--title", "Paper Books")["job"])
    manifest = jobs.read_manifest(job)
    manifest.update({"url": "https://www.youtube.com/watch?v=abc", "site": "YouTube",
                     "duration": 300.0})
    jobs.write_manifest(job, manifest)
    run("asr", job, "--provider", "recorded")
    run("plan-summarize", job)
    for directory in tasks.task_dirs(job, "summarize"):
        (directory / "result.md").write_text("Paper wins (00:05)", encoding="utf-8")
    run("apply-summarize", job)
    run("plan-synthesize", job)
    for directory in tasks.task_dirs(job, "synthesize"):
        (directory / "result.md").write_text("Opens on paper (00:05)", encoding="utf-8")
    first = run("apply-synthesize", job)
    again = run("apply-synthesize", job)
    assert first["timestamps_linked"] == again["timestamps_linked"] == 1
    assert Path(first["summary"]).read_text() == Path(again["summary"]).read_text()
