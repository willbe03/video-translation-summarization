import json
from pathlib import Path

import pytest

from vstudio import jobs


def words(texts, *, step=0.5):
    return [
        {"index": i, "text": t, "start": round(i * step, 3), "end": round(i * step + step, 3)}
        for i, t in enumerate(texts)
    ]


@pytest.fixture
def job(tmp_path):
    directory = tmp_path / "job"
    directory.mkdir()
    jobs.write_manifest(directory, {"job": str(directory), "title": "Test video",
                                    "source_language": "English", "target_language": "Chinese",
                                    "duration": 60.0})
    return directory


@pytest.fixture
def transcribed(job):
    sample = "one two three. four five six seven. eight nine ten eleven twelve.".split()
    payload = {
        "provider": "fake", "language": "English", "context": "", "duration": 6.5,
        "word_count": len(sample), "timings": {},
        "windows": [{"index": 0, "start": 0.0, "duration": 6.5, "words": len(sample), "seconds": 1.0}],
        "words": words(sample),
    }
    (job / "asr").mkdir()
    (job / "asr" / "words.json").write_text(json.dumps(payload), encoding="utf-8")
    return job


def write_result(directory: Path, value) -> None:
    (directory / "result.json").write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def write_lines(directory: Path, lines) -> None:
    (directory / "result.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def cue_lines(texts, per_line):
    """Words written back with a break every `per_line` words."""
    return [" ".join(texts[i : i + per_line]) for i in range(0, len(texts), per_line)]
