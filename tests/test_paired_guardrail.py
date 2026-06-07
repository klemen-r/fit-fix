"""Tests for the paired-comparison mismatch guardrail."""

from __future__ import annotations

from paired_compare import MISMATCH_FLAG, evaluate_alignment_guardrail


def test_guardrail_passes_when_offset_small_and_overlap_positive() -> None:
    verdict = evaluate_alignment_guardrail(
        {"start_offset_s": 5, "overlap_seconds": 600}
    )
    assert verdict["flag"] == "OK"
    assert verdict["is_valid_for_estimator_validation"] is True
    assert verdict["reasons"] == []


def test_guardrail_flags_large_offset() -> None:
    verdict = evaluate_alignment_guardrail(
        {"start_offset_s": -680252, "overlap_seconds": 0}
    )
    assert verdict["flag"] == MISMATCH_FLAG
    assert verdict["is_valid_for_estimator_validation"] is False
    assert any("start offset" in reason for reason in verdict["reasons"])
    assert any("zero overlapping" in reason for reason in verdict["reasons"])


def test_guardrail_flags_zero_overlap_alone() -> None:
    verdict = evaluate_alignment_guardrail(
        {"start_offset_s": 60, "overlap_seconds": 0}
    )
    assert verdict["flag"] == MISMATCH_FLAG
    assert any("zero overlapping" in reason for reason in verdict["reasons"])


def test_guardrail_handles_missing_offset() -> None:
    verdict = evaluate_alignment_guardrail({"start_offset_s": None, "overlap_seconds": 0})
    assert verdict["flag"] == MISMATCH_FLAG
    assert any("could not be computed" in reason for reason in verdict["reasons"])
