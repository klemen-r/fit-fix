"""Tests for the paired MyWhoosh vs Garmin-native comparison helpers."""

from __future__ import annotations

from pathlib import Path

from fix_fit import (
    F_TIMESTAMP,
    MSG_RECORD,
    MSG_SESSION,
    TYPE_SINT32,
    TYPE_UINT8,
    TYPE_UINT16,
    TYPE_UINT32,
    Message,
    _pack_field,
)
from paired_compare import (
    _format_optional,
    align_record_streams,
    compare_record_streams,
    compare_reverse_engineered_fields,
    write_paired_reports,
)


def _record(timestamp: int, heart_rate: int | None = None, power: int | None = None, cadence: int | None = None) -> Message:
    fields = [_pack_field("<", F_TIMESTAMP, TYPE_UINT32, timestamp)]
    if heart_rate is not None:
        fields.append(_pack_field("<", 3, TYPE_UINT8, heart_rate))
    if cadence is not None:
        fields.append(_pack_field("<", 4, TYPE_UINT8, cadence))
    if power is not None:
        fields.append(_pack_field("<", 7, TYPE_UINT16, power))
    return Message(MSG_RECORD, "<", tuple(fields))


def _session(values: dict[int, tuple[int, int]]) -> Message:
    fields = tuple(_pack_field("<", number, base_type, value) for number, (base_type, value) in values.items())
    return Message(MSG_SESSION, "<", fields)


def test_align_record_streams_pairs_by_timestamp_within_tolerance() -> None:
    # Each right record is consumed at most once and matched to the closest left record
    # in tolerance order. With tolerance=1s, t=1001 (left) has no t=1001 on the right and
    # consumes the next available right record within tolerance (t=1002).
    left = [_record(1000, heart_rate=120), _record(1001, heart_rate=121), _record(1002, heart_rate=122)]
    right = [_record(1000, heart_rate=119), _record(1002, heart_rate=124)]
    matches = align_record_streams(left, right, 3)
    assert matches == [(120, 119), (121, 124)]


def test_align_record_streams_exact_timestamps() -> None:
    left = [_record(1000 + index, heart_rate=120 + index) for index in range(3)]
    right = [_record(1000 + index, heart_rate=130 + index) for index in range(3)]
    matches = align_record_streams(left, right, 3)
    assert matches == [(120, 130), (121, 131), (122, 132)]


def test_align_record_streams_handles_start_offset() -> None:
    left = [_record(1000 + index, heart_rate=120 + index) for index in range(5)]
    right = [_record(1003 + index, heart_rate=130 + index) for index in range(5)]
    matches = align_record_streams(left, right, 3)
    # left t=1003 pairs right t=1003, etc. Tolerance is +-1s so t=1002 also matches t=1003.
    assert (122, 130) in matches
    assert (123, 130) in matches or (123, 131) in matches
    assert (124, 131) in matches or (124, 132) in matches


def test_align_record_streams_ignores_invalid_values() -> None:
    left = [_record(1000), _record(1001, heart_rate=120)]
    right = [_record(1000, heart_rate=119), _record(1001, heart_rate=121)]
    matches = align_record_streams(left, right, 3)
    assert matches == [(120, 121)]


def test_compare_record_streams_computes_overlap_and_stats() -> None:
    left = [_record(1000 + index, heart_rate=120 + index, power=200) for index in range(5)]
    right = [_record(1000 + index, heart_rate=120 + index, power=210) for index in range(5)]
    comparison = compare_record_streams(left, right)
    overlap = comparison["overlap"]
    assert overlap["left_record_count"] == 5
    assert overlap["right_record_count"] == 5
    assert overlap["start_offset_s"] == 0
    assert overlap["overlap_seconds"] == 4
    hr = comparison["streams"]["heart_rate"]
    assert hr["matched_pairs"] == 5
    assert hr["exact_equal_pairs"] == 5
    assert hr["mean_abs_diff"] == 0
    power = comparison["streams"]["power"]
    assert power["matched_pairs"] == 5
    assert power["exact_equal_pairs"] == 0
    assert power["mean_abs_diff"] == 10


def test_compare_reverse_engineered_fields_reports_estimate_error() -> None:
    left_session = _session(
        {
            24: (TYPE_UINT8, 0xFF),  # absent (invalid)
            137: (TYPE_UINT8, 0xFF),
        }
    )
    # Garmin native session: aerobic TE = 4.2, anaerobic TE = 1.5, training load = 200.0.
    right_session = _session(
        {
            24: (TYPE_UINT8, 42),
            137: (TYPE_UINT8, 15),
            168: (TYPE_SINT32, round(200.0 * 65536)),
        }
    )
    inferred = {
        "written": True,
        "aerobic_te": 3.8,
        "anaerobic_te": 2.0,
        "training_load": 165.783,
    }
    output = compare_reverse_engineered_fields([left_session], [right_session], inferred)
    assert output["aerobic_te"]["mywhoosh_decoded"] is None
    assert output["aerobic_te"]["garmin_native_decoded"] == 4.2
    assert output["aerobic_te"]["reverse_engineered_estimate"] == 3.8
    assert abs(output["aerobic_te"]["absolute_error_vs_native"] - 0.4) < 1e-6
    assert output["anaerobic_te"]["garmin_native_decoded"] == 1.5
    assert abs(output["anaerobic_te"]["absolute_error_vs_native"] - 0.5) < 1e-6
    assert output["training_load"]["garmin_native_decoded"] == 200.0
    assert abs(output["training_load"]["absolute_error_vs_native"] - 34.217) < 1e-3


