import json
from pathlib import Path

import pytest

from conftest import write_result
from vstudio import post, tasks


@pytest.fixture(autouse=True)
def default_settings(monkeypatch):
    for name in (post.POSTPROCESS_ENV, post.SNAP_GAP_ENV, post.LEAD_ENV, post.LAG_ENV):
        monkeypatch.delenv(name, raising=False)


def _cue(cue_id, start, end, source, translation=None):
    return {"id": cue_id, "start": start, "end": end, "source": source, "source_exact": source,
            "translation": translation or f"译{cue_id}", "start_word": cue_id, "end_word": cue_id}


def _final(job, cues):
    (job / "cues").mkdir(exist_ok=True)
    (job / "cues" / "final.json").write_text(json.dumps(cues, ensure_ascii=False), encoding="utf-8")


def _with_duplicate(job):
    """Line 4 is line 3 printed again with the same timestamps."""
    cues = [_cue(i, i * 1.5, i * 1.5 + 1, f"line {i}") for i in range(4)]
    cues.append(_cue(4, 4.5, 5.5, "line 3", "译3"))
    cues += [_cue(i, (i - 1) * 1.5, (i - 1) * 1.5 + 1, f"line {i}") for i in range(5, 8)]
    _final(job, cues)
    return job


def _answer(job, lines):
    directory = tasks.task_dirs(job, "postcheck")[0]
    write_result(directory, {"groups": [{"group": 0, "lines": lines}]})


MERGED = [{"from": [3, 4], "start": 4.5, "end": 5.5, "source": "line 3", "translation": "译3"}]


def test_clusters_catch_a_line_overlapping_one_that_is_not_its_neighbour():
    cues = [_cue(0, 0, 5, "a"), _cue(1, 1, 2, "b"), _cue(2, 3, 4, "c"), _cue(3, 6, 7, "d")]
    assert post.overlap_clusters(cues) == [[0, 1, 2]]
    assert post.overlap_clusters([_cue(0, 0, 1, "a"), _cue(1, 1, 2, "b")]) == []


def test_a_set_without_overlaps_needs_no_worker(job):
    _final(job, [_cue(0, 0, 1, "a"), _cue(1, 1.5, 2, "b")])
    plan = post.plan_postcheck(job, source_language="English", target_language="Chinese")
    assert plan["dispatch"] == [] and plan["overlapping_cues"] == 0
    assert json.loads((job / "cues" / "checked.json").read_text()) == json.loads(
        (job / "cues" / "final.json").read_text())


def test_overlapping_lines_go_out_with_read_only_context(job):
    _with_duplicate(job)
    plan = post.plan_postcheck(job, source_language="English", target_language="Chinese")
    assert plan["agent"] == "vstudio-worker-deep" and plan["round"] == 1
    assert plan["overlapping_cues"] == 2 and len(plan["dispatch"]) == 1
    directory = tasks.task_dirs(job, "postcheck")[0]
    rows = [json.loads(l) for l in (directory / "payload.jsonl").read_text().splitlines()]
    assert [r["id"] for r in rows if r.get("context")] == [0, 1, 2, 5, 6, 7]
    assert [r["id"] for r in rows if not r.get("context")] == [3, 4]
    meta = json.loads((directory / "meta.json").read_text())["groups"][0]
    assert meta["lower"] == 4.0 and meta["upper"] == 6.0


def test_a_duplicate_folded_into_its_original_is_deleted(job):
    _with_duplicate(job)
    post.plan_postcheck(job, source_language="English", target_language="Chinese")
    _answer(job, MERGED)
    result = post.apply_postcheck(job)
    assert result["rejected"] == [] and result["overlapping_cues"] == 0
    assert (result["lines_before"], result["lines_after"]) == (8, 7)
    checked = json.loads((job / "cues" / "checked.json").read_text())
    assert [c["id"] for c in checked] == [0, 1, 2, 3, 5, 6, 7]
    assert checked[3]["postcheck"] == {"from": [3, 4], "round": 1, "spoken": [4.5, 5.5]}
    assert "postprocess" in result["next"]


