from __future__ import annotations

import gc
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from vstudio.cues import offset_words
from vstudio.media import media_duration

ASR_MODEL_ID = "Qwen/Qwen3-ASR-1.7B-hf"

ALIGNER_MODEL_ID = "Qwen/Qwen3-ForcedAligner-0.6B-hf"

# Smallest slot a repaired word may occupy. ASS timestamps have 10 ms
# resolution, so 20 ms is the shortest span that survives rounding into the
# subtitle format as a distinct cue boundary.
MIN_REPAIRED_SECONDS = 0.02


def validate_aligned_words(
    words: Sequence[Mapping[str, Any]], *, chunk_start: float, chunk_end: float
) -> bool:
    previous_end = chunk_start
    tolerance = 0.05
    for word in words:
        start = float(word["start_time"])
        end = float(word["end_time"])
        if start < chunk_start - tolerance or end > chunk_end + tolerance:
            raise ValueError(f"aligned word {start}..{end} is outside chunk {chunk_start}..{chunk_end}")
        if start < previous_end - tolerance:
            raise ValueError(f"aligned words overlap at {start}; previous word ended at {previous_end}")
        if end < start:
            raise ValueError(f"aligned word ends before it starts: {start}..{end}")
        previous_end = end
    return True


def clamp_words_to_chunk(
    words: Sequence[Mapping[str, Any]], *, chunk_start: float, chunk_end: float
) -> list[dict[str, Any]]:
    """Pull word times back inside the chunk they belong to.

    The forced aligner routinely puts the last word of a chunk a little past the
    chunk end. Those words are not degenerate, so the repair pass leaves them
    alone, and `validate_aligned_words` then rejects the whole chunk and throws
    away a completed ASR pass. Clamping first costs a few milliseconds of
    accuracy on one word and turns a hard crash into a flagged timestamp.
    """
    clamped: list[dict[str, Any]] = []
    for word in words:
        item = dict(word)
        start = float(item["start_time"])
        end = float(item["end_time"])
        if start < chunk_start or end > chunk_end:
            item["start_time"] = round(min(max(start, chunk_start), chunk_end), 6)
            item["end_time"] = round(min(max(end, chunk_start), chunk_end), 6)
            item["timestamp_repaired"] = True
        clamped.append(item)
    return clamped


def repair_degenerate_word_times(
    words: Sequence[Mapping[str, Any]], *, chunk_start: float, chunk_end: float
) -> list[dict[str, Any]]:
    repaired = [dict(word) for word in words]
    index = 0
    previous_end = chunk_start
    while index < len(repaired):
        word = repaired[index]
        start = float(word["start_time"])
        end = float(word["end_time"])
        if end > start and start >= previous_end:
            previous_end = end
            index += 1
            continue

        run_start = index
        run_end = index
        while run_end + 1 < len(repaired):
            candidate = repaired[run_end + 1]
            candidate_start = float(candidate["start_time"])
            candidate_end = float(candidate["end_time"])
            if candidate_end > candidate_start and candidate_start > previous_end:
                break
            run_end += 1

        right = chunk_end
        if run_end + 1 < len(repaired):
            right = float(repaired[run_end + 1]["start_time"])
        count = run_end - run_start + 1
        available = right - previous_end
        if available <= 0:
            # The run sits at or past the chunk's right edge, which happens when
            # the preceding word already ends on the boundary. There is no
            # forward budget to divide, so borrow a minimum slot per word
            # backwards from the preceding word. Failing here would throw away a
            # completed transcription over the last word of a chunk.
            needed = count * MIN_REPAIRED_SECONDS
            floor = chunk_start
            if run_start > 0:
                previous_start = float(repaired[run_start - 1]["start_time"])
                previous_span = float(repaired[run_start - 1]["end_time"]) - previous_start
                # Never take more than half of the neighbour. That bounds how far
                # a repair can distort a word that was timed correctly, and keeps
                # the neighbour positive even when it is itself only milliseconds
                # long, which is the case that a fixed minimum floor got wrong.
                floor = previous_start + previous_span / 2
            borrowed = max(floor, right - needed)
            if right - borrowed <= 0:
                # The neighbour had nothing to give. Take the space from the
                # chunk start instead; a crash here would discard a completed
                # transcription over the width of one word.
                borrowed = max(chunk_start, right - needed)
            if right - borrowed <= 0:
                raise ValueError(
                    f"cannot repair aligned words {run_start}..{run_end} inside chunk {chunk_start}..{chunk_end}"
                )
            if run_start > 0:
                previous_word = repaired[run_start - 1]
                previous_word["end_time"] = round(borrowed, 6)
                previous_word["timestamp_repaired"] = True
            previous_end = borrowed
            available = right - previous_end
        step = available / count
        for position in range(count):
            item = repaired[run_start + position]
            item["start_time"] = round(previous_end + step * position, 6)
            item["end_time"] = round(previous_end + step * (position + 1), 6)
            item["timestamp_repaired"] = True
        previous_end = float(repaired[run_end]["end_time"])
        index = run_end + 1
    return repaired


