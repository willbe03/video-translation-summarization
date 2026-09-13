import json

import pytest

from conftest import write_lines, write_result
from conftest import words as timed_words
from vstudio import glossary, pipeline, tasks

SPOKEN = "はるかちゃんはミモザで待ってる。ミモザは駅の前だよ。"
ENTRIES = [
    {"source": "はるかちゃん", "aliases": ["はるか"], "kind": "person", "translation": "小遥"},
    {"source": "ミモザ", "kind": "organization", "translation": "含羞草咖啡馆"},
]


def _transcribed(job, texts=SPOKEN):
    payload = {"provider": "fake", "language": "Japanese", "context": "", "duration": len(texts) * 0.5,
               "word_count": len(texts), "timings": {}, "windows": [], "words": timed_words(list(texts))}
    (job / "asr").mkdir()
    (job / "asr" / "words.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return job


def _glossed(job, entries=ENTRIES):
    pipeline.plan_glossary(job, source_language="Japanese", target_language="Chinese")
    write_result(tasks.task_dirs(job, "glossary")[0], {"entries": entries})
    return pipeline.apply_glossary(job)


def _segmented(job):
    pipeline.plan_segment(job, language="Japanese", size=100)
    write_lines(tasks.task_dirs(job, "segment")[0], ["はるかちゃんはミモザで待ってる", "ミモザは駅の前だよ"])
    pipeline.apply_segment(job)


def _proofread(job, translations=("小遥在含羞草咖啡馆等着", "含羞草就在车站前面")):
    pipeline.plan_translate(job, source_language="Japanese", target_language="Chinese", size=100)
    write_lines(tasks.task_dirs(job, "translate")[0], [f"{i}\t{t}" for i, t in enumerate(translations)])
    pipeline.apply_translate(job)
    pipeline.plan_proofread(job, source_language="Japanese", target_language="Chinese", size=100)
    write_result(tasks.task_dirs(job, "proofread")[0], {"checked": 2, "cues": []})
    return pipeline.apply_proofread(job)


def test_a_repeated_source_is_merged_and_a_second_rendering_is_reported():
    entries, problems = glossary.validate([
        {"source": "ミモザ", "translation": "含羞草咖啡馆"},
        {"source": "ミモザ", "aliases": ["みもざ"], "translation": "米莫萨"},
        {"source": "", "translation": "x"},
    ])
    assert [(e["source"], e["aliases"], e["translation"]) for e in entries] == [
        ("ミモザ", ["みもざ"], "含羞草咖啡馆")]
    assert any("two renderings" in p for p in problems)
    assert any("dropped" in p for p in problems)


def test_a_form_listed_under_two_entries_is_reported():
    _, problems = glossary.validate([
        {"source": "はるかちゃん", "aliases": ["はるか"], "translation": "小遥"},
        {"source": "はるか", "translation": "遥"},
    ])
    assert any("also occurs inside" in p for p in problems)


def test_a_form_inside_another_entrys_longer_form_is_reported():
    _, problems = glossary.validate([
        {"source": "ユキちゃん", "translation": "小雪"},
        {"source": "ユキオ", "aliases": ["ユキ"], "translation": "幸夫"},
    ])
    assert problems == ["'ユキ' under 'ユキオ' also occurs inside 'ユキちゃん' under 'ユキちゃん', "
                        "whose rendering does not contain its own"]


def test_a_given_name_inside_a_full_name_is_fine_when_the_renderings_nest():
    _, problems = glossary.validate([
        {"source": "明美", "translation": "明美"},
        {"source": "山田明美", "translation": "山田明美"},
    ])
    assert problems == []


def test_a_rendering_counts_as_used_whatever_its_punctuation():
    entries = [{"source": "ミモザの歌", "aliases": [], "kind": "work", "translation": "《含羞草之歌》"}]
    cues = [{"id": 0, "source": "ミモザの歌が好き", "translation": "我喜欢含羞草之歌"}]
    assert glossary.adherence(entries, cues)["ミモザの歌"]["followed"] == 1


def test_a_mention_belongs_to_the_longest_form_across_all_entries():
    entries = [{"source": "ユキちゃん", "aliases": [], "kind": "person", "translation": "小雪"},
               {"source": "ユキオ", "aliases": ["ユキ"], "kind": "person", "translation": "幸夫"}]
    rows = glossary.adherence(entries, [{"id": 0, "source": "ユキちゃんが来た", "translation": "小雪来了"}])
    assert (rows["ユキちゃん"]["mentions"], rows["ユキちゃん"]["followed"]) == (1, 1)
    assert rows["ユキオ"]["mentions"] == 0


def test_mentions_are_found_in_what_was_heard_not_in_an_edited_source():
    cues = [{"id": 0, "source_exact": "はるかちゃん", "source": "ハルカちゃん", "translation": "小遥"}]
    assert glossary.adherence(ENTRIES, cues)["はるかちゃん"]["followed"] == 1


def test_coherence_changes_are_counted_and_split_by_glossary_renderings():
    before = [{"id": 0, "translation": "阿遥来了"}, {"id": 1, "translation": "好的"},
              {"id": 2, "translation": "走吧"}, {"id": 3, "translation": "真的?"}]
    after = [{"id": 0, "translation": "小遥来了"}, {"id": 1, "translation": "好吧"},
             {"id": 2, "translation": "走吧"}, {"id": 3, "translation": "真的？"}]
    assert glossary.coherence_changes(ENTRIES, before, after) == {
        "translations_changed": 3, "typography_only": 1, "touching_glossary": 1}


def test_a_mention_is_counted_once_by_its_longest_form():
    assert glossary.occurrences(ENTRIES[:1], "はるかちゃんとはるか") == {"はるかちゃん": 2}


def test_adherence_counts_the_cues_that_use_the_rendering():
    cues = [{"id": 0, "source": "はるかちゃん", "translation": "小遥来了"},
            {"id": 1, "source": "はるか", "translation": "阿遥来了"},
            {"id": 2, "source": "駅", "translation": "车站"}]
    row = glossary.adherence(ENTRIES, cues)["はるかちゃん"]
    assert (row["mentions"], row["followed"], row["not_followed"]) == (2, 1, [1])


def test_the_glossary_reads_the_asr_words_and_never_the_cues(job):
    _transcribed(job)
    (job / "cues").mkdir()
    (job / "cues" / "cues.json").write_text(json.dumps([{"source": "a distinctive cue"}]), encoding="utf-8")
    plan = pipeline.plan_glossary(job, source_language="Japanese", target_language="Chinese")
    assert plan["agent"] == "vstudio-worker-deep" and len(plan["dispatch"]) == 1
    payload = (tasks.task_dirs(job, "glossary")[0] / "payload.txt").read_text(encoding="utf-8")
    assert "ミモザ" in payload and "a distinctive cue" not in payload


def test_applying_the_glossary_reports_quantity_and_names_the_transcript_lacks(job):
    _transcribed(job)
    result = _glossed(job, [*ENTRIES, {"source": "ツバメ", "kind": "place", "translation": "燕子"}])
    assert result["entries"] == 3 and result["kinds"] == {"person": 1, "organization": 1, "place": 1}
    assert result["not_in_transcript"] == ["ツバメ"]


def test_segmentation_leaves_a_slot_for_a_glossary_that_is_still_running(job):
    _transcribed(job)
    assert pipeline.plan_segment(job, language="Japanese", concurrency=4)["glossary_slot"] is False
    pipeline.plan_glossary(job, source_language="Japanese", target_language="Chinese")
    assert pipeline.plan_segment(job, language="Japanese", concurrency=4)["glossary_slot"] is True


def test_translation_waits_for_a_planned_glossary(job):
    _transcribed(job)
    pipeline.plan_glossary(job, source_language="Japanese", target_language="Chinese")
    _segmented(job)
    with pytest.raises(RuntimeError, match="apply-glossary"):
        pipeline.plan_translate(job, source_language="Japanese", target_language="Chinese")


def test_every_translation_and_proofreading_brief_carries_the_glossary(job):
    _transcribed(job)
    _glossed(job)
    _segmented(job)
    plan = pipeline.plan_translate(job, source_language="Japanese", target_language="Chinese", size=100)
    assert plan["glossary_entries"] == 2
    assert "はるかちゃん / はるか = 小遥" in (tasks.task_dirs(job, "translate")[0] / "brief.md").read_text()
    write_lines(tasks.task_dirs(job, "translate")[0], ["0\t小遥在含羞草咖啡馆等着", "1\t含羞草就在车站前面"])
    pipeline.apply_translate(job)
    pipeline.plan_proofread(job, source_language="Japanese", target_language="Chinese", size=100)
    assert "ミモザ = 含羞草咖啡馆" in (tasks.task_dirs(job, "proofread")[0] / "brief.md").read_text()


def test_coherence_reads_every_cue_and_changes_translations_only(job):
    _transcribed(job)
    _glossed(job)
    _segmented(job)
    _proofread(job)
    plan = pipeline.plan_cohere(job, source_language="Japanese", target_language="Chinese")
    assert plan["cues"] == 2 and len(plan["dispatch"]) == 1
    write_result(tasks.task_dirs(job, "cohere")[0], {
        "checked": 2,
        "cues": [{"id": 1, "translation": "含羞草咖啡馆就在车站前面", "source": "書き換え"},
                 {"id": 0, "translation": "小遥在含羞草咖啡馆等着！"}],
        "notes": [{"id": 0, "note": "who is waiting is unclear"}],
    })
    result = pipeline.apply_cohere(job)
    final = json.loads((job / "cues" / "final.json").read_text(encoding="utf-8"))
    assert (result["translations_changed"], result["typography_only"]) == (2, 1)
    assert not result["coverage_warnings"]
    assert final[1]["translation"] == "含羞草咖啡馆就在车站前面"
    assert final[1]["source"] == "ミモザは駅の前だよ"
    notes = json.loads((job / "cues" / "uncertainties.json").read_text(encoding="utf-8"))
    assert [(u["id"], u["stage"]) for u in notes] == [(0, "cohere")]


def test_coherence_rejects_a_cue_id_that_is_not_in_the_set(job):
    _transcribed(job)
    _glossed(job)
    _segmented(job)
    _proofread(job)
    pipeline.plan_cohere(job, source_language="Japanese", target_language="Chinese")
    write_result(tasks.task_dirs(job, "cohere")[0], {"checked": 2, "cues": [{"id": 9, "translation": "x"}]})
    with pytest.raises(RuntimeError, match="not in the set"):
        pipeline.apply_cohere(job)


def test_a_proofread_rerun_after_coherence_says_coherence_must_run_again(job):
    _transcribed(job)
    _glossed(job)
    _segmented(job)
    assert "cohere_needs_rerun" not in _proofread(job)
    pipeline.plan_cohere(job, source_language="Japanese", target_language="Chinese")
    assert pipeline.apply_proofread(job)["cohere_needs_rerun"] is True