@pytest.mark.parametrize("lines, reason", [
    ([{**MERGED[0], "source": "line 3 extra"}], "adds words"),
    ([{**MERGED[0], "from": [3]}], "exactly one from"),
    ([{**MERGED[0], "start": 3.9}], "before the previous line ends"),
    ([{**MERGED[0], "end": 6.1}], "after the next line starts"),
    ([{**MERGED[0], "from": [3]}, {**MERGED[0], "from": [4]}], "still overlap"),
    ([{**MERGED[0], "from": [3, 9]}], "outside the group"),
    ([{**MERGED[0], "from": "34"}], "list of cue ids"),
    ([{**MERGED[0], "from": [3, "x"]}], "list of cue ids"),
    ([{**MERGED[0], "start": 5.6, "end": 5.9}], "no longer covers its speech"),
    ([{**MERGED[0], "start": 4.5001, "end": 4.5004}], "start before end"),
])
def test_an_answer_that_breaks_a_rule_is_refused_and_the_lines_kept(job, lines, reason):
    _with_duplicate(job)
    post.plan_postcheck(job, source_language="English", target_language="Chinese")
    _answer(job, lines)
    result = post.apply_postcheck(job)
    assert reason in result["rejected"][0]["reason"]
    assert result["overlapping_cues"] == 2 and "plan-postcheck" in result["next"]
    assert len(json.loads((job / "cues" / "checked.json").read_text())) == 8


def test_three_failed_rounds_stop_and_name_the_lines(job):
    _with_duplicate(job)
    for _ in range(post.POSTCHECK_MAX_ROUNDS):
        post.plan_postcheck(job, source_language="English", target_language="Chinese")
        _answer(job, [])
        post.apply_postcheck(job)
    with pytest.raises(RuntimeError, match=r"3 rounds .* \[3, 4\]"):
        post.plan_postcheck(job, source_language="English", target_language="Chinese")
    result, _ = post.postprocess(job, settings=post.postprocess_settings(), subtitle_format="srt")
    assert result["overlapping_cues_left"] == 2


def test_a_round_cannot_be_applied_twice_or_after_final_changes(job):
    _with_duplicate(job)
    post.plan_postcheck(job, source_language="English", target_language="Chinese")
    _answer(job, [])
    post.apply_postcheck(job)
    with pytest.raises(RuntimeError, match="already applied"):
        post.apply_postcheck(job)
    post.plan_postcheck(job, source_language="English", target_language="Chinese")
    _with_duplicate(job)
    final = job / "cues" / "final.json"
    final.write_text(final.read_text().replace("译7", "改7"), encoding="utf-8")
    with pytest.raises(RuntimeError, match="changed after"):
        post.apply_postcheck(job)


def test_a_kept_line_cannot_have_its_translation_rewritten(job):
    _with_duplicate(job)
    post.plan_postcheck(job, source_language="English", target_language="Chinese")
    directory = tasks.task_dirs(job, "postcheck")[0]
    write_result(directory, {"groups": [{"group": 0, "lines": [
        {"from": [3], "start": 4.5, "end": 5.0, "source": "line 3", "translation": "别的"},
        {"from": [4], "start": 5.0, "end": 5.5, "source": "line 3", "translation": "译3"}]}]})
    assert "rewrites its translation" in post.apply_postcheck(job)["rejected"][0]["reason"]


def test_a_folded_duplicate_keeps_a_translation_one_of_its_cues_had():
    by_id = {3: _cue(3, 4.5, 5.5, "line 3"), 4: _cue(4, 4.5, 5.5, "line 3", "译3")}
    group = {"group": 0, "ids": [3, 4], "lower": 0.0, "upper": 30.0}
    rewritten = {"lines": [{**MERGED[0], "translation": "全新的"}]}
    assert "rewrites its translation" in post.resolve_group(rewritten, group, by_id, round_number=1)[1]


def test_lines_written_out_of_time_order_are_accepted():
    by_id = {1: _cue(1, 1.0, 2.0, "a"), 2: _cue(2, 1.5, 4.0, "b")}
    group = {"group": 0, "ids": [1, 2], "lower": 0.0, "upper": 30.0}
    answer = {"lines": [{"from": [2], "start": 2.0, "end": 4.0, "source": "b", "translation": "译2"},
                        {"from": [1], "start": 1.0, "end": 2.0, "source": "a", "translation": "译1"}]}
    lines, reason = post.resolve_group(answer, group, by_id, round_number=1)
    assert reason is None and [l["id"] for l in lines] == [1, 2]


