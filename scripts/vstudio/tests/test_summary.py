import pytest

from vstudio.summary import check_summary
from vstudio.timestamps import (
    forbidden_citations,
    iter_timestamps,
    linkify_summary,
    parse_timestamp,
)

FORBIDDEN = ("（00:15）", "(12:34）", "(12：34)", "(０１:38)", "(1:02:03)", "【05:00～06:00】",
             "(05:00-06:00)", "(00:30, 01:10)", "(约01:05)", "[03:00]", "(01:38 )", "(01:60)",
             "「01:38」", "〔01:38〕", "{01:38}", "『01:38』")


def test_only_the_single_ascii_form_is_a_citation():
    text = "开场 (00:15) 然后 (104:10) 以及 " + " ".join(FORBIDDEN)
    assert [seconds for _, seconds in iter_timestamps(text)] == [15.0, 6250.0]


@pytest.mark.parametrize("token", FORBIDDEN)
def test_every_other_form_is_reported_as_forbidden(token):
    assert forbidden_citations(f"见 {token} 处") == [token]


def test_allowed_citations_links_and_prose_times_are_not_forbidden():
    text = ("(01:38) (02:09) [(03:00)](https://www.youtube.com/watch?v=x&t=180s) "
            "[(04:00)](https://www.bilibili.com/video/BVx?t=240) 会议在 10:30 开始（见附录）")
    assert forbidden_citations(text) == []


def test_nested_brackets_around_an_allowed_citation_are_fine():
    for text in ("(see (01:38))", "((01:38))", "[note (01:38)]", "（见 (01:38)）",
                 "[(01:38)](https://www.youtube.com/watch?v=x&t=98s) and （06:00）"):
        found = forbidden_citations(text)
        assert found == ([] if "06:00" not in text else ["（06:00）"]), text


def test_a_bracketed_ratio_or_time_of_day_is_refused_with_a_hint():
    """Documented trade-off: anything bracketed that looks like a clock is a citation."""
    assert forbidden_citations("画面比例 (16:9) 与 (下午3:30)") == ["(16:9)", "(下午3:30)"]
    report = check_summary({"duration": 600.0}, [{"start": 0.0, "end": 590.0}],
                           "Summary (05:00) 比例 (16:9)")
    assert any("without brackets" in f for f in report["failures"])


def test_parse_timestamp_accepts_ascii_mm_ss_only():
    assert parse_timestamp("12:34") == 754.0 and parse_timestamp("95:30") == 5730.0
    for bad in ("12：34", "1:02:03", "０１:38", "12:60", "12:3"):
        assert parse_timestamp(bad) is None, bad


def test_only_the_allowed_form_is_linked_in_place():
    text, problems = linkify_summary("看这里 (02:15) 和（02:20）", url="https://www.youtube.com/watch?v=x")
    assert text == "看这里 [(02:15)](https://www.youtube.com/watch?v=x&t=135s) 和（02:20）"
    assert problems == []
    again, _ = linkify_summary(text, url="https://www.youtube.com/watch?v=x")
    assert again == text


def test_a_forbidden_form_fails_the_summary_check():
    report = check_summary({"duration": 600.0}, [{"start": 0.0, "end": 590.0}],
                           "Summary (05:00) 以及（06:00）")
    assert not report["ok"]
    assert any("other than (MM:SS)" in f and "（06:00）" in f for f in report["failures"])
    assert report["details"]["timestamps_cited"] == 1


def test_speech_ending_before_the_video_is_a_warning_not_a_failure():
    """Credits and music end a film without speech, which looks the same as a
    transcript that stopped early, so a reader decides which it is."""
    report = check_summary({"duration": 600.0}, [{"start": 0.0, "end": 400.0}], "Summary (05:00)")
    assert report["ok"]
    assert any("confirm the last 200s" in w for w in report["warnings"])


def test_a_citation_past_the_end_still_fails():
    report = check_summary({"duration": 600.0}, [{"start": 0.0, "end": 590.0}], "Summary (20:00)")
    assert not report["ok"] and "past the end" in report["failures"][0]
