import json

import pytest

from conftest import cue_lines, write_lines, write_result
from conftest import words as timed_words
from vstudio import jobs, pipeline, tasks
from vstudio import prompts
from vstudio.cues import build_cues, lexical_weight, repair_boundaries, strip_punctuation


def _results(job, stage, values):
    for directory, value in zip(tasks.task_dirs(job, stage), values):
        if isinstance(value, list):
            write_lines(directory, value)
        else:
            write_result(directory, value)


def _segment_lines(job, per_line=4):
    """Answer every segment task by writing its words back with breaks."""
    for directory in tasks.task_dirs(job, "segment"):
        words = (directory / "payload.txt").read_text(encoding="utf-8").split()
        write_lines(directory, cue_lines(words, per_line))


def test_word_batches_prefer_a_sentence_end(transcribed):
    words = json.loads((transcribed / "asr" / "words.json").read_text())["words"]
    ranges = pipeline.batch_words(words, 4)
    assert ranges[0] == (0, 2)  # "three." ends the first sentence
    assert ranges[0][0] == 0 and ranges[-1][1] == len(words) - 1
    for earlier, later in zip(ranges, ranges[1:]):
        assert later[0] == earlier[1] + 1


def test_word_batches_cover_everything_when_there_is_no_punctuation(tmp_path):
    words = [{"text": f"w{i}"} for i in range(37)]
    ranges = pipeline.batch_words(words, 10)
    covered = [i for start, end in ranges for i in range(start, end + 1)]
    assert covered == list(range(37))


def test_segment_round_trip_builds_cues(transcribed):
    plan = pipeline.plan_segment(transcribed, language="English", size=100)
    assert plan["agent"] == "vstudio-worker-light"
    assert len(plan["dispatch"]) == 1
    _results(transcribed, "segment", [[
        "one two three.", "four five six seven.", "eight nine ten eleven twelve.",
    ]])
    result = pipeline.apply_segment(transcribed)
    assert result["cues"] == 3
    cues = json.loads((transcribed / "cues" / "cues.json").read_text())
    assert cues[0]["start"] == 0.0
    assert cues[-1]["source_exact"].startswith("eight")
    assert result["boundary_repairs"] == []


def test_a_dropped_word_joins_the_cue_before_it_and_is_reported(transcribed):
    """Line breaks cannot produce a gap or an overlap, only a missing word."""
    pipeline.plan_segment(transcribed, language="English", size=100)
    _results(transcribed, "segment", [[
        "one two three.", "four six seven.", "eight nine ten eleven twelve.",
    ]])
    result = pipeline.apply_segment(transcribed)
    assert any("1 of 12 words were not written back" in n for n in result["boundary_repairs"])
    cues = json.loads((transcribed / "cues" / "cues.json").read_text())
    assert [(c["start_word"], c["end_word"]) for c in cues] == [(0, 2), (3, 6), (7, 11)]


def test_an_invented_word_is_reported_and_changes_no_cue(transcribed):
    pipeline.plan_segment(transcribed, language="English", size=100)
    _results(transcribed, "segment", [[
        "one two three.", "four five six seven INVENTED.", "eight nine ten eleven twelve.",
    ]])
    result = pipeline.apply_segment(transcribed)
    assert any("not in the transcript" in n for n in result["boundary_repairs"])
    cues = json.loads((transcribed / "cues" / "cues.json").read_text())
    assert [(c["start_word"], c["end_word"]) for c in cues] == [(0, 2), (3, 6), (7, 11)]
    assert "INVENTED" not in cues[1]["source"]


def test_punctuation_and_case_do_not_stop_a_word_matching(transcribed):
    pipeline.plan_segment(transcribed, language="English", size=100)
    _results(transcribed, "segment", [["One, two, THREE!", "four five six seven", "eight nine ten eleven twelve"]])
    result = pipeline.apply_segment(transcribed)
    assert result["boundary_repairs"] == []
    assert result["cues"] == 3


