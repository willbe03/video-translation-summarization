import json

import pytest

from vstudio import jobs
from vstudio.verify import source_substitutions, verify_translation


def test_a_failing_step_is_still_timed(job):
    with pytest.raises(ValueError):
        with jobs.timed(job, "asr", provider="fake"):
            raise ValueError("boom")
    entries = json.loads(jobs.timing_path(job).read_text())
    assert entries[0]["step"] == "asr" and entries[0]["failed"] is True


def test_the_timing_report_shows_each_step_as_a_share(job):
    jobs.record_timing(job, "acquire", 10.0)
    jobs.record_timing(job, "asr", 30.0, provider="mai")
    report = jobs.timing_report(job)
    assert "75.0%" in report and "provider=mai" in report and "total" in report


def test_artifacts_root_follows_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv(jobs.ARTIFACTS_ENV, str(tmp_path / "elsewhere"))
    assert jobs.artifacts_root() == tmp_path / "elsewhere"
    monkeypatch.delenv(jobs.ARTIFACTS_ENV)
    assert jobs.artifacts_root().name == "runs"


def test_a_job_directory_is_named_after_the_video(tmp_path):
    created = jobs.new_job_dir("Why StarCraft II Is Still Good", root=tmp_path)
    assert created.name.endswith("-why-starcraft-ii-is-still-good")


def test_stripped_punctuation_is_not_a_substitution():
    cues = [{"id": 0, "source_exact": "hello, world.", "source": "hello world"}]
    assert source_substitutions(cues) == []


def test_an_added_word_is_reported_as_added():
    cues = [{"id": 0, "source_exact": "GG", "source": "good game"}]
    found = source_substitutions(cues)
    assert found[0]["added_words"] == 1 and found[0]["now"] == "good game"


def test_a_moved_word_is_a_substitution_but_not_an_addition():
    """The diff splits a move into a deletion and an insertion, and the cue did not grow."""
    cues = [{"id": 0, "source_exact": "alpha beta gamma delta epsilon",
             "source": "beta gamma delta alpha epsilon"}]
    found = source_substitutions(cues)
    assert found and all(s["added_words"] == 0 for s in found)


def test_a_japanese_comma_swapped_for_a_space_adds_no_word():
    cues = [{"id": 0, "source_exact": "そうですね、行きましょう", "source": "そうですね 行きましょう"}]
    assert source_substitutions(cues) == []


def test_a_kanji_fix_to_an_unspaced_name_is_a_substitution_not_an_addition():
    cues = [{"id": 0, "source_exact": "たなかさんです", "source": "田中さんです"}]
    assert source_substitutions(cues) == [{"id": 0, "was": "たなか", "now": "田中", "added_words": 0}]


def test_respelling_hiragana_as_katakana_with_long_vowel_marks_adds_no_word():
    cues = [{"id": 0, "source_exact": "こんぴゅたをかう", "source": "コンピューターをかう"}]
    assert [s["added_words"] for s in source_substitutions(cues)] == [0]


def test_a_repeated_word_respelled_in_katakana_is_not_an_added_word():
    cues = [{"id": 0, "source_exact": "ねこネコ", "source": "ネコネコ"}]
    assert source_substitutions(cues) == [{"id": 0, "was": "ねこ", "now": "ネコ", "added_words": 0}]


def _cue(cue_id, start, end, source, translation, **extra):
    return {"id": cue_id, "start": start, "end": end, "source": source,
            "source_exact": source, "translation": translation, **extra}


def test_verify_passes_a_clean_set():
    final = [_cue(0, 0.0, 2.0, "hello world", "你好世界"), _cue(1, 2.0, 4.0, "goodbye", "再见")]
    report = verify_translation({}, final, final, None, ass_dialogue_count=2, ass_codec="ass")
    assert report["ok"] and not report["failures"]


def test_verify_catches_a_moved_timestamp():
    draft = [_cue(0, 0.0, 2.0, "hello", "你好")]
    final = [_cue(0, 0.0, 9.0, "hello", "你好")]
    report = verify_translation({}, final, draft, None)
    assert not report["ok"]
    assert any("timestamp" in f for f in report["failures"])