def test_a_merge_keeps_the_earliest_id_whatever_order_the_worker_wrote(job):
    _with_duplicate(job)
    post.plan_postcheck(job, source_language="English", target_language="Chinese")
    _answer(job, [{**MERGED[0], "from": [4, 3]}])
    post.apply_postcheck(job)
    checked = json.loads((job / "cues" / "checked.json").read_text())
    assert [c["id"] for c in checked] == [0, 1, 2, 3, 5, 6, 7]


def test_the_last_group_is_not_bounded_by_a_duration_the_worker_never_sees(job):
    cues = [_cue(0, 0, 1, "a"), _cue(1, 59.5, 60.2, "b"), _cue(2, 59.5, 60.2, "b")]
    _final(job, cues)
    post.plan_postcheck(job, source_language="English", target_language="Chinese")
    _answer(job, [{"from": [1, 2], "start": 59.5, "end": 60.2, "source": "b", "translation": "译1"}])
    assert post.apply_postcheck(job)["rejected"] == []


def test_an_empty_source_exact_does_not_crash_the_merge(job):
    _with_duplicate(job)
    final = job / "cues" / "final.json"
    cues = json.loads(final.read_text())
    cues[4]["source_exact"] = ""
    final.write_text(json.dumps(cues), encoding="utf-8")
    post.plan_postcheck(job, source_language="English", target_language="Chinese")
    _answer(job, MERGED)
    assert post.apply_postcheck(job)["rejected"] == []


def test_postprocess_refuses_to_run_before_post_check(job):
    _final(job, [_cue(0, 0, 1, "a")])
    with pytest.raises(RuntimeError, match="plan-postcheck"):
        post.postprocess(job, settings=post.postprocess_settings(), subtitle_format="srt")


def test_close_neighbours_meet_at_the_midpoint_and_each_group_is_padded():
    cues = [_cue(0, 1.0, 2.0, "a"), _cue(1, 2.2, 3.0, "b"), _cue(2, 3.0, 4.0, "c"),
            _cue(3, 5.0, 6.0, "d")]
    out, report = post.snap_and_pad(cues, snap_gap_ms=300, lead_ms=100, lag_ms=200)
    assert [(c["start"], c["end"]) for c in out] == [(0.9, 2.1), (2.1, 3.0), (3.0, 4.2), (4.9, 6.2)]
    assert report["groups"] == 2 and report["gaps_snapped"] == 1
    assert report["lines_in_joined_groups"] == 3
    assert (out[0]["checked_start"], out[0]["checked_end"]) == (1.0, 2.0)


def test_a_gap_of_exactly_the_snap_threshold_takes_both_paddings_without_overlap():
    cues = [_cue(0, 1.0, 2.0, "a"), _cue(1, 2.3, 3.0, "b")]
    out, report = post.snap_and_pad(cues, snap_gap_ms=300, lead_ms=100, lag_ms=200)
    assert (out[0]["end"], out[1]["start"]) == (2.2, 2.2) and report["padding_shortened"] == 0


def test_padding_that_would_collide_shrinks_and_overlaps_are_left_alone():
    cues = [_cue(0, 1.0, 2.0, "a"), _cue(1, 2.2, 3.0, "b")]
    out, report = post.snap_and_pad(cues, snap_gap_ms=100, lead_ms=300, lag_ms=300)
    assert out[0]["end"] == out[1]["start"] == 2.1 and report["padding_shortened"] == 1
    overlapping = [_cue(0, 1.0, 2.5, "a"), _cue(1, 2.0, 3.0, "b")]
    out, report = post.snap_and_pad(overlapping, snap_gap_ms=300, lead_ms=100, lag_ms=200)
    assert (out[0]["end"], out[1]["start"]) == (2.5, 2.0) and report["overlapping_cues_left"] == 2