def test_a_worker_that_wrote_nothing_is_named_for_re_dispatch(transcribed):
    pipeline.plan_segment(transcribed, language="English", size=100)
    (tasks.task_dirs(transcribed, "segment")[0] / "result.txt").write_text("  \n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="000"):
        pipeline.apply_segment(transcribed)


def test_a_segmenter_cannot_move_a_timestamp(transcribed):
    """A cue break is a line break, so there is nowhere to put a timestamp."""
    pipeline.plan_segment(transcribed, language="English", size=100)
    _results(transcribed, "segment", [["one two three four five six seven eight nine ten eleven twelve"]])
    pipeline.apply_segment(transcribed)
    cues = json.loads((transcribed / "cues" / "cues.json").read_text())
    assert cues[0]["start"] == 0.0 and cues[-1]["end"] == 6.0


def test_the_cue_text_always_comes_from_the_recogniser(transcribed):
    pipeline.plan_segment(transcribed, language="English", size=100)
    _results(transcribed, "segment", [["one two three", "four five six seven",
                                       "eight nine ten eleven twelve"]])
    pipeline.apply_segment(transcribed)
    cues = json.loads((transcribed / "cues" / "cues.json").read_text())
    for cue in cues:
        assert cue["source"] == strip_punctuation(cue["source_exact"])


def _segmented(job):
    pipeline.plan_segment(job, language="English", size=100)
    _results(job, "segment", [["one two three.", "four five six seven.",
                               "eight nine ten eleven twelve."]])
    pipeline.apply_segment(job)
    return job


def test_translate_names_the_ids_that_came_back_empty(transcribed):
    job = _segmented(transcribed)
    pipeline.plan_translate(job, source_language="English", target_language="Chinese", size=100)
    _results(job, "translate", [["0\t一二三"]])
    with pytest.raises(RuntimeError, match=r"\[1, 2\]"):
        pipeline.apply_translate(job)


def test_translate_accepts_a_bare_list_and_records_uncertainties(transcribed):
    job = _segmented(transcribed)
    pipeline.plan_translate(job, source_language="English", target_language="Chinese", size=100)
    _results(job, "translate", [["0\t一", "1\t二", "2\t三", "? 1 what is on screen here"]])
    result = pipeline.apply_translate(job)
    assert result["cues"] == 3 and result["uncertainties"] == 1


@pytest.mark.parametrize("tag", ["</content>", "&lt;/content&gt;"])
def test_a_stray_closing_tag_from_a_worker_is_not_a_cue_line(transcribed, tag):
    job = _segmented(transcribed)
    pipeline.plan_translate(job, source_language="English", target_language="Chinese", size=100)
    _results(job, "translate", [["0\t一", "1\t二", "2\t三", tag]])
    assert pipeline.apply_translate(job)["cues"] == 3


def _translated(job):
    job = _segmented(job)
    pipeline.plan_translate(job, source_language="English", target_language="Chinese", size=100)
    _results(job, "translate", [["0\t一", "1\t二", "2\t三"]])
    pipeline.apply_translate(job)
    return job


def test_proofread_context_rows_are_marked_and_never_returnable(transcribed):
    job = _translated(transcribed)
    pipeline.plan_proofread(job, source_language="English", target_language="Chinese", size=1)
    first = tasks.task_dirs(job, "proofread")[0]
    payload = [json.loads(l) for l in (first / "payload.jsonl").read_text().splitlines()]
    assert payload[0]["id"] == 0 and "context" not in payload[0]
    assert any(row.get("context") for row in payload[1:])
    write_result(first, {"checked": 1, "cues": [{"id": 2, "translation": "hijacked"}]})
    for directory in tasks.task_dirs(job, "proofread")[1:]:
        write_result(directory, {"checked": 1, "cues": []})
    with pytest.raises(RuntimeError, match="context only"):
        pipeline.apply_proofread(job)


def test_proofread_warns_when_a_worker_underreports_coverage(transcribed):
    job = _translated(transcribed)
    pipeline.plan_proofread(job, source_language="English", target_language="Chinese", size=100)
    _results(job, "proofread", [{"checked": 1, "cues": []}])
    result = pipeline.apply_proofread(job)
    assert result["coverage_warnings"]


def test_proofread_applies_only_the_cues_it_returned(transcribed):
    job = _translated(transcribed)
    pipeline.plan_proofread(job, source_language="English", target_language="Chinese", size=100)
    _results(job, "proofread", [{"checked": 3, "cues": [{"id": 1, "translation": "二号"}]}])
    result = pipeline.apply_proofread(job)
    final = json.loads((job / "cues" / "final.json").read_text())
    assert result["fields_changed"] == 1
    assert [c["translation"] for c in final] == ["一", "二号", "三"]


def test_proofread_can_target_a_subset(transcribed):
    job = _translated(transcribed)
    plan = pipeline.plan_proofread(job, source_language="English", target_language="Chinese",
                                   size=100, only=[2])
    assert plan["cues"] == 1
    payload = [json.loads(l) for l in (tasks.task_dirs(job, "proofread")[0] / "payload.jsonl").read_text().splitlines()]
    assert [row["id"] for row in payload if not row.get("context")] == [2]


def test_a_targeted_proofread_keeps_the_full_pass_and_wins_for_its_own_cues(transcribed):
    job = _translated(transcribed)
    pipeline.plan_proofread(job, source_language="English", target_language="Chinese", size=100)
    _results(job, "proofread", [{"checked": 3, "cues": [{"id": 0, "translation": "一号"},
                                                         {"id": 2, "translation": "三号"}]}])
    pipeline.apply_proofread(job)
    plan = pipeline.plan_proofread(job, source_language="English", target_language="Chinese",
                                   size=100, only=[2])
    assert len(plan["dispatch"]) == 1 and len(tasks.task_dirs(job, "proofread")) == 2
    write_result(tasks.task_dirs(job, "proofread")[-1], {"checked": 1, "cues": []})
    assert tasks.dispatch_timing(job, "proofread")["tasks"] == 1
    result = pipeline.apply_proofread(job)
    final = json.loads((job / "cues" / "final.json").read_text())
    assert [c["translation"] for c in final] == ["一号", "二", "三"]
    assert result["fields_changed"] == 1


def test_applying_proofread_again_does_not_repeat_its_uncertainties(transcribed):
    job = _translated(transcribed)
    pipeline.plan_proofread(job, source_language="English", target_language="Chinese", size=100)
    _results(job, "proofread", [{"checked": 3, "cues": [],
                                 "uncertainties": [{"id": 1, "question": "which one"}]}])
    pipeline.apply_proofread(job)
    pipeline.apply_proofread(job)
    stored = json.loads((job / "cues" / "uncertainties.json").read_text())
    assert [(u["id"], u["stage"]) for u in stored] == [(1, "proofread")]


def test_a_targeted_proofread_replaces_the_uncertainties_of_its_cues(transcribed):
    job = _translated(transcribed)
    pipeline.plan_proofread(job, source_language="English", target_language="Chinese", size=100)
    _results(job, "proofread", [{"checked": 3, "cues": [], "uncertainties": [
        {"id": 1, "question": "old"}, {"id": 2, "question": "kept"}]}])
    pipeline.apply_proofread(job)
    pipeline.plan_proofread(job, source_language="English", target_language="Chinese",
                            size=100, only=[1])
    write_result(tasks.task_dirs(job, "proofread")[-1],
                 {"checked": 1, "cues": [], "uncertainties": [{"id": 1, "question": "new"}]})
    pipeline.apply_proofread(job)
    stored = json.loads((job / "cues" / "uncertainties.json").read_text())
    assert sorted((u["id"], u["question"]) for u in stored) == [(1, "new"), (2, "kept")]


def test_render_writes_one_dialogue_line_per_cue(transcribed):
    job = _translated(transcribed)
    output = pipeline.render(job)
    lines = [l for l in output.read_text(encoding="utf-8-sig").splitlines() if l.startswith("Dialogue:")]
    assert len(lines) == 3
    assert "一" in lines[0]


def test_summarize_falls_back_to_asr_windows_before_cues_exist(transcribed):
    plan = pipeline.plan_summarize(transcribed, language="English")
    assert plan["parts"] == 1
    payload = [json.loads(l) for l in (tasks.task_dirs(transcribed, "summarize")[0] / "payload.jsonl").read_text().splitlines()]
    assert payload[0]["text"].startswith("one two three")


def test_synthesize_writes_the_summary(transcribed):
    pipeline.plan_summarize(transcribed, language="English")
    _results(transcribed, "summarize", [{"notes": "They count to twelve (00:00)"}])
    pipeline.apply_summarize(transcribed)
    pipeline.plan_synthesize(transcribed, language="English")
    _results(transcribed, "synthesize", [{"summary": "# Counting\n\nThey count to twelve (00:00)"}])
    output = pipeline.apply_synthesize(transcribed)
    assert "count to twelve" in output.read_text()

def test_an_overlong_cue_is_split_evenly():
    repaired, notes = repair_boundaries(
        [{"start_word": 0, "end_word": 11, "text": "one two three"}], 12, max_words=5
    )
    assert any("split a 12 word cue" in note for note in notes)
    assert [(b["start_word"], b["end_word"]) for b in repaired] == [(0, 2), (3, 5), (6, 8), (9, 11)]
    # A piece cannot keep display text that covers words it no longer holds.
    assert all(b["text"] == "" for b in repaired)


def test_a_cue_at_the_cap_is_left_alone():
    repaired, notes = repair_boundaries(
        [{"start_word": 0, "end_word": 4, "text": "kept"}], 5, max_words=5
    )
    assert notes == [] and repaired[0]["text"] == "kept"


def test_no_cue_survives_apply_segment_over_the_prompt_cap(transcribed):
    """The verifier fails a cue over the cap, so segmentation must not emit one."""
    pipeline.plan_segment(transcribed, language="English", size=100)
    _results(transcribed, "segment", [["one two three four five six seven eight nine ten eleven twelve"]])
    pipeline.apply_segment(transcribed)
    cues = json.loads((transcribed / "cues" / "cues.json").read_text())
    assert all(c["end_word"] - c["start_word"] + 1 <= prompts.MAX_WORDS_PER_CUE for c in cues)


def _transcribed_as(job, texts, language):
    payload = {"provider": "fake", "language": language, "context": "", "duration": len(texts) * 0.5,
               "word_count": len(texts), "timings": {}, "windows": [], "words": timed_words(texts)}
    (job / "asr").mkdir()
    (job / "asr" / "words.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return job


def test_unspaced_lines_keep_their_breaks_against_one_character_words(job):
    """A recogniser can return Japanese a character at a time while the worker
    writes it normally, so the two only agree character by character."""
    _transcribed_as(job, list("今日はいい天気ですね。散歩に行きましょう。"), "Japanese")
    pipeline.plan_segment(job, language="Japanese", size=100)
    _results(job, "segment", [["今日はいい天気ですね", "散歩に行きましょう"]])
    result = pipeline.apply_segment(job)
    assert result["boundary_repairs"] == []
    cues = json.loads((job / "cues" / "cues.json").read_text(encoding="utf-8"))
    assert [c["source"] for c in cues] == ["今日はいい天気ですね", "散歩に行きましょう"]


def test_unspaced_lines_align_against_multi_character_words(job):
    _transcribed_as(job, ["今天", "天气", "很好。", "我们", "去", "散步", "吧。"], "Chinese")
    pipeline.plan_segment(job, language="Chinese", size=100)
    _results(job, "segment", [["今天天气很好", "我们去散步吧"]])
    result = pipeline.apply_segment(job)
    assert result["boundary_repairs"] == []
    cues = json.loads((job / "cues" / "cues.json").read_text(encoding="utf-8"))
    assert [(c["start_word"], c["end_word"]) for c in cues] == [(0, 2), (3, 6)]


def test_an_unspaced_cue_counts_two_characters_as_one_word():
    kept, notes = repair_boundaries([{"start_word": 0, "end_word": 33, "text": ""}], 34,
                                    max_words=17, weights=[lexical_weight("天")] * 34)
    assert notes == [] and len(kept) == 1
    split, _ = repair_boundaries([{"start_word": 0, "end_word": 35, "text": ""}], 36,
                                 max_words=17, weights=[lexical_weight("天")] * 36)
    assert [(b["start_word"], b["end_word"]) for b in split] == [(0, 17), (18, 35)]


def test_unspaced_batches_are_sized_in_words_not_characters():
    characters = [{"text": "天"} for _ in range(40)]
    assert pipeline.batch_words(characters, 10) == [(0, 19), (20, 39)]


def test_cue_times_are_the_asr_word_times_even_where_speech_overlaps():
    spoken = [{"text": "a", "start": 0.0, "end": 1.0}, {"text": "b", "start": 1.0, "end": 2.5},
              {"text": "c", "start": 2.0, "end": 3.0}, {"text": "d", "start": 3.0, "end": 4.0}]
    cues = build_cues(spoken, [{"start_word": 0, "end_word": 1}, {"start_word": 2, "end_word": 3}])
    assert [(c["start"], c["end"]) for c in cues] == [(0.0, 2.5), (2.0, 4.0)]


def _paused(job):
    spoken = [{"index": i, "text": t, "start": s, "end": s + 0.3} for i, (t, s) in enumerate(
        [("one", 0.0), ("two", 0.4), ("three", 0.8), ("four", 5.0), ("five", 5.4)])]
    (job / "asr").mkdir()
    (job / "asr" / "words.json").write_text(json.dumps({"words": spoken}), encoding="utf-8")
    return job


def test_the_segment_payload_breaks_the_line_where_the_speaker_paused(job):
    plan = pipeline.plan_segment(_paused(job), language="English", size=100)
    payload = (tasks.task_dirs(job, "segment")[0] / "payload.txt").read_text(encoding="utf-8")
    assert payload == "one two three\nfour five"
    assert plan["pause_breaks"] == 1 and plan["pause_threshold_seconds"] == 1.0


def test_the_pause_threshold_follows_the_video_within_its_bounds():
    def spaced(gap):
        return [{"start": i * (0.3 + gap), "end": i * (0.3 + gap) + 0.3} for i in range(50)]
    assert pipeline.pause_threshold(spaced(0.05)) == pipeline.PAUSE_FLOOR_SECONDS
    assert pipeline.pause_threshold(spaced(2.0)) == pipeline.PAUSE_CEILING_SECONDS
    assert pipeline.pause_threshold(spaced(0.6)) == pytest.approx(0.6)


def test_a_cue_that_stays_on_screen_through_a_silence_is_reported(job):
    pipeline.plan_segment(_paused(job), language="English", size=100)
    write_lines(tasks.task_dirs(job, "segment")[0], ["one two three four five"])
    result = pipeline.apply_segment(job)
    assert (result["hanging_cues"], result["hanging_cue_ids"]) == (1, [0])


def test_a_cue_spans_its_earliest_start_to_its_latest_end():
    """Where speech overlaps, the last word can end before the first one starts."""
    spoken = [{"text": "a", "start": 5.0, "end": 6.0}, {"text": "b", "start": 1.0, "end": 2.0}]
    cues = build_cues(spoken, [{"start_word": 0, "end_word": 1}])
    assert (cues[0]["start"], cues[0]["end"]) == (1.0, 6.0)


def test_a_worker_round_separates_thinking_from_orchestrator_delay(transcribed):
    """Nothing in Python runs while workers think, so the gap between the two
    commands is the round. The result files say when the work itself ended,
    which is what tells a slow worker from a slow orchestrator."""
    pipeline.plan_segment(transcribed, language="English", size=100)
    _results(transcribed, "segment", [["one two three four five six"]])
    stamp = tasks.stage_root(transcribed, "segment") / "planned_at"
    # Ten seconds before the result landed, thirty before now.
    stamp.write_text(str(float(stamp.read_text()) - 30), encoding="utf-8")
    timing = tasks.dispatch_timing(transcribed, "segment")
    assert 30 <= timing["seconds"] < 40
    assert 29 <= timing["last_done"] <= timing["seconds"]


def test_a_stage_that_was_never_planned_reports_no_dispatch_time(job):
    assert tasks.dispatch_timing(job, "translate") is None


def test_a_stage_with_no_results_yet_still_reports_the_gap(transcribed):
    pipeline.plan_segment(transcribed, language="English", size=100)
    timing = tasks.dispatch_timing(transcribed, "segment")
    assert "seconds" in timing and "last_done" not in timing


def test_parallel_segment_tasks_cannot_collide(transcribed):
    """Workers run at the same time, so the only thing keeping them apart is
    that their inputs and outputs are disjoint by construction."""
    plan = pipeline.plan_segment(transcribed, language="English", size=4)
    directories = tasks.task_dirs(transcribed, "segment")
    assert len(directories) == len(plan["dispatch"]) > 1

    ranges, seen = [], set()
    for directory in directories:
        meta = json.loads((directory / "meta.json").read_text())
        ranges.append((meta["first_word"], meta["last_word"]))
        # Each worker sees only the text of its own slice.
        words = json.loads((transcribed / "asr" / "words.json").read_text())["words"]
        own = " ".join(w["text"] for w in words[meta["first_word"] : meta["last_word"] + 1])
        assert (directory / "payload.txt").read_text(encoding="utf-8") == own
        seen.add(directory)
    ranges.sort()
    assert ranges[0][0] == 0
    for earlier, later in zip(ranges, ranges[1:]):
        assert later[0] == earlier[1] + 1, "task word ranges overlap or leave a gap"

    # One result file each, none shared, and the merge checks the whole run.
    _segment_lines(transcribed, per_line=99)
    result = pipeline.apply_segment(transcribed)
    cues = json.loads((transcribed / "cues" / "cues.json").read_text())
    assert result["cues"] == len(ranges)
    assert [c["start_word"] for c in cues] == [start for start, _ in ranges]


def test_a_worker_writing_another_slices_words_cannot_reach_them(transcribed):
    """The worst a bad answer can do is spoil its own slice. Words it writes
    that are not in its own payload are reported and changed nothing."""
    pipeline.plan_segment(transcribed, language="English", size=4)
    directories = tasks.task_dirs(transcribed, "segment")
    for index, directory in enumerate(directories):
        texts = (directory / "payload.txt").read_text(encoding="utf-8").split()
        if index == 1:
            texts = texts + ["eleven", "twelve"]  # words belonging to the last task
        write_lines(directory, [" ".join(texts)])
    result = pipeline.apply_segment(transcribed)
    cues = json.loads((transcribed / "cues" / "cues.json").read_text())
    metas = [json.loads((d / "meta.json").read_text()) for d in directories]
    assert [(c["start_word"], c["end_word"]) for c in cues] == [
        (m["first_word"], m["last_word"]) for m in metas
    ]
    assert any("not in the transcript" in n for n in result["boundary_repairs"])


def test_the_summary_fallback_does_not_collapse_on_a_whole_file_provider(transcribed):
    """A provider that takes the whole file reports one window. Splitting on
    windows made the entire transcript a single row with nothing to cite."""
    payload = json.loads((transcribed / "asr" / "words.json").read_text())
    payload["windows"] = [{"index": 0, "start": 0.0, "duration": 3600.0, "words": 12, "seconds": 1.0}]
    for index, word in enumerate(payload["words"]):
        word["start"], word["end"] = index * 40.0, index * 40.0 + 1.0
    (transcribed / "asr" / "words.json").write_text(json.dumps(payload), encoding="utf-8")
    segments = pipeline._transcript_segments(transcribed, payload)
    assert len(segments) == len(payload["words"])
    assert segments[0]["start"] == 0.0 and segments[1]["start"] == 40.0


def test_cues_are_preferred_over_the_fallback(transcribed):
    _segmented(transcribed)
    payload = json.loads((transcribed / "asr" / "words.json").read_text())
    segments = pipeline._transcript_segments(transcribed, payload)
    assert [s["text"] for s in segments] == ["one two three", "four five six seven",
                                             "eight nine ten eleven twelve"]


def test_work_is_spread_across_every_worker_the_harness_will_run():
    """Not a cap. More slots means more, smaller tasks, up to the point where a
    worker would lose the context it needs."""
    batch = pipeline.TRANSLATE_BATCH
    assert -(-1415 // pipeline.batch_size(1415, batch, 10)) == 10
    assert -(-1415 // pipeline.batch_size(1415, batch, 40)) == 40


def test_slots_are_left_idle_rather_than_starving_a_worker_of_context():
    """A short video across many slots would put a handful of cues in each,
    and a translator with no surrounding speech loses the terminology."""
    batch = pipeline.TRANSLATE_BATCH
    assert pipeline.batch_size(94, batch, 40) == batch.floor
    assert -(-94 // batch.floor) == 5  # five workers used out of forty


def test_a_batch_stops_at_the_ceiling_and_spills_into_a_second_wave():
    batch = pipeline.SEGMENT_BATCH
    assert pipeline.batch_size(100_000, batch, 10) == batch.ceiling
    assert pipeline._wave_plan(-(-100_000 // batch.ceiling), 10)["waves"] > 1


def test_the_preferred_size_is_used_when_the_harness_capacity_is_unknown():
    for batch in (pipeline.SEGMENT_BATCH, pipeline.TRANSLATE_BATCH, pipeline.PROOFREAD_BATCH):
        assert pipeline.batch_size(5000, batch, None) == batch.preferred
        assert batch.floor <= batch.preferred <= batch.ceiling


def test_waves_are_reported_only_when_one_round_will_not_do():
    assert pipeline._wave_plan(8, 10) == {"tasks_planned": 8, "waves": 1}
    assert pipeline._wave_plan(8, None) == {"tasks_planned": 8, "waves": 1}
    many = pipeline._wave_plan(12, 10)
    assert many["waves"] == 2 and many["dispatch_at_once"] == 10
    assert "groups of 10" in many["note"]


def test_concurrency_comes_from_the_environment_when_not_passed(transcribed, monkeypatch):
    monkeypatch.setenv(pipeline.CONCURRENCY_ENV, "4")
    plan = pipeline.plan_segment(transcribed, language="English")
    assert plan["waves"] == 1 and plan["words_per_task"] == pipeline.SEGMENT_BATCH.floor


def test_a_malformed_concurrency_falls_back_rather_than_failing_a_job(transcribed, monkeypatch):
    monkeypatch.setenv(pipeline.CONCURRENCY_ENV, "lots")
    plan = pipeline.plan_segment(transcribed, language="English")
    assert plan["words_per_task"] == pipeline.SEGMENT_BATCH.preferred
