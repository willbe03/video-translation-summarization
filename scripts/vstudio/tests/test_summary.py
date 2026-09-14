from vstudio.summary import check_summary
from vstudio.timestamps import iter_timestamps, linkify_summary, parse_timestamp


def test_full_width_citations_are_found_like_ascii_ones():
    text = "开场（00:15）然后（104:10）与（1：02：03）以及(12:34）和【05:00～06:00】"
    assert [seconds for _, seconds in iter_timestamps(text)] == [15.0, 6250.0, 3723.0, 754.0, 300.0]


def test_a_full_width_colon_parses():
    assert parse_timestamp("12：34") == 754.0


def test_a_full_width_citation_is_linked_in_place():
    text, problems = linkify_summary("看这里（02:15）", url="https://www.youtube.com/watch?v=x")
    assert text == "看这里[（02:15）](https://www.youtube.com/watch?v=x&t=135s)"
    assert problems == []


def test_speech_ending_before_the_video_is_a_warning_not_a_failure():
    """Credits and music end a film without speech, which looks the same as a
    transcript that stopped early, so a reader decides which it is."""
    report = check_summary({"duration": 600.0}, [{"start": 0.0, "end": 400.0}], "Summary (05:00)")
    assert report["ok"]
    assert any("confirm the last 200s" in w for w in report["warnings"])


def test_a_citation_past_the_end_still_fails():
    report = check_summary({"duration": 600.0}, [{"start": 0.0, "end": 590.0}], "Summary (20:00)")
    assert not report["ok"] and "past the end" in report["failures"][0]