def test_padding_never_pushes_a_line_into_a_long_one_that_contains_its_neighbour():
    cues = [_cue(0, 0, 10, "a"), _cue(1, 2, 3, "b"), _cue(2, 10.05, 12, "c")]
    out, report = post.snap_and_pad(cues, snap_gap_ms=300, lead_ms=100, lag_ms=200)
    assert out[2]["start"] >= out[0]["end"]
    assert report["overlapping_cues_left"] == 2
    cues = [_cue(0, 0, 10.1, "a"), _cue(1, 2, 9.9, "b"), _cue(2, 10.15, 12, "c")]
    out, _ = post.snap_and_pad(cues, snap_gap_ms=300, lead_ms=100, lag_ms=200)
    assert out[2]["start"] >= out[0]["end"]
    assert out[1]["end"] == 9.9, "a line nested in a left-over overlap is not stretched"


def test_a_rebuilt_final_drops_the_delivered_cues(job):
    _final(job, [_cue(0, 0, 1, "a")])
    post.plan_postcheck(job, source_language="English", target_language="Chinese")
    post.postprocess(job, settings=post.postprocess_settings(), subtitle_format="srt")
    assert (job / "cues" / "delivered.json").is_file()
    _final(job, [_cue(0, 0, 1, "b")])
    with pytest.raises(RuntimeError, match="plan-postcheck"):
        post.postprocess(job, settings=post.postprocess_settings(), subtitle_format="srt")
    assert not (job / "cues" / "delivered.json").exists()


def test_a_gap_after_a_nested_line_closes_against_the_line_that_ends_last():
    cues = [_cue(0, 0, 10, "a"), _cue(1, 2, 5, "b"), _cue(2, 10.1, 12, "c")]
    out, _ = post.snap_and_pad(cues, snap_gap_ms=300, lead_ms=100, lag_ms=200)
    assert out[0]["end"] == out[2]["start"] == 10.05 and out[1]["end"] == 5


def test_frequent_duplicates_do_not_chain_into_one_group():
    cues = []
    for block in range(40):
        base = block * 6 * 1.5
        cues += [_cue(len(cues) + k, base + k * 1.5, base + k * 1.5 + 1, f"l{block}{k}") for k in range(5)]
        cues.append(_cue(len(cues), cues[-1]["start"], cues[-1]["end"], cues[-1]["source"]))
    ranges = post._groups(post.overlap_clusters(cues))
    assert len(ranges) == 40 and all(last - first == 1 for first, last in ranges)


def test_postprocess_waits_for_an_unapplied_or_unfinished_round(job):
    _with_duplicate(job)
    post.plan_postcheck(job, source_language="English", target_language="Chinese")
    (job / "cues" / "checked.json").write_text((job / "cues" / "final.json").read_text())
    with pytest.raises(RuntimeError, match="not applied"):
        post.postprocess(job, settings=post.postprocess_settings(), subtitle_format="srt")
    _answer(job, [])
    post.apply_postcheck(job)
    with pytest.raises(RuntimeError, match="next round"):
        post.postprocess(job, settings=post.postprocess_settings(), subtitle_format="srt")


def test_a_bare_group_answer_is_read_as_a_list_of_one(job):
    _with_duplicate(job)
    post.plan_postcheck(job, source_language="English", target_language="Chinese")
    write_result(tasks.task_dirs(job, "postcheck")[0], {"group": 0, "lines": MERGED})
    assert post.apply_postcheck(job)["overlapping_cues"] == 0


def test_a_malformed_answer_names_the_task(job):
    _with_duplicate(job)
    post.plan_postcheck(job, source_language="English", target_language="Chinese")
    write_result(tasks.task_dirs(job, "postcheck")[0], {"answer": "none"})
    with pytest.raises(RuntimeError, match="000.*Re-dispatch"):
        post.apply_postcheck(job)


def test_the_last_group_refuses_an_end_far_past_the_video(job):
    _final(job, [_cue(0, 0, 1, "a"), _cue(1, 59.5, 60.2, "b"), _cue(2, 59.5, 60.2, "b")])
    post.plan_postcheck(job, source_language="English", target_language="Chinese")
    _answer(job, [{"from": [1, 2], "start": 59.5, "end": 9999, "source": "b", "translation": "译1"}])
    assert "after the next line starts" in post.apply_postcheck(job)["rejected"][0]["reason"]


