"""Guards on the wording that took the most tuning to get right.

The proofreader is the one worker that can quietly rewrite what a person said.
Every rule below was added after a run where the model did exactly that, so a
refactor that drops one should fail here rather than in a finished subtitle.
"""

from vstudio import prompts


def test_the_proofreader_is_told_its_input_is_asr():
    body = prompts.proofread(source_language="English", target_language="Chinese")
    assert "automatic speech recognition" in body
    assert "repair recognition errors not to edit what the speaker said" in body


def test_the_proofreader_may_not_expand_shorthand():
    body = prompts.proofread(source_language="English", target_language="Chinese")
    assert "Expanding shorthand into a longer official name is not" in body
    assert "Never add a word the speaker did not say" in body
    assert "Never invent a proper name" in body


def test_no_prompt_licenses_a_canonical_rewrite():
    """An earlier build let a term be replaced by "its canonical multi-word form",
    which is how a proofreader talks itself into renaming a speaker."""
    every = "\n".join([
        prompts.segment(language="English"),
        prompts.translate(source_language="English", target_language="Chinese"),
        prompts.proofread(source_language="English", target_language="Chinese"),
        prompts.summarize(language="English"),
        prompts.synthesize(title="t", language="English"),
    ])
    assert "canonical" not in every.lower()


def test_page_text_is_admitted_for_spelling_only():
    body = prompts.translate(
        source_language="English", target_language="Chinese", page_text="Subscribe to my channel"
    )
    assert "spelling only" in body
    assert "Never take content from it" in body


def test_page_text_is_absent_when_there_is_none():
    body = prompts.translate(source_language="English", target_language="Chinese")
    assert "Page text" not in body


def test_a_summary_worker_must_cite_timestamps():
    body = prompts.summarize(language="English")
    assert "(MM:SS)" in body
    assert "Every paragraph needs at least one citation" in body


def test_the_mechanical_stage_asks_the_worker_not_to_deliberate():
    """Effort is a property of an agent definition, and two of the three
    harnesses cannot set one per task, so the prompt is the only portable way
    to keep the cheap stage cheap."""
    body = prompts.segment(language="English")
    assert "This is a mechanical pass" in body
    assert "Do not deliberate" in body
    for other in (
        prompts.translate(source_language="English", target_language="Chinese"),
        prompts.proofread(source_language="English", target_language="Chinese"),
    ):
        assert "Do not deliberate" not in other