def build_aligned_chunk(
    raw_chunk: Mapping[str, Any], aligned_words: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    chunk_id = int(raw_chunk["chunk_id"])
    start = float(raw_chunk["start"])
    end = float(raw_chunk["end"])
    shifted = offset_words(aligned_words, start, chunk_id=chunk_id)
    if shifted:
        shifted = clamp_words_to_chunk(shifted, chunk_start=start, chunk_end=end)
        shifted = repair_degenerate_word_times(shifted, chunk_start=start, chunk_end=end)
        validate_aligned_words(shifted, chunk_start=start, chunk_end=end)
    return {
        "chunk_id": chunk_id,
        "start": start,
        "end": end,
        "audio": str(raw_chunk["audio"]),
        "language": str(raw_chunk["language"]),
        "transcript": str(raw_chunk["transcript"]),
        "words": shifted,
    }


def release_model(model: Any) -> None:
    del model
    gc.collect()
    try:
        import torch

        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def transcribe_alignment_chunks(
    chunks: Sequence[Path],
    *,
    context: str,
    model_id: str = ASR_MODEL_ID,
) -> list[dict[str, Any]]:
    import torch

    processor, model, device, dtype = load_asr_model(model_id)
    records: list[dict[str, Any]] = []
    cursor = 0.0
    try:
        for chunk_id, chunk in enumerate(chunks):
            duration = media_duration(chunk)
            inputs = processor.apply_transcription_request(audio=str(chunk), prompt=context)
            inputs = inputs.to(device, dtype)
            with torch.inference_mode():
                output_ids = model.generate(**inputs, max_new_tokens=4096, do_sample=False)
            generated_ids = output_ids[:, inputs["input_ids"].shape[1] :]
            parsed = processor.decode(generated_ids, return_format="parsed")[0]
            transcript = str(parsed["transcription"]).strip()
            language = str(parsed.get("language") or "English")
            # A silent stretch is ordinary: an intro card, music, a pause
            # between speakers. Failing the job over one would mean a single
            # quiet window throws away an hour of transcription.
            records.append(
                {
                    "chunk_id": chunk_id,
                    "start": cursor,
                    "end": cursor + duration,
                    "audio": str(chunk),
                    "language": language,
                    "transcript": transcript,
                }
            )
            cursor += duration
    finally:
        release_model(model)
    return records


def load_asr_model(model_id: str = ASR_MODEL_ID):
    import torch
    from transformers import AutoModelForMultimodalLM, AutoProcessor

    if torch.backends.mps.is_available():
        device = torch.device("mps")
        dtype = torch.float16
    elif torch.cuda.is_available():
        device = torch.device("cuda")
        dtype = torch.float16
    else:
        device = torch.device("cpu")
        dtype = torch.float32

    processor = AutoProcessor.from_pretrained(model_id)
    model = AutoModelForMultimodalLM.from_pretrained(
        model_id,
        dtype=dtype,
        attn_implementation="eager",
        low_cpu_mem_usage=True,
    ).to(device)
    model.eval()
    return processor, model, device, dtype


def load_aligner_model(model_id: str = ALIGNER_MODEL_ID):
    import torch
    from transformers import AutoModelForTokenClassification, AutoProcessor

    if torch.backends.mps.is_available():
        device = torch.device("mps")
        dtype = torch.float16
    elif torch.cuda.is_available():
        device = torch.device("cuda")
        dtype = torch.float16
    else:
        device = torch.device("cpu")
        dtype = torch.float32
    processor = AutoProcessor.from_pretrained(model_id)
    model = AutoModelForTokenClassification.from_pretrained(
        model_id,
        dtype=dtype,
        low_cpu_mem_usage=True,
    ).to(device)
    model.eval()
    return processor, model, device, dtype


def align_transcribed_chunks(
    raw_chunks: Sequence[Mapping[str, Any]],
    *,
    model_id: str = ALIGNER_MODEL_ID,
) -> list[dict[str, Any]]:
    import torch

    if all(not str(raw.get("transcript") or "").strip() for raw in raw_chunks):
        # Nothing to align, and loading a 600 MB model to prove it is waste.
        return [build_aligned_chunk(raw, []) for raw in raw_chunks]
    processor, model, device, dtype = load_aligner_model(model_id)
    aligned: list[dict[str, Any]] = []
    try:
        for raw in raw_chunks:
            if not str(raw.get("transcript") or "").strip():
                aligned.append(build_aligned_chunk(raw, []))
                continue
            inputs, word_lists = processor.prepare_forced_aligner_inputs(
                audio=str(raw["audio"]),
                transcript=str(raw["transcript"]),
                language=str(raw["language"]),
            )
            inputs = inputs.to(device, dtype)
            with torch.inference_mode():
                outputs = model(**inputs)
            timestamps = processor.decode_forced_alignment(
                logits=outputs.logits,
                input_ids=inputs["input_ids"],
                word_lists=word_lists,
                timestamp_token_id=model.config.timestamp_token_id,
            )[0]
            aligned.append(build_aligned_chunk(raw, timestamps))
    finally:
        release_model(model)
    return aligned


# --- Backend ---

from vstudio.asr.base import Capabilities, Window, Word  # noqa: E402


class QwenLocalBackend:
    """Qwen3-ASR for text, then Qwen3-ForcedAligner for word times.

    Both models are large and a laptop GPU holds one at a time, so this backend
    runs as a batch: every window is transcribed, the ASR model is released,
    then every window is aligned. That is why it implements ``transcribe_all``
    instead of the per-window ``transcribe``.
    """

    capabilities = Capabilities(
        name="qwen-local",
        # The ASR model has a bounded audio context, so long media must be cut.
        needs_segmentation=True,
        segment_seconds=45,
        # One GPU, one window at a time. Parallelism here only thrashes memory.
        max_parallel=1,
        returns_word_times=True,
        requirements=(
            "the local-asr extra (torch, transformers) and about 5.5 GB of "
            "cached Qwen weights"
        ),
        prefers_whole_file=False,
        extras=("local-asr",),
    )

    def preflight(self) -> None:
        try:
            import torch  # noqa: F401
            import transformers  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "local ASR is not installed. Run `uv sync --extra local-asr` in "
                "scripts/vstudio, or choose --provider mai."
            ) from exc

    def report(self, *, live: bool = False) -> dict[str, Any]:
        """Extra detail for doctor: whether the weights are already cached.

        `live` is ignored: there is no endpoint to reach, and loading the model
        to prove it works would take minutes.
        """
        from vstudio.media import asr_weights_present

        return {
            "asr_weights_cached": bool(asr_weights_present(ASR_MODEL_ID)),
            "aligner_weights_cached": bool(asr_weights_present(ALIGNER_MODEL_ID)),
        }

    def transcribe_all(self, windows, *, context, language):
        raw = transcribe_alignment_chunks(
            [w.path for w in windows], context=context, model_id=ASR_MODEL_ID
        )
        for record, window in zip(raw, windows):
            record["start"], record["end"] = 0.0, window.duration
        aligned = align_transcribed_chunks(raw)
        results = []
        for record, chunk in zip(raw, aligned):
            words = [
                Word(str(w["text"]), float(w["start_time"]), float(w["end_time"]),
                     bool(w.get("timestamp_repaired")))
                for w in chunk["words"]
            ]
            results.append(
                (str(record["transcript"]), words, {"language": str(record["language"])})
            )
        return results

    def transcribe(self, window, *, context, language):
        return self.transcribe_all([window], context=context, language=language)[0]
