"""The local backend's logic, with the two model calls faked.

The models are 5.5 GB and need a GPU, so these do not load them. What they do
cover is everything between: the batch contract the runner relies on, the
window-relative timing, and the silent-window case that used to fail a whole
job. The model calls themselves remain untested here and are exercised only by
a real run with the local-asr extra installed.
"""

import pytest

from vstudio.asr import qwen_local
from vstudio.asr.base import Window


def _aligned(*words):
    return [{"text": t, "start_time": s, "end_time": e} for t, s, e in words]


@pytest.fixture
def fake_models(monkeypatch, tmp_path):
    """Stand in for Qwen3-ASR and the forced aligner."""
    spoken = {0: "hello there", 1: "", 2: "goodbye"}
    timings = {
        0: _aligned(("hello", 0.0, 0.5), ("there", 0.6, 1.0)),
        2: _aligned(("goodbye", 0.0, 0.9)),
    }

    def transcribe(chunks, *, context, model_id):
        return [
            {"chunk_id": i, "start": 0.0, "end": 45.0, "audio": str(c),
             "language": "English", "transcript": spoken[i]}
            for i, c in enumerate(chunks)
        ]

    def align(raw_chunks, *, model_id=qwen_local.ALIGNER_MODEL_ID):
        return [
            qwen_local.build_aligned_chunk(raw, timings.get(int(raw["chunk_id"]), []))
            for raw in raw_chunks
        ]

    monkeypatch.setattr(qwen_local, "transcribe_alignment_chunks", transcribe)
    monkeypatch.setattr(qwen_local, "align_transcribed_chunks", align)
    return [Window(i, tmp_path / f"c{i}.wav", i * 45.0, 45.0) for i in range(3)]


def test_the_batch_contract_matches_what_the_runner_expects(fake_models):
    """The runner calls transcribe_all and unpacks (text, words, extra)."""
    results = qwen_local.QwenLocalBackend().transcribe_all(
        fake_models, context="", language=None
    )
    assert len(results) == len(fake_models)
    for text, words, extra in results:
        assert isinstance(text, str) and isinstance(words, list)
        assert extra["language"] == "English"


def test_word_times_stay_relative_to_their_own_window(fake_models):
    """The runner adds the window offset, so a backend that pre-shifted would
    put every word after the first window twice as far along the video."""
    results = qwen_local.QwenLocalBackend().transcribe_all(
        fake_models, context="", language=None
    )
    last_words = results[2][1]
    assert last_words[0].start == 0.0, "third window must not carry its own offset"
    assert last_words[0].text == "goodbye"


def test_a_silent_window_yields_no_words_instead_of_failing_the_job(fake_models):
    """An intro card or a pause between speakers is ordinary. Failing over one
    would throw away every other window's transcription."""
    results = qwen_local.QwenLocalBackend().transcribe_all(
        fake_models, context="", language=None
    )
    assert results[1] == ("", [], {"language": "English"})
    assert [len(words) for _, words, _ in results] == [2, 0, 1]


def test_an_interpolated_timestamp_survives_into_the_word(fake_models, monkeypatch):
    """verify warns on a long run of these, which needs the flag to arrive."""
    chunk = qwen_local.build_aligned_chunk(
        {"chunk_id": 0, "start": 0.0, "end": 10.0, "audio": "a", "language": "English",
         "transcript": "a b"},
        _aligned(("a", 1.0, 1.0), ("b", 2.0, 3.0)),
    )
    assert chunk["words"][0]["timestamp_repaired"] is True


def test_the_backend_declares_what_the_runner_needs_to_schedule_it():
    caps = qwen_local.QwenLocalBackend.capabilities
    assert caps.needs_segmentation and caps.segment_seconds
    assert caps.max_parallel == 1, "one GPU holds one model at a time"
    assert caps.accepts_codecs == () and caps.convert_to == (), "local ASR wants raw audio"
    assert "local-asr" in caps.extras


def test_preflight_names_the_command_that_fixes_it(monkeypatch):
    import builtins

    real = builtins.__import__

    def missing(name, *args, **kwargs):
        if name in ("torch", "transformers"):
            raise ImportError(name)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    with pytest.raises(RuntimeError, match="uv sync --extra local-asr"):
        qwen_local.QwenLocalBackend().preflight()
