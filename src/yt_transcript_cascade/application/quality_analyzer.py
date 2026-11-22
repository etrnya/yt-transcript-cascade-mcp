"""Multi-dimensional TranscriptQualityReport analyzer (no single combined score)."""

from __future__ import annotations

from ..domain.models import TranscriptQualityReport, TranscriptSegment


def analyze_transcript_quality(
    segments: list[TranscriptSegment],
    *,
    duration_sec: float | None = None,
    expected_languages: list[str] | None = None,
    detected_language: str | None = None,
) -> TranscriptQualityReport:
    """Evaluate structural, coverage, repetition, and language quality independently."""
    empty_count = 0
    timestamp_monotonic = True
    repetition_anomalies = 0

    prev_start = -1.0
    prev_text = ""
    consecutive_repeats = 0
    covered_sec = 0.0
    has_confidence = False

    for seg in segments:
        text = seg.text.strip()
        if not text:
            empty_count += 1
        if seg.start < prev_start or seg.end < seg.start:
            timestamp_monotonic = False
        prev_start = seg.start

        if seg.end > seg.start:
            covered_sec += seg.end - seg.start

        if text and text == prev_text:
            consecutive_repeats += 1
            if consecutive_repeats >= 2:
                repetition_anomalies += 1
        else:
            consecutive_repeats = 0
            prev_text = text

        if seg.confidence is not None:
            has_confidence = True

    coverage_ratio: float | None = None
    if duration_sec and duration_sec > 0:
        coverage_ratio = round(min(covered_sec / duration_sec, 1.0), 4)

    language_consistent: bool | None = None
    if expected_languages and detected_language:
        det_base = detected_language.lower().split("-")[0]
        exp_bases = {lang.lower().split("-")[0] for lang in expected_languages}
        language_consistent = det_base in exp_bases

    structural_ok = len(segments) > 0 and timestamp_monotonic and empty_count == 0
    warnings_present = (
        not structural_ok
        or repetition_anomalies > 0
        or (coverage_ratio is not None and coverage_ratio < 0.3)
        or (language_consistent is False)
    )

    return TranscriptQualityReport(
        structural_ok=structural_ok,
        empty_segment_count=empty_count,
        repetition_anomaly_count=repetition_anomalies,
        duration_coverage_ratio=coverage_ratio,
        language_consistent=language_consistent,
        confidence_note=(
            "Engine-estimated metric only; not a calibrated word accuracy rate."
            if has_confidence
            else None
        ),
        status="WARNING" if warnings_present else "OK",
    )