def test_postprocess_re_renders_the_other_format_with_the_delivered_timing(job):
    _final(job, [_cue(0, 1.0, 2.0, "one")])
    stale = job / "out" / f"{job.name}.srt"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale", encoding="utf-8")
    post.plan_postcheck(job, source_language="English", target_language="Chinese")
    result, delivered = post.postprocess(job, settings=post.postprocess_settings(), subtitle_format="ass")
    assert post.refresh_other_formats(job, Path(result["subtitles"]), "ass", delivered) == [str(stale)]
    assert "00:00:00,900 --> 00:00:02,200" in stale.read_text(encoding="utf-8")


def test_a_merged_line_must_cover_the_speech_of_every_cue_it_takes_in():
    by_id = {i: _cue(i, i, i + 0.8, f"w{i}") for i in (12, 13, 14)}
    group = {"group": 0, "ids": [12, 13, 14], "lower": 0.0, "upper": 30.0}
    answer = {"lines": [
        {"from": [12, 14], "start": 12, "end": 12.8, "source": "w12", "translation": "译12"},
        {"from": [13], "start": 13, "end": 13.8, "source": "w13", "translation": "译13"}]}
    lines, reason = post.resolve_group(answer, group, by_id, round_number=1)
    assert lines is None and "takes in cue 14" in reason


def test_a_duplicate_folds_into_its_original_across_a_line_sorted_between_them():
    by_id = {40: _cue(40, 10.0, 12.0, "hello world"), 41: _cue(41, 10.02, 11.0, "hi"),
             42: _cue(42, 10.03, 12.0, "hello world")}
    group = {"group": 0, "ids": [40, 41, 42], "lower": 0.0, "upper": 30.0}
    answer = {"lines": [
        {"from": [40, 42], "start": 10.0, "end": 10.5, "source": "hello world", "translation": "译40"},
        {"from": [41], "start": 10.5, "end": 11.0, "source": "hi", "translation": "译41"}]}
    lines, reason = post.resolve_group(answer, group, by_id, round_number=1)
    assert reason is None and [l["id"] for l in lines] == [40, 41]


def test_ids_written_as_strings_or_whole_floats_are_read_and_repeats_are_named():
    by_id = {3: _cue(3, 4.5, 5.5, "line 3"), 4: _cue(4, 4.5, 5.5, "line 3", "译3")}
    group = {"group": 0, "ids": [3, 4], "lower": 0.0, "upper": 30.0}
    loose = {"lines": [{**MERGED[0], "from": ["3", 4.0]}]}
    assert post.resolve_group(loose, group, by_id, round_number=1)[1] is None
    doubled = {"lines": [{**MERGED[0], "from": [3, 3]}]}
    assert "repeats an id" in post.resolve_group(doubled, group, by_id, round_number=1)[1]


def test_ids_reordered_by_an_earlier_round_can_still_merge():
    by_id = {13: _cue(13, 5, 6, "x"), 12: _cue(12, 6, 8, "y"), 14: _cue(14, 7, 9, "z")}
    group = {"group": 0, "ids": [13, 12, 14], "lower": 0.0, "upper": 30.0}
    answer = {"lines": [
        {"from": [12, 13], "start": 5, "end": 7, "source": "x y", "translation": "译"},
        {"from": [14], "start": 7, "end": 9, "source": "z", "translation": "译14"}]}
    lines, reason = post.resolve_group(answer, group, by_id, round_number=2)
    assert reason is None and lines[0]["id"] == 13


def test_a_bare_list_of_lines_answers_the_only_group(job):
    _with_duplicate(job)
    post.plan_postcheck(job, source_language="English", target_language="Chinese")
    write_result(tasks.task_dirs(job, "postcheck")[0], MERGED)
    assert post.apply_postcheck(job)["overlapping_cues"] == 0


def test_apply_after_an_empty_plan_points_to_postprocess(job):
    _final(job, [_cue(0, 0, 1, "a")])
    post.plan_postcheck(job, source_language="English", target_language="Chinese")
    result = post.apply_postcheck(job)
    assert result["nothing_to_apply"] and "postprocess" in result["next"]


