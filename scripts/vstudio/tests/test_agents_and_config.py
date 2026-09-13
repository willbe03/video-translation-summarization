from pathlib import Path

import pytest

from vstudio import agents, jobs, tasks


def test_a_real_environment_variable_beats_the_config_file(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    jobs.write_config({jobs.ARTIFACTS_ENV: "/from/file"})
    monkeypatch.setenv(jobs.ARTIFACTS_ENV, "/from/env")
    jobs.load_config()
    assert jobs.artifacts_root() == Path("/from/env")


def test_configure_keeps_settings_it_was_not_asked_to_change(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    jobs.write_config({jobs.ARTIFACTS_ENV: "/artifacts", jobs.PROVIDER_ENV: "mai"})
    jobs.write_config({jobs.PROVIDER_ENV: "qwen-local"})
    text = jobs.config_path().read_text()
    assert "/artifacts" in text and "qwen-local" in text


def test_every_stage_maps_to_a_worker_that_ships(tmp_path):
    written = agents.install(tmp_path, harness="claude", skill_dir=tmp_path)
    names = {p.stem for p in written}
    for stage in tasks.STAGES.values():
        assert stage.agent in names


def test_claude_workers_get_read_and_write_and_nothing_else(tmp_path):
    for path in agents.install(tmp_path, harness="claude", skill_dir=tmp_path):
        text = path.read_text()
        if "worker" not in path.stem:
            continue
        assert "tools: Read, Write" in text
        # Named again in disallowedTools so the ban survives a release that
        # widens what an allowlist implies.
        banned = text.split("disallowedTools:")[1].splitlines()[0]
        for tool in ("Bash", "BashOutput", "KillShell", "Task", "Agent"):
            assert tool in banned, tool


def test_no_worker_runs_below_sonnet():
    """The cheap stage is cheap because it thinks less, not because it knows
    less, so a smaller model saves nothing and costs cue quality."""
    assert {w.model for w in agents.WORKERS} == {"sonnet", "opus"}
    light = next(w for w in agents.WORKERS if w.name.endswith("light"))
    assert light.model == "sonnet" and light.effort == "low"


def test_install_can_be_undone(tmp_path):
    written = agents.install(tmp_path, harness="claude", skill_dir=tmp_path)
    (tmp_path / "someone-elses-agent.md").write_text("mine", encoding="utf-8")
    removed = agents.remove(tmp_path, harness="claude")
    assert sorted(removed) == sorted(written)
    assert (tmp_path / "someone-elses-agent.md").is_file()


def test_codex_workers_carry_the_same_prompt(tmp_path):
    written = agents.install(tmp_path, harness="codex", skill_dir=tmp_path)
    assert len(written) == len(agents.WORKERS)
    text = written[0].read_text()
    assert "model_reasoning_effort" in text
    assert "Do not use a shell for either" in text


def test_hermes_needs_no_files(tmp_path):
    assert agents.install(tmp_path, harness="hermes", skill_dir=tmp_path) == []


def test_an_unknown_harness_lists_the_known_ones(tmp_path):
    with pytest.raises(RuntimeError, match="claude, codex, hermes"):
        agents.install(tmp_path, harness="emacs", skill_dir=tmp_path)


def test_the_worker_prompt_forbids_self_verification():
    assert "check your own answer by running code" in agents.SYSTEM_PROMPT
    assert "Do not read, search for, or ask for any other file." in agents.SYSTEM_PROMPT


def test_the_worker_prompt_holds_where_the_harness_cannot_take_the_shell_away():
    """Hermes cannot restrict a child's tools, and a worker that keeps its shell
    reads with cat, writes with a heredoc, and drafts to a temporary file."""
    prompt = " ".join(agents.SYSTEM_PROMPT.split())
    assert "Do not use a shell for either, even if you have one" in prompt
    assert "Write nothing anywhere except the one result file the brief names" in prompt
