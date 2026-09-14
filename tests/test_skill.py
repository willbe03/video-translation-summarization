"""Invariants for the video-subtitles-and-summaries skill.

This skill puts agents in charge and uses Python as a helper, so the contracts
worth pinning are architectural as well as editorial: the main session must
hand the job to an orchestrator, a worker must never be handed payload text or
a shell, and the prompt rules that stop a proofreader rewriting a speaker must
survive refactors.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[1]
HELPER = SKILL / "scripts" / "vstudio"
SRC = HELPER / "src" / "vstudio"

# A machine-specific path or an author's own identity in a published skill is
# both a privacy leak and a bug: it points every install at whoever built it.
# Matched by shape rather than by name, so this file carries no identity either.
HOME_PATH = re.compile(r"/(?:Users|home)/(?!<|runner\b)[a-z0-9_.-]+/", re.I)
EMAIL = re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", re.I)
TIME_SENSITIVE = re.compile(r"\bas of\b|\b(?:early|mid|late)\s+20\d\d|\b20\d\d(?![-\d])", re.I)
WINDOWS_PATH = re.compile(r"[A-Za-z0-9_.-]+\\[A-Za-z0-9_.-]+")


def _docs() -> list[Path]:
    return [SKILL / "SKILL.md", *sorted((SKILL / "references").glob("*.md"))]


def _frontmatter() -> dict:
    """The scalar frontmatter fields, parsed without a YAML dependency.

    The tests read only one-line string fields, and a published skill is easier
    to trust when running its checks needs nothing installed.
    """
    content = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    end = re.search(r"\n---\s*\n", content[3:])
    block = content[3 : end.start() + 3]
    fields: dict[str, str] = {}
    for line in block.splitlines():
        if line.startswith((" ", "\t")) or ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def _sources() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


# --- packaging ---


def test_the_skill_and_its_references_exist():
    assert (SKILL / "SKILL.md").is_file()
    for name in ("pipeline.md", "INSTALL.md", "harness-shared.md",
                 "harness-claude-code.md", "harness-codex.md", "harness-hermes.md"):
        assert (SKILL / "references" / name).is_file(), name


def test_the_helper_is_bundled_and_runnable_as_a_project():
    assert (HELPER / "pyproject.toml").is_file()
    assert (SRC / "cli.py").is_file()
    assert "vstudio = " in (HELPER / "pyproject.toml").read_text(encoding="utf-8")


def test_local_asr_is_an_optional_extra_not_a_hard_dependency():
    """Torch and the Qwen weights are gigabytes, and the user opts in at install."""
    text = (HELPER / "pyproject.toml").read_text(encoding="utf-8")
    base, _, extras = text.partition("[project.optional-dependencies]")
    declared = "\n".join(l for l in base.splitlines() if not l.lstrip().startswith("#"))
    assert "torch" not in declared and "transformers" not in declared
    assert "torch" in extras and "local-asr" in extras


def test_no_job_artifacts_or_virtualenv_are_committed():
    for pattern in (".venv", "runs", "tasks"):
        assert not (HELPER / pattern).exists() or pattern == ".venv" and (
            HELPER / ".gitignore"
        ).read_text(encoding="utf-8").count(".venv")


def test_evaluations_cover_more_than_one_scenario():
    scenarios = json.loads((SKILL / "evaluations.json").read_text(encoding="utf-8"))
    assert len(scenarios) >= 3
    for scenario in scenarios:
        assert scenario["skills"] == [_frontmatter()["name"]]
        assert scenario["query"] and scenario["expected_behavior"]


# --- authoring rules ---


def test_frontmatter_fields_and_description_rules():
    front = _frontmatter()
    name, description = front["name"], front["description"]
    assert re.fullmatch(r"[a-z0-9-]{1,64}", name)
    assert not re.search(r"anthropic|claude", name)
    assert 0 < len(description) <= 1024
    assert not re.search(r"\bI\b|\byou can\b", description)


def test_the_description_says_what_the_skill_does_and_when_to_use_it():
    """The description is all Claude reads when choosing a skill, so it names
    what comes out, that a file on disk is a valid source, and the requests
    that should load it."""
    lowered = _frontmatter()["description"].lower()
    for term in ("subtitle", "translat", "summar", "local", "use when"):
        assert term in lowered, term


def test_skill_body_stays_under_five_hundred_lines():
    assert len((SKILL / "SKILL.md").read_text(encoding="utf-8").splitlines()) < 500


def test_long_reference_files_open_with_a_table_of_contents():
    for path in _docs()[1:]:
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) > 100:
            assert "## Contents" in "\n".join(lines[:12]), path.name


def test_every_reference_is_linked_directly_from_the_skill():
    """Claude partially reads a file reached through another file, so keep them
    one level deep."""
    body = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    for path in (SKILL / "references").glob("*.md"):
        assert f"references/{path.name}" in body, path.name


@pytest.mark.parametrize("doc", _docs(), ids=lambda p: p.name)
def test_documents_avoid_dashes_windows_paths_and_bare_years(doc: Path):
    text = doc.read_text(encoding="utf-8")
    assert "—" not in text and "–" not in text
    body = "\n".join(
        line for line in text.splitlines() if not line.startswith("| ") and "`" not in line
    )
    assert not WINDOWS_PATH.search(body)
    assert not TIME_SENSITIVE.search(re.sub(r"`[^`]*`", "", body))


@pytest.mark.parametrize("path", [*_docs(), *_sources()], ids=lambda p: p.name)
def test_nothing_carries_a_personal_identifier(path: Path):
    text = path.read_text(encoding="utf-8")
    assert not HOME_PATH.search(text), "a home directory path would pin this to one machine"
    assert not EMAIL.search(text), "an address in a shipped file is the author's, not the user's"


def test_no_absolute_path_is_pinned_anywhere():
    """The artifacts directory comes from config, the skill finds itself at run
    time, and job directories record their own paths."""
    for path in [*_docs(), *_sources()]:
        text = path.read_text(encoding="utf-8")
        for match in re.findall(r'"(/[A-Za-z0-9_./-]{6,})"', text):
            assert match.startswith(("/opt/", "/usr/")), f"{path.name}: {match}"


# --- architecture ---


def test_the_skill_hands_the_job_to_an_orchestrator():
    body = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    assert "Do not run this job in the current session" in body
    assert "orchestrator subagent" in body


def test_a_worker_gets_read_and_write_and_no_shell(tmp_path):
    sys.path.insert(0, str(SRC.parent))
    from vstudio import agents

    for path in agents.install(tmp_path, harness="claude", skill_dir=tmp_path):
        if "worker" not in path.stem:
            continue
        assert "tools: Read, Write" in path.read_text(encoding="utf-8")
    assert "Do not use a shell for either, even if you have one" in " ".join(
        agents.SYSTEM_PROMPT.split()
    )
    assert "check your own answer by running code" in agents.SYSTEM_PROMPT


def test_a_brief_carries_instructions_and_the_payload_stays_a_file(tmp_path):
    sys.path.insert(0, str(SRC.parent))
    from vstudio import tasks

    directory = tasks.write_task(
        tmp_path, "translate", 0, 1, instructions="do it",
        payload=[{"id": 0, "source": "a distinctive sentence"}],
    )
    assert "a distinctive sentence" not in (directory / "brief.md").read_text(encoding="utf-8")
    assert (directory / "payload.jsonl").is_file()


def test_every_stage_declares_an_effort_suited_to_its_work():
    """Cutting cues is the only stage that thinks less. Translating and
    proofreading both decide what a person meant, and both get full effort."""
    sys.path.insert(0, str(SRC.parent))
    from vstudio import tasks

    from vstudio import agents

    assert agents.tier(agents.tier_of(tasks.STAGES["segment"].agent))[1] == "low"
    assert agents.tier(agents.tier_of(tasks.STAGES["translate"].agent))[1] == "high"
    assert agents.tier(agents.tier_of(tasks.STAGES["proofread"].agent))[1] == "high"
    for stage in ("glossary", "cohere"):
        assert tasks.STAGES[stage].agent == "vstudio-worker-deep"


def test_no_worker_runs_below_sonnet():
    sys.path.insert(0, str(SRC.parent))
    from vstudio import agents

    assert {model for model, _ in agents.TIER_DEFAULTS.values()} <= {"sonnet", "opus"}


def test_a_worker_cannot_run_a_command_on_claude_code(tmp_path):
    sys.path.insert(0, str(SRC.parent))
    from vstudio import agents

    for path in agents.install(tmp_path, harness="claude", skill_dir=tmp_path):
        if "worker" not in path.stem:
            continue
        banned = path.read_text(encoding="utf-8").split("disallowedTools:")[1].splitlines()[0]
        for tool in ("Bash", "BashOutput", "KillShell", "Task", "Agent"):
            assert tool in banned, f"{path.name} does not ban {tool}"


def test_the_helper_never_calls_a_model():
    """Python does the deterministic half. A model call here would put the
    architecture back the way it was."""
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        for marker in ("openai", "anthropic", "chat/completions", "messages.create"):
            assert marker not in text.lower(), f"{path.name} mentions {marker}"


def test_the_asr_registry_names_every_backend_the_documents_promise():
    sys.path.insert(0, str(SRC.parent))
    from vstudio import asr

    documented = (SKILL / "references" / "INSTALL.md").read_text(encoding="utf-8")
    for name in asr.backend_names():
        assert f"`{name}`" in documented, name


def test_no_prompt_licenses_rewriting_what_the_speaker_said():
    sys.path.insert(0, str(SRC.parent))
    from vstudio import prompts

    body = prompts.proofread(source_language="English", target_language="Chinese")
    assert "automatic speech recognition" in body
    assert "Expanding shorthand into a longer official name is not" in body
    assert "Never invent a proper name" in body


def test_the_cli_exposes_every_command_the_playbook_names():
    playbook = (SKILL / "references" / "pipeline.md").read_text(encoding="utf-8")
    install = (SKILL / "references" / "INSTALL.md").read_text(encoding="utf-8")
    help_text = subprocess.run(
        [sys.executable, "-c",
         f"import sys; sys.path.insert(0, {str(SRC.parent)!r});"
         "from vstudio.cli import build_parser; build_parser().print_help()"],
        capture_output=True, text=True, check=True,
    ).stdout
    named = set(re.findall(r"vstudio ([a-z][a-z-]+)", playbook + install))
    named -= {"doctor"}
    for command in named:
        assert command in help_text, command


def test_each_harness_has_its_own_file_and_names_its_own_launcher():
    """An orchestrator loads the shared contract plus one file, never all four."""
    launchers = {
        "harness-claude-code.md": "Agent(",
        "harness-codex.md": "~/.codex/agents",
        "harness-hermes.md": "delegate_task(",
    }
    for name, launcher in launchers.items():
        text = (SKILL / "references" / name).read_text(encoding="utf-8")
        assert launcher in text, name
        # No file may teach the reader another harness's mechanism.
        for other, foreign in launchers.items():
            if other != name:
                assert foreign not in text, f"{name} leaks {other}"


def test_the_shared_contract_carries_the_rules_that_do_not_vary():
    text = (SKILL / "references" / "harness-shared.md").read_text(encoding="utf-8")
    assert "Never put payload text in the launch message" in text
    assert "vstudio worker-prompt" in text
    assert "Launch every task in a plan at once" in text


def test_a_missing_worker_definition_never_blocks_a_job():
    """Definitions need a restart on Claude Code, so needing them would mean a
    fresh install could not run a job at all."""
    shared = (SKILL / "references" / "harness-shared.md").read_text(encoding="utf-8")
    assert "Definitions are an upgrade, not a requirement" in shared
    assert "Never wait for a restart" in shared
    claude = (SKILL / "references" / "harness-claude-code.md").read_text(encoding="utf-8")
    assert "Agent type not found" in claude
    assert "general-purpose" in claude
    body = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    assert "enforcement upgrade, not a prerequisite" in body


def test_every_harness_file_says_what_it_can_do_about_model_and_effort():
    """A plan asks for a model and an effort. Each platform can honour that to
    a different degree, and an orchestrator reads only its own file, so each
    has to answer the question rather than leaving it to the shared one."""
    for name in ("harness-claude-code.md", "harness-codex.md", "harness-hermes.md"):
        text = (SKILL / "references" / name).read_text(encoding="utf-8").lower()
        assert "model" in text and "effort" in text, name


def test_the_harness_that_can_honour_neither_says_so_plainly():
    """Hermes takes both from global config for every child at once, so an
    orchestrator there must not spend effort trying to tier its workers."""
    text = (SKILL / "references" / "harness-hermes.md").read_text(encoding="utf-8")
    assert "delegation.reasoning_effort" in text and "delegation.model" in text
    assert "cannot be honoured" in text