def test_compare_reverse_engineered_fields_handles_missing_native_and_estimate() -> None:
    left_session = _session({})
    right_session = _session({})
    output = compare_reverse_engineered_fields(
        [left_session], [right_session], inferred_metrics=None
    )
    for entry in output.values():
        assert entry["garmin_native_decoded"] is None
        assert entry["reverse_engineered_estimate"] is None
        assert entry["absolute_error_vs_native"] is None
        assert entry["relative_error_vs_native"] is None


def test_format_optional_handles_none_and_floats() -> None:
    assert _format_optional(None) == "-"
    assert _format_optional(1.23456) == "1.235"
    assert _format_optional(42) == "42"
    assert _format_optional(0, " s") == "0 s"


def test_write_paired_reports_renders_three_markdown_files(tmp_path: Path) -> None:
    result = {
        "inputs": {
            "mywhoosh": {
                "path": "mywhoosh.fit",
                "sha256": "a" * 64,
                "size": 1234,
                "file_id": {"manufacturer": 331, "product": 3570, "serial_number": 1},
                "message_inventory": [],
                "unknown_fields": {},
                "sdk_errors": [],
                "sdk_session": {},
            },
            "garmin_native": {
                "path": "fr265.fit",
                "sha256": "b" * 64,
                "size": 5678,
                "file_id": {"manufacturer": 1, "product": 4440, "serial_number": 2},
                "message_inventory": [],
                "unknown_fields": {},
                "sdk_errors": [],
                "sdk_session": {},
            },
        },
        "records": {
            "overlap": {
                "left_record_count": 10,
                "right_record_count": 10,
                "left_first_timestamp": "2024-01-01T00:00:00+00:00",
                "left_last_timestamp": "2024-01-01T00:00:09+00:00",
                "right_first_timestamp": "2024-01-01T00:00:00+00:00",
                "right_last_timestamp": "2024-01-01T00:00:09+00:00",
                "start_offset_s": 0,
                "overlap_seconds": 9,
            },
            "streams": {
                name: {
                    "left_present": 0,
                    "right_present": 0,
                    "matched_pairs": 0,
                    "exact_equal_pairs": 0,
                    "mean_abs_diff": None,
                    "max_abs_diff": None,
                    "left_mean": None,
                    "right_mean": None,
                }
                for name in (
                    "heart_rate",
                    "cadence",
                    "distance",
                    "speed",
                    "power",
                    "altitude",
                    "enhanced_altitude",
                    "position_lat",
                    "position_long",
                )
            },
        },
        "session": {
            "total_timer_time_ms": {"field_number": 8, "left": 1000, "right": 1000, "delta": 0}
        },
        "reverse_engineered_fields": {
            "aerobic_te": {
                "field_number": 24,
                "scale_divisor": 10.0,
                "mywhoosh_raw": None,
                "mywhoosh_decoded": None,
                "garmin_native_raw": 42,
                "garmin_native_decoded": 4.2,
                "reverse_engineered_estimate": 3.8,
                "absolute_error_vs_native": 0.4,
                "relative_error_vs_native": 0.0952,
            },
            "anaerobic_te": {
                "field_number": 137,
                "scale_divisor": 10.0,
                "mywhoosh_raw": None,
                "mywhoosh_decoded": None,
                "garmin_native_raw": 15,
                "garmin_native_decoded": 1.5,
                "reverse_engineered_estimate": 2.0,
                "absolute_error_vs_native": 0.5,
                "relative_error_vs_native": 0.333,
            },
            "training_load": {
                "field_number": 168,
                "scale_divisor": 65536.0,
                "mywhoosh_raw": None,
                "mywhoosh_decoded": None,
                "garmin_native_raw": 13107200,
                "garmin_native_decoded": 200.0,
                "reverse_engineered_estimate": 165.783,
                "absolute_error_vs_native": 34.217,
                "relative_error_vs_native": 0.171,
            },
        },
        "device_info": {"left": [], "right": []},
        "user_profile_and_zones": {
            "user_profile": {"left": [], "right": []},
            "zones_target": {"left": [], "right": []},
        },
        "sport": {
            "left": None,
            "right": None,
            "session_sport_left": 2,
            "session_sport_right": 2,
            "session_sub_sport_left": 6,
            "session_sub_sport_right": 6,
        },
        "events_laps_activity": {
            "events_left": [],
            "events_right": [],
            "laps_left": [],
            "laps_right": [],
            "activity_left": [],
            "activity_right": [],
        },
        "inferred_metrics_attempt": {"written": True, "aerobic_te": 3.8},
    }
    write_paired_reports(tmp_path, result)
    paired = (tmp_path / "paired_mywhoosh_vs_fr265.md").read_text(encoding="utf-8")
    metric = (tmp_path / "paired_metric_fields.md").read_text(encoding="utf-8")
    recommendations = (tmp_path / "paired_conversion_recommendations.md").read_text(encoding="utf-8")
    assert "Paired MyWhoosh vs Garmin-Native Comparison" in paired
    assert "heart_rate" in paired
    assert "aerobic_te" in metric
    assert "structural_only" in recommendations
