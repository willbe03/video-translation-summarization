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
    assert {model for model, _ in agents.TIER_DEFAULTS.values()} == {"sonnet", "opus"}
    assert agents.TIER_DEFAULTS["light"] == ("sonnet", "low")


def test_install_can_be_undone(tmp_path):
    written = agents.install(tmp_path, harness="claude", skill_dir=tmp_path)
    (tmp_path / "someone-elses-agent.md").write_text("mine", encoding="utf-8")
    removed = agents.remove(tmp_path, harness="claude")
    assert sorted(removed) == sorted(written)
    assert (tmp_path / "someone-elses-agent.md").is_file()


def test_codex_workers_carry_the_same_prompt(tmp_path):
    written = agents.install(tmp_path, harness="codex", skill_dir=tmp_path)
    assert len(written) == len(agents.workers())
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


def test_the_effort_a_plan_reports_is_the_effort_the_worker_runs_at(tmp_path, monkeypatch):
    """A plan and its worker definition must agree even after a tier is
    changed, which is why the effort is derived from the tier rather than
    stored beside the stage."""
    from vstudio import tasks

    monkeypatch.setenv(agents.tier_env_name("deep"), "sonnet/medium")
    written = {p.stem: p.read_text() for p in
               agents.install(tmp_path, harness="claude", skill_dir=tmp_path)}
    for name, stage in tasks.STAGES.items():
        plan = tasks.plan(tmp_path, name, [])
        assert plan["agent"] == stage.agent, name
        assert f"effort: {plan['effort']}" in written[stage.agent], name
        assert f"model: {plan['model']}" in written[stage.agent], name
    assert tasks.plan(tmp_path, "proofread", [])["effort"] == "medium"


def test_every_stage_maps_onto_a_tier_that_ships():
    from vstudio.tasks import STAGES

    shipped = {w.name for w in agents.workers()}
    for name, stage in STAGES.items():
        assert stage.agent in shipped, f"{name} names a worker that does not ship"


def test_a_tier_can_be_changed_without_touching_python(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    jobs.write_config({agents.tier_env_name("light"): "opus/xhigh"})
    jobs.load_config()
    assert agents.tier("light") == ("opus", "xhigh")
    assert agents.worker("light").model == "opus"


def test_a_mistyped_tier_is_refused_rather_than_quietly_ignored():
    """The only symptom of a silently wrong tier is a bill."""
    for bad in ("sonnet", "sonnet/", "/low", "sonnet/lo", ""):
        with pytest.raises(RuntimeError):
            agents.parse_tier(bad)
    with pytest.raises(RuntimeError, match="unknown tier"):
        agents.tier("gigantic")


def test_the_defaults_are_what_was_measured():
    """Changing these is a decision, not a refactor: low effort on the light
    tier was a three quarter saving with no loss in cue quality."""
    assert agents.TIER_DEFAULTS == {
        "light": ("sonnet", "low"),
        "standard": ("sonnet", "high"),
        "deep": ("opus", "high"),
    }
