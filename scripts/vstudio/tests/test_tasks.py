import json

import pytest

from vstudio import tasks


def test_a_brief_never_carries_the_payload(tmp_path):
    directory = tasks.write_task(
        tmp_path, "translate", 0, 1,
        instructions="Translate these", payload=[{"id": 0, "source": "secret words"}],
    )
    brief = (directory / "brief.md").read_text()
    assert "secret words" not in brief
    assert "payload.jsonl" in brief
    assert tasks.STAGES["translate"].result in brief


def test_a_text_payload_is_written_as_plain_text_and_named_in_the_brief(tmp_path):
    directory = tasks.write_task(tmp_path, "segment", 0, 1, instructions="go", payload="some words")
    assert (directory / "payload.txt").read_text(encoding="utf-8") == "some words"
    assert not (directory / "payload.jsonl").exists()
    assert "payload.txt" in (directory / "brief.md").read_text()


def test_the_brief_forbids_reading_anything_else(tmp_path):
    directory = tasks.write_task(tmp_path, "translate", 0, 1, instructions="go", payload=[])
    brief = " ".join((directory / "brief.md").read_text().split())
    assert "Do not read any other file" in brief
    assert "do not run any command" in brief
    assert "write it with your file writing tool rather than through a shell" in brief


def test_planning_again_clears_a_stale_result(tmp_path):
    first = tasks.write_task(tmp_path, "translate", 0, 1, instructions="go", payload=[{"id": 0}])
    (first / "result.json").write_text("[]", encoding="utf-8")
    again = tasks.write_task(tmp_path, "translate", 0, 1, instructions="go", payload=[{"id": 0}])
    assert not (again / "result.json").exists()


@pytest.mark.parametrize(
    "raw",
    ['{"translations":[]}', '```json\n{"translations":[]}\n```', 'Sure!\n{"translations":[]}\n'],
)
def test_a_result_survives_a_fence_or_a_greeting(tmp_path, raw):
    directory = tmp_path / "000"
    directory.mkdir()
    (directory / "result.json").write_text(raw, encoding="utf-8")
    assert tasks.read_result(directory) == {"translations": []}


def test_a_missing_result_names_the_task_to_re_dispatch(tmp_path):
    directory = tmp_path / "007"
    directory.mkdir()
    with pytest.raises(RuntimeError, match="007"):
        tasks.read_result(directory)


def test_every_stage_declares_an_effort_and_a_worker():
    for name, stage in tasks.STAGES.items():
        assert stage.effort in {"low", "medium", "high"}, name
        assert stage.agent.startswith("vstudio-worker-"), name