def test_a_line_cannot_drift_from_its_speech_over_rounds():
    by_id = {20: {**_cue(20, 11.9, 13.0, "x"), "postcheck": {"from": [20], "spoken": [10.0, 12.0]}}}
    group = {"group": 0, "ids": [20], "lower": 0.0, "upper": 30.0}
    answer = {"lines": [{"from": [20], "start": 12.95, "end": 14.0, "source": "x", "translation": "译20"}]}
    lines, reason = post.resolve_group(answer, group, by_id, round_number=2)
    assert lines is None and "no longer covers its speech at 10.0-12.0" in reason


def test_a_line_the_recogniser_emitted_late_is_sorted_rather_than_treated_as_an_overlap(job):
    _final(job, [_cue(1, 5, 6, "a"), _cue(2, 6, 7, "b"), _cue(3, 1, 2, "c")])
    plan = post.plan_postcheck(job, source_language="English", target_language="Chinese")
    assert plan["overlapping_cues"] == 0
    checked = json.loads((job / "cues" / "checked.json").read_text())
    assert [c["id"] for c in checked] == [3, 1, 2]


def test_a_repeat_emitted_out_of_order_lands_in_the_group_of_the_line_it_repeats(job):
    _final(job, [_cue(12, 1, 2, "X"), _cue(13, 2, 3, "Y"), _cue(14, 1, 2, "X")])
    post.plan_postcheck(job, source_language="English", target_language="Chinese")
    meta = json.loads((tasks.task_dirs(job, "postcheck")[0] / "meta.json").read_text())
    assert meta["groups"][0]["ids"] == [12, 14]


def test_padding_in_whole_milliseconds_never_overlaps_by_rounding():
    cues = [_cue(0, 0.5, 1.0007, "a"), _cue(1, 1.3002, 2.0, "b")]
    out, report = post.snap_and_pad(cues, snap_gap_ms=300, lead_ms=100, lag_ms=200)
    assert out[0]["end"] <= out[1]["start"] and report["overlapping_cues_left"] == 0


def test_padding_stays_inside_the_video():
    cues = [_cue(0, 0.05, 1.0, "a"), _cue(1, 2.0, 59.9, "b")]
    out, _ = post.snap_and_pad(cues, snap_gap_ms=300, lead_ms=100, lag_ms=200, duration=60.0)
    assert out[0]["start"] == 0.0 and out[-1]["end"] == 60.0
    out, _ = post.snap_and_pad([_cue(0, 600.0, 600.4, "a")], snap_gap_ms=300, lead_ms=100, lag_ms=200,
                               duration=600.002)
    assert out[0]["end"] == 600.4, "the clamp never cuts a line short of its checked end"


def test_settings_come_from_the_flag_then_the_config_then_the_default(monkeypatch):
    assert post.postprocess_settings() == {"enabled": True, "snap_gap_ms": 300,
                                           "lead_ms": 100, "lag_ms": 200}
    monkeypatch.setenv(post.SNAP_GAP_ENV, "500")
    monkeypatch.setenv(post.POSTPROCESS_ENV, "off")
    settings = post.postprocess_settings(lag_ms=0)
    assert settings == {"enabled": False, "snap_gap_ms": 500, "lead_ms": 100, "lag_ms": 0}
    monkeypatch.setenv(post.LEAD_ENV, "-5")
    with pytest.raises(RuntimeError, match="negative"):
        post.postprocess_settings()


def test_postprocess_writes_the_delivered_timing_into_the_subtitle_file(job):
    _final(job, [_cue(0, 1.0, 2.0, "one"), _cue(1, 2.2, 3.0, "two")])
    post.plan_postcheck(job, source_language="English", target_language="Chinese")
    result, _ = post.postprocess(job, settings=post.postprocess_settings(), subtitle_format="srt")
    srt = (job / "out" / f"{job.name}.srt").read_text(encoding="utf-8")
    assert "00:00:00,900 --> 00:00:02,100" in srt and "00:00:02,100 --> 00:00:03,200" in srt
    assert result["groups"] == 1
    off, delivered = post.postprocess(job, settings=post.postprocess_settings(enabled=False),
                                      subtitle_format="srt")
    assert "skipped" in off and delivered[0]["checked_start"] == 1.0
    assert "00:00:01,000 --> 00:00:02,000" in (job / "out" / f"{job.name}.srt").read_text(encoding="utf-8")