def test_verify_catches_a_comma_that_slipped_through():
    final = [_cue(0, 0.0, 2.0, "hello world", "你好，世界")]
    report = verify_translation({}, final, final, None)
    assert any("comma or period" in f for f in report["failures"])


def test_verify_catches_an_ass_file_that_lost_a_line():
    final = [_cue(0, 0.0, 2.0, "a", "甲"), _cue(1, 2.0, 4.0, "b", "乙")]
    report = verify_translation({}, final, final, None, ass_dialogue_count=1, ass_codec="ass")
    assert any("dialogue lines" in f for f in report["failures"])


def test_a_long_interpolated_run_is_warned_about():
    final = [_cue(0, 0.0, 2.0, "a", "甲")]
    words = [{"repaired": True} for _ in range(12)]
    report = verify_translation({}, final, final, words)
    assert any("interpolated timestamp run is 12" in w for w in report["warnings"])
    assert report["ok"]


def test_an_unspaced_cue_is_measured_in_words_against_the_cap():
    final = [_cue(0, 0.0, 5.0, "天" * 30, "天" * 30, start_word=0, end_word=29)]
    report = verify_translation({}, final, final, None)
    assert not any("aligned words" in f for f in report["failures"])


def _timed_words(*spans):
    return [{"text": "w", "start": start, "end": end} for start, end in spans]


def test_a_cue_left_on_screen_through_a_silence_is_warned_about():
    words = _timed_words((0.0, 1.0), (4.0, 5.0))
    final = [_cue(0, 0.0, 5.0, "a b", "甲乙", start_word=0, end_word=1)]
    report = verify_translation({}, final, final, words)
    assert report["ok"] and report["details"]["hanging_cues"] == 1
    assert any("through a silence" in w for w in report["warnings"])


def test_cues_may_overlap_where_the_transcript_does():
    words = _timed_words((0.0, 1.0), (1.0, 2.5), (2.0, 3.0), (3.0, 4.0))
    final = [_cue(0, 0.0, 2.5, "a b", "甲乙", start_word=0, end_word=1),
             _cue(1, 2.0, 4.0, "c d", "丙丁", start_word=2, end_word=3)]
    report = verify_translation({}, final, final, words)
    assert report["ok"] and report["details"]["overlapping_cues"] == 1


def test_a_cue_not_timed_like_its_asr_words_fails():
    words = _timed_words((0.0, 1.0), (1.0, 2.5), (2.0, 3.0), (3.0, 4.0))
    final = [_cue(0, 0.0, 2.0, "a b", "甲乙", start_word=0, end_word=1),
             _cue(1, 2.0, 4.0, "c d", "丙丁", start_word=2, end_word=3)]
    report = verify_translation({}, final, final, words)
    assert any("differ from the ASR word times: [0]" in f for f in report["failures"])


def test_an_ass_line_not_timed_like_its_cue_fails():
    final = [_cue(0, 0.0, 2.0, "a", "甲")]
    report = verify_translation({}, final, final, None, ass_dialogue_count=1, ass_codec="ass",
                                ass_times=[("0:00:00.00", "0:00:02.50")])
    assert any("not timed like their cues" in f for f in report["failures"])


def test_renaming_a_job_rewrites_the_paths_inside_it(tmp_path):
    created = jobs.new_job_dir("video", root=tmp_path)
    jobs.write_manifest(created, {"job": str(created), "title": "The Real Title",
                                  "media": str(created / "media" / "video.mkv")})
    renamed = jobs.rename_job(created, "The Real Title")
    assert renamed.name.endswith("-the-real-title")
    manifest = jobs.read_manifest(renamed)
    assert manifest["media"].startswith(str(renamed))
    assert manifest["job"] == str(renamed)


def test_renaming_leaves_a_local_source_alone(tmp_path):
    created = jobs.new_job_dir("video", root=tmp_path)
    outside = str(tmp_path / "elsewhere" / "recording.mkv")
    jobs.write_manifest(created, {"job": str(created), "media": outside})
    renamed = jobs.rename_job(created, "Recording")
    assert jobs.read_manifest(renamed)["media"] == outside
