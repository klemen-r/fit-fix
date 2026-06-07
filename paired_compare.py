"""Compare a MyWhoosh ride against a Garmin-native recording of the same ride.

The Garmin-native FIT (for example an FR265 Indoor Bike / Virtual Cycling
recording captured simultaneously with the MyWhoosh export) is used as
ground truth for proprietary Garmin fields that the donor pipeline can only
infer empirically.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from fix_fit import (
    FIT_EPOCH,
    F_TIMESTAMP,
    MSG_ACTIVITY,
    MSG_DEVICE_INFO,
    MSG_EVENT,
    MSG_FILE_ID,
    MSG_LAP,
    MSG_RECORD,
    MSG_SESSION,
    MSG_SPORT,
    Message,
    _parse_fit,
    _read_sint32,
    _read_uint,
)
from garmin_pipeline import (
    MSG_USER_PROFILE,
    MSG_ZONES_TARGET,
    REVERSE_ENGINEERED_SESSION_FIELDS,
    _field_name,
    _message_name,
    _sdk_decode,
    _write_json,
    _write_text,
)

MISMATCH_THRESHOLD_S = 300  # 5 minutes
MISMATCH_FLAG = "INVALID_FOR_ESTIMATOR_VALIDATION"


def evaluate_alignment_guardrail(overlap: dict[str, Any]) -> dict[str, Any]:
    """Return a guardrail verdict for a paired-comparison overlap dict.

    The verdict flags two failure modes:
    * the two rides are offset by more than MISMATCH_THRESHOLD_S seconds at start
    * the two rides have zero overlapping seconds
    """

    start_offset = overlap.get("start_offset_s")
    overlap_seconds = overlap.get("overlap_seconds") or 0
    reasons: list[str] = []
    if start_offset is None:
        reasons.append("start offset could not be computed (missing timestamps)")
    elif abs(start_offset) > MISMATCH_THRESHOLD_S:
        reasons.append(
            f"start offset {start_offset} s exceeds {MISMATCH_THRESHOLD_S} s threshold"
        )
    if overlap_seconds <= 0:
        reasons.append("rides have zero overlapping seconds")
    return {
        "flag": MISMATCH_FLAG if reasons else "OK",
        "is_valid_for_estimator_validation": not reasons,
        "reasons": reasons,
        "start_offset_s": start_offset,
        "overlap_seconds": overlap_seconds,
        "threshold_s": MISMATCH_THRESHOLD_S,
    }


RECORD_FIELDS = {
    3: ("heart_rate", "bpm", False),
    4: ("cadence", "rpm", False),
    5: ("distance", "raw (1/100 m)", False),
    6: ("speed", "raw (mm/s)", False),
    7: ("power", "watts", False),
    2: ("altitude", "raw", False),
    78: ("enhanced_altitude", "raw", False),
    0: ("position_lat", "semicircles", True),
    1: ("position_long", "semicircles", True),
}

SESSION_FIELDS_SUMMARY = {
    2: "start_time",
    5: "sport",
    6: "sub_sport",
    7: "total_elapsed_time_ms",
    8: "total_timer_time_ms",
    9: "total_distance_raw",
    11: "total_calories",
    14: "avg_speed_raw",
    15: "max_speed_raw",
    16: "avg_heart_rate",
    17: "max_heart_rate",
    18: "avg_cadence",
    19: "max_cadence",
    20: "avg_power",
    21: "max_power",
    22: "total_ascent",
    23: "total_descent",
    34: "normalized_power",
    48: "total_work",
}

DEVICE_INFO_FIELDS = {
    0: "device_index",
    1: "device_type",
    2: "manufacturer",
    3: "serial_number",
    4: "product",
    5: "software_version",
    6: "hardware_version",
}

USER_PROFILE_FIELDS = {
    1: "gender",
    2: "age",
    3: "height_raw",
    4: "weight_raw",
    5: "language",
    8: "resting_heart_rate",
    9: "default_max_running_heart_rate",
    10: "default_max_biking_heart_rate",
    11: "default_max_heart_rate",
    16: "wake_time",
    17: "sleep_time",
}

ZONES_TARGET_FIELDS = {
    1: "max_heart_rate",
    2: "threshold_heart_rate",
    3: "functional_threshold_power",
    5: "hr_calc_type",
    7: "pwr_calc_type",
}

REVERSE_ENGINEERED_FIELD_NAMES = {
    24: ("aerobic_te", 10.0),
    137: ("anaerobic_te", 10.0),
    168: ("training_load", 65536.0),
}


@dataclass(frozen=True)
class StreamComparison:
    field_name: str
    left_count: int
    right_count: int
    common_count: int
    equal_count: int
    mean_abs_diff: Optional[float]
    max_abs_diff: Optional[int]
    left_mean: Optional[float]
    right_mean: Optional[float]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _messages_of(messages: Sequence[Message], number: int) -> list[Message]:
    return [message for message in messages if message.global_message == number]


def _read_field(record: Message, number: int, signed: bool) -> Optional[int]:
    return _read_sint32(record, number) if signed else _read_uint(record, number)


def _fit_to_iso(value: Optional[int]) -> Optional[str]:
    if value is None:
        return None
    seconds = FIT_EPOCH.timestamp() + value
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


def align_record_streams(
    left_records: Sequence[Message],
    right_records: Sequence[Message],
    field_number: int,
    *,
    signed: bool = False,
    tolerance_s: int = 1,
) -> list[tuple[int, int]]:
    """Match record values by timestamp within +/- tolerance seconds.

    Returns matched (left_value, right_value) pairs in left order.
    Each right-side record is consumed at most once.
    """

    left_pairs: list[tuple[int, int]] = []
    for record in left_records:
        timestamp = _read_uint(record, F_TIMESTAMP)
        value = _read_field(record, field_number, signed)
        if timestamp is not None and value is not None:
            left_pairs.append((timestamp, value))
    right_pairs: list[tuple[int, int]] = []
    for record in right_records:
        timestamp = _read_uint(record, F_TIMESTAMP)
        value = _read_field(record, field_number, signed)
        if timestamp is not None and value is not None:
            right_pairs.append((timestamp, value))

    if not left_pairs or not right_pairs:
        return []

    matches: list[tuple[int, int]] = []
    matched_right: set[int] = set()
    right_cursor = 0
    for left_timestamp, left_value in left_pairs:
        while (
            right_cursor < len(right_pairs)
            and right_pairs[right_cursor][0] < left_timestamp - tolerance_s
        ):
            right_cursor += 1
        candidate_index = right_cursor
        best: Optional[tuple[int, int, int]] = None
        while (
            candidate_index < len(right_pairs)
            and right_pairs[candidate_index][0] <= left_timestamp + tolerance_s
        ):
            if candidate_index in matched_right:
                candidate_index += 1
                continue
            right_timestamp, right_value = right_pairs[candidate_index]
            distance = abs(right_timestamp - left_timestamp)
            if best is None or distance < best[0]:
                best = (distance, candidate_index, right_value)
            candidate_index += 1
        if best is not None:
            matches.append((left_value, best[2]))
            matched_right.add(best[1])
    return matches


def _stream_stats(
    field_name: str,
    left_count: int,
    right_count: int,
    matches: Sequence[tuple[int, int]],
) -> StreamComparison:
    if matches:
        diffs = [abs(left - right) for left, right in matches]
        left_values = [left for left, _ in matches]
        right_values = [right for _, right in matches]
        return StreamComparison(
            field_name=field_name,
            left_count=left_count,
            right_count=right_count,
            common_count=len(matches),
            equal_count=sum(1 for difference in diffs if difference == 0),
            mean_abs_diff=statistics.mean(diffs),
            max_abs_diff=max(diffs),
            left_mean=statistics.mean(left_values),
            right_mean=statistics.mean(right_values),
        )
    return StreamComparison(
        field_name=field_name,
        left_count=left_count,
        right_count=right_count,
        common_count=0,
        equal_count=0,
        mean_abs_diff=None,
        max_abs_diff=None,
        left_mean=None,
        right_mean=None,
    )


def compare_record_streams(
    left_messages: Sequence[Message], right_messages: Sequence[Message]
) -> dict[str, Any]:
    left_records = _messages_of(left_messages, MSG_RECORD)
    right_records = _messages_of(right_messages, MSG_RECORD)
    left_timestamps = [
        _read_uint(record, F_TIMESTAMP) for record in left_records
    ]
    right_timestamps = [
        _read_uint(record, F_TIMESTAMP) for record in right_records
    ]
    left_valid_timestamps = [value for value in left_timestamps if value is not None]
    right_valid_timestamps = [value for value in right_timestamps if value is not None]
    overlap: dict[str, Any] = {
        "left_record_count": len(left_records),
        "right_record_count": len(right_records),
        "left_first_timestamp": _fit_to_iso(min(left_valid_timestamps) if left_valid_timestamps else None),
        "left_last_timestamp": _fit_to_iso(max(left_valid_timestamps) if left_valid_timestamps else None),
        "right_first_timestamp": _fit_to_iso(min(right_valid_timestamps) if right_valid_timestamps else None),
        "right_last_timestamp": _fit_to_iso(max(right_valid_timestamps) if right_valid_timestamps else None),
    }
    if left_valid_timestamps and right_valid_timestamps:
        start_offset_s = min(right_valid_timestamps) - min(left_valid_timestamps)
        overlap_start = max(min(left_valid_timestamps), min(right_valid_timestamps))
        overlap_end = min(max(left_valid_timestamps), max(right_valid_timestamps))
        overlap["start_offset_s"] = start_offset_s
        overlap["overlap_seconds"] = max(0, overlap_end - overlap_start)
    else:
        overlap["start_offset_s"] = None
        overlap["overlap_seconds"] = 0

    streams = {}
    for number, (name, _unit, signed) in RECORD_FIELDS.items():
        matches = align_record_streams(
            left_records, right_records, number, signed=signed
        )
        left_count = sum(
            1 for record in left_records if _read_field(record, number, signed) is not None
        )
        right_count = sum(
            1 for record in right_records if _read_field(record, number, signed) is not None
        )
        stats = _stream_stats(name, left_count, right_count, matches)
        streams[name] = {
            "left_present": stats.left_count,
            "right_present": stats.right_count,
            "matched_pairs": stats.common_count,
            "exact_equal_pairs": stats.equal_count,
            "mean_abs_diff": stats.mean_abs_diff,
            "max_abs_diff": stats.max_abs_diff,
            "left_mean": stats.left_mean,
            "right_mean": stats.right_mean,
        }
    return {"overlap": overlap, "streams": streams}


def _summary_value(message: Optional[Message], number: int) -> Optional[int]:
    if message is None:
        return None
    return _read_uint(message, number)


def compare_session(
    left_messages: Sequence[Message], right_messages: Sequence[Message]
) -> dict[str, Any]:
    left = next(iter(_messages_of(left_messages, MSG_SESSION)), None)
    right = next(iter(_messages_of(right_messages, MSG_SESSION)), None)
    rows = {}
    for number, name in SESSION_FIELDS_SUMMARY.items():
        left_value = _summary_value(left, number)
        right_value = _summary_value(right, number)
        rows[name] = {
            "field_number": number,
            "left": left_value,
            "right": right_value,
            "delta": (
                right_value - left_value
                if left_value is not None and right_value is not None
                else None
            ),
        }
    return rows


def compare_reverse_engineered_fields(
    left_messages: Sequence[Message],
    right_messages: Sequence[Message],
    inferred_metrics: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    left = next(iter(_messages_of(left_messages, MSG_SESSION)), None)
    right = next(iter(_messages_of(right_messages, MSG_SESSION)), None)
    output: dict[str, Any] = {}
    for number in REVERSE_ENGINEERED_SESSION_FIELDS:
        name, scale = REVERSE_ENGINEERED_FIELD_NAMES[number]
        if number == 168:
            left_raw = _read_sint32(left, number) if left is not None else None
            right_raw = _read_sint32(right, number) if right is not None else None
        else:
            left_raw = _summary_value(left, number)
            right_raw = _summary_value(right, number)
        left_scaled = left_raw / scale if left_raw is not None else None
        right_scaled = right_raw / scale if right_raw is not None else None
        inferred_value = (
            inferred_metrics.get(name)
            if inferred_metrics and inferred_metrics.get("written")
            else None
        )
        absolute_error = (
            abs(inferred_value - right_scaled)
            if inferred_value is not None and right_scaled is not None
            else None
        )
        relative_error = (
            absolute_error / abs(right_scaled)
            if absolute_error is not None and right_scaled not in (None, 0)
            else None
        )
        output[name] = {
            "field_number": number,
            "scale_divisor": scale,
            "mywhoosh_raw": left_raw,
            "mywhoosh_decoded": left_scaled,
            "garmin_native_raw": right_raw,
            "garmin_native_decoded": right_scaled,
            "reverse_engineered_estimate": inferred_value,
            "absolute_error_vs_native": absolute_error,
            "relative_error_vs_native": relative_error,
        }
    return output


def _describe_message(message: Message, fields: dict[int, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for number, name in fields.items():
        out[name] = _read_uint(message, number)
    return out


def compare_device_info(
    left_messages: Sequence[Message], right_messages: Sequence[Message]
) -> dict[str, Any]:
    return {
        "left": [
            _describe_message(message, DEVICE_INFO_FIELDS)
            for message in _messages_of(left_messages, MSG_DEVICE_INFO)
        ],
        "right": [
            _describe_message(message, DEVICE_INFO_FIELDS)
            for message in _messages_of(right_messages, MSG_DEVICE_INFO)
        ],
    }


def compare_profile_and_zones(
    left_messages: Sequence[Message], right_messages: Sequence[Message]
) -> dict[str, Any]:
    return {
        "user_profile": {
            "left": [
                _describe_message(message, USER_PROFILE_FIELDS)
                for message in _messages_of(left_messages, MSG_USER_PROFILE)
            ],
            "right": [
                _describe_message(message, USER_PROFILE_FIELDS)
                for message in _messages_of(right_messages, MSG_USER_PROFILE)
            ],
        },
        "zones_target": {
            "left": [
                _describe_message(message, ZONES_TARGET_FIELDS)
                for message in _messages_of(left_messages, MSG_ZONES_TARGET)
            ],
            "right": [
                _describe_message(message, ZONES_TARGET_FIELDS)
                for message in _messages_of(right_messages, MSG_ZONES_TARGET)
            ],
        },
    }


def compare_sport(
    left_messages: Sequence[Message], right_messages: Sequence[Message]
) -> dict[str, Any]:
    sport_fields = {0: "sport", 1: "sub_sport", 3: "name_raw"}
    left = next(iter(_messages_of(left_messages, MSG_SPORT)), None)
    right = next(iter(_messages_of(right_messages, MSG_SPORT)), None)
    return {
        "left": _describe_message(left, sport_fields) if left is not None else None,
        "right": _describe_message(right, sport_fields) if right is not None else None,
        "session_sport_left": _summary_value(
            next(iter(_messages_of(left_messages, MSG_SESSION)), None), 5
        ),
        "session_sport_right": _summary_value(
            next(iter(_messages_of(right_messages, MSG_SESSION)), None), 5
        ),
        "session_sub_sport_left": _summary_value(
            next(iter(_messages_of(left_messages, MSG_SESSION)), None), 6
        ),
        "session_sub_sport_right": _summary_value(
            next(iter(_messages_of(right_messages, MSG_SESSION)), None), 6
        ),
    }


def compare_events_laps_activity(
    left_messages: Sequence[Message], right_messages: Sequence[Message]
) -> dict[str, Any]:
    def _event_summary(message: Message) -> dict[str, Any]:
        return {
            "timestamp": _read_uint(message, F_TIMESTAMP),
            "event": _read_uint(message, 0),
            "event_type": _read_uint(message, 1),
        }

    def _lap_summary(message: Message) -> dict[str, Any]:
        return {
            "start_time": _read_uint(message, 2),
            "timestamp": _read_uint(message, F_TIMESTAMP),
            "total_elapsed_time_ms": _read_uint(message, 7),
            "total_timer_time_ms": _read_uint(message, 8),
            "total_distance_raw": _read_uint(message, 9),
            "avg_heart_rate": _read_uint(message, 15),
            "max_heart_rate": _read_uint(message, 16),
            "avg_power": _read_uint(message, 19),
            "max_power": _read_uint(message, 20),
        }

    def _activity_summary(message: Message) -> dict[str, Any]:
        return {
            "timestamp": _read_uint(message, F_TIMESTAMP),
            "total_timer_time_ms": _read_uint(message, 0),
            "num_sessions": _read_uint(message, 1),
            "type": _read_uint(message, 2),
            "event": _read_uint(message, 3),
            "event_type": _read_uint(message, 4),
            "local_timestamp": _read_uint(message, 5),
        }

    return {
        "events_left": [_event_summary(message) for message in _messages_of(left_messages, MSG_EVENT)],
        "events_right": [_event_summary(message) for message in _messages_of(right_messages, MSG_EVENT)],
        "laps_left": [_lap_summary(message) for message in _messages_of(left_messages, MSG_LAP)],
        "laps_right": [_lap_summary(message) for message in _messages_of(right_messages, MSG_LAP)],
        "activity_left": [
            _activity_summary(message) for message in _messages_of(left_messages, MSG_ACTIVITY)
        ],
        "activity_right": [
            _activity_summary(message) for message in _messages_of(right_messages, MSG_ACTIVITY)
        ],
    }


def _file_id_summary(messages: Sequence[Message]) -> dict[str, Any]:
    file_id = next(iter(_messages_of(messages, MSG_FILE_ID)), None)
    if file_id is None:
        return {}
    return {
        "type": _read_uint(file_id, 0),
        "manufacturer": _read_uint(file_id, 1),
        "product": _read_uint(file_id, 2),
        "serial_number": _read_uint(file_id, 3),
        "time_created": _read_uint(file_id, 4),
    }


def _message_inventory(messages: Sequence[Message]) -> list[dict[str, Any]]:
    counts: dict[int, int] = {}
    for message in messages:
        counts[message.global_message] = counts.get(message.global_message, 0) + 1
    inventory = []
    for number in sorted(counts):
        inventory.append(
            {
                "number": number,
                "name": _message_name(number),
                "count": counts[number],
                "known_to_sdk": not _message_name(number).startswith("unknown_"),
            }
        )
    return inventory


def _unknown_field_summary(messages: Sequence[Message]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for message in messages:
        for field in message.fields:
            name = _field_name(message.global_message, field.number)
            if name.startswith("unknown_"):
                key = f"{message.global_message}.{field.number}"
                counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _load_inferred_metrics(notes_path: Optional[Path]) -> Optional[dict[str, Any]]:
    if notes_path is None or not notes_path.is_file():
        return None
    payload = json.loads(notes_path.read_text(encoding="utf-8"))
    return payload.get("reverse_engineered_metrics_attempt")


def run_paired_comparison(
    mywhoosh_path: Path,
    garmin_native_path: Path,
    outputs_dir: Path,
    notes_path: Optional[Path] = None,
) -> dict[str, Any]:
    mywhoosh_data = mywhoosh_path.read_bytes()
    garmin_data = garmin_native_path.read_bytes()
    _, mywhoosh_messages = _parse_fit(mywhoosh_data)
    _, garmin_messages = _parse_fit(garmin_data)
    inferred = _load_inferred_metrics(notes_path)
    sdk_left, sdk_left_errors, _, _ = _sdk_decode(mywhoosh_data)
    sdk_right, sdk_right_errors, _, _ = _sdk_decode(garmin_data)

    record_comparison = compare_record_streams(mywhoosh_messages, garmin_messages)
    guardrail = evaluate_alignment_guardrail(record_comparison["overlap"])
    session_comparison = compare_session(mywhoosh_messages, garmin_messages)
    re_comparison = compare_reverse_engineered_fields(
        mywhoosh_messages, garmin_messages, inferred
    )
    device_info_comparison = compare_device_info(mywhoosh_messages, garmin_messages)
    profile_comparison = compare_profile_and_zones(mywhoosh_messages, garmin_messages)
    sport_comparison = compare_sport(mywhoosh_messages, garmin_messages)
    events_comparison = compare_events_laps_activity(mywhoosh_messages, garmin_messages)

    result = {
        "inputs": {
            "mywhoosh": {
                "path": str(mywhoosh_path),
                "sha256": _sha256(mywhoosh_data),
                "size": len(mywhoosh_data),
                "file_id": _file_id_summary(mywhoosh_messages),
                "message_inventory": _message_inventory(mywhoosh_messages),
                "unknown_fields": _unknown_field_summary(mywhoosh_messages),
                "sdk_errors": sdk_left_errors,
                "sdk_session": (sdk_left.get("session_mesgs") or [{}])[0],
            },
            "garmin_native": {
                "path": str(garmin_native_path),
                "sha256": _sha256(garmin_data),
                "size": len(garmin_data),
                "file_id": _file_id_summary(garmin_messages),
                "message_inventory": _message_inventory(garmin_messages),
                "unknown_fields": _unknown_field_summary(garmin_messages),
                "sdk_errors": sdk_right_errors,
                "sdk_session": (sdk_right.get("session_mesgs") or [{}])[0],
            },
        },
        "records": record_comparison,
        "session": session_comparison,
        "reverse_engineered_fields": re_comparison,
        "device_info": device_info_comparison,
        "user_profile_and_zones": profile_comparison,
        "sport": sport_comparison,
        "events_laps_activity": events_comparison,
        "inferred_metrics_attempt": inferred,
        "alignment_guardrail": guardrail,
    }
    write_paired_reports(outputs_dir / "reports", result)
    _write_json(outputs_dir / "paired_comparison.json", result)
    return result


def _format_optional(value: Any, suffix: str = "") -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3f}{suffix}"
    return f"{value}{suffix}"


def _records_section(record_comparison: dict[str, Any]) -> str:
    overlap = record_comparison["overlap"]
    streams = record_comparison["streams"]
    lines = [
        "| Field | MyWhoosh present | Garmin present | Matched pairs | Exact | Mean abs diff | Max abs diff | MyWhoosh mean | Garmin mean |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, item in streams.items():
        lines.append(
            "| `{name}` | {left} | {right} | {matched} | {exact} | {mad} | {maxd} | {lm} | {rm} |".format(
                name=name,
                left=item["left_present"],
                right=item["right_present"],
                matched=item["matched_pairs"],
                exact=item["exact_equal_pairs"],
                mad=_format_optional(item["mean_abs_diff"]),
                maxd=_format_optional(item["max_abs_diff"]),
                lm=_format_optional(item["left_mean"]),
                rm=_format_optional(item["right_mean"]),
            )
        )
    overlap_lines = (
        f"- MyWhoosh records: {overlap['left_record_count']} from "
        f"`{overlap['left_first_timestamp']}` to `{overlap['left_last_timestamp']}`\n"
        f"- Garmin native records: {overlap['right_record_count']} from "
        f"`{overlap['right_first_timestamp']}` to `{overlap['right_last_timestamp']}`\n"
        f"- Start offset (Garmin - MyWhoosh): "
        f"{_format_optional(overlap['start_offset_s'], ' s')}\n"
        f"- Overlapping coverage: {overlap['overlap_seconds']} s"
    )
    return overlap_lines + "\n\n" + "\n".join(lines)


def _session_section(session_comparison: dict[str, Any]) -> str:
    lines = [
        "| Session field | # | MyWhoosh | Garmin native | Delta |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, item in session_comparison.items():
        lines.append(
            "| `{name}` | {n} | {left} | {right} | {delta} |".format(
                name=name,
                n=item["field_number"],
                left=_format_optional(item["left"]),
                right=_format_optional(item["right"]),
                delta=_format_optional(item["delta"]),
            )
        )
    return "\n".join(lines)


def _reverse_engineered_table(re_comparison: dict[str, Any]) -> str:
    lines = [
        "| Metric | Field | MyWhoosh decoded | Garmin native decoded | RE estimate | Abs error | Rel error |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, item in re_comparison.items():
        lines.append(
            "| {name} | {field} | {l} | {r} | {est} | {ae} | {re} |".format(
                name=name,
                field=item["field_number"],
                l=_format_optional(item["mywhoosh_decoded"]),
                r=_format_optional(item["garmin_native_decoded"]),
                est=_format_optional(item["reverse_engineered_estimate"]),
                ae=_format_optional(item["absolute_error_vs_native"]),
                re=_format_optional(item["relative_error_vs_native"]),
            )
        )
    return "\n".join(lines)


def _device_info_section(device_comparison: dict[str, Any]) -> str:
    def _render(rows: Sequence[dict[str, Any]]) -> str:
        if not rows:
            return "  (none)"
        lines = []
        for row in rows:
            lines.append(
                "  - manufacturer=`{m}` product=`{p}` serial=`{s}` device_type=`{t}` software_version=`{sv}`".format(
                    m=row.get("manufacturer"),
                    p=row.get("product"),
                    s=row.get("serial_number"),
                    t=row.get("device_type"),
                    sv=row.get("software_version"),
                )
            )
        return "\n".join(lines)

    return (
        "MyWhoosh `device_info` messages:\n"
        + _render(device_comparison["left"])
        + "\n\nGarmin native `device_info` messages:\n"
        + _render(device_comparison["right"])
    )


def _profile_section(profile_comparison: dict[str, Any]) -> str:
    user = profile_comparison["user_profile"]
    zones = profile_comparison["zones_target"]
    return (
        f"MyWhoosh user_profile: {user['left'] or '(none)'}\n\n"
        f"Garmin native user_profile: {user['right'] or '(none)'}\n\n"
        f"MyWhoosh zones_target: {zones['left'] or '(none)'}\n\n"
        f"Garmin native zones_target: {zones['right'] or '(none)'}"
    )


def _ground_truth_interpretation(result: dict[str, Any]) -> str:
    re_section = result["reverse_engineered_fields"]
    inferred = result.get("inferred_metrics_attempt") or {}
    lines = []
    for name, item in re_section.items():
        native = item["garmin_native_decoded"]
        estimate = item["reverse_engineered_estimate"]
        absolute_error = item["absolute_error_vs_native"]
        if native is None:
            lines.append(
                f"- `{name}`: Garmin-native value is absent. Either the FR265 did not write "
                f"field {item['field_number']} for this sport/sub_sport, or the watch did not "
                f"compute the metric for this ride. The reverse-engineered estimate cannot be "
                f"validated against this recording."
            )
        elif estimate is None:
            lines.append(
                f"- `{name}`: Garmin-native value is {native} but no reverse-engineered "
                f"estimate was produced. Re-run the donor pipeline to populate "
                f"`reverse_engineered_metrics_attempt` before comparing."
            )
        else:
            qualifier = "close" if absolute_error is not None and absolute_error <= 0.5 else "off"
            lines.append(
                f"- `{name}`: Garmin native {native}, reverse-engineered estimate {estimate}, "
                f"absolute error {absolute_error} ({qualifier}). Treat as a single-point check, "
                f"not a calibration."
            )
    if not inferred.get("written"):
        lines.append(
            "- Reverse-engineered metrics were not written (insufficient donor evidence). "
            "Run `garmin_pipeline.py build` first to produce an estimate."
        )
    return "\n".join(lines)


def write_paired_reports(reports_dir: Path, result: dict[str, Any]) -> None:
    overlap = result["records"]["overlap"]
    streams = result["records"]["streams"]
    hr = streams["heart_rate"]
    power = streams["power"]
    cadence = streams["cadence"]
    distance = streams["distance"]
    speed = streams["speed"]
    left_file_id = result["inputs"]["mywhoosh"]["file_id"]
    right_file_id = result["inputs"]["garmin_native"]["file_id"]
    re_section = result["reverse_engineered_fields"]
    guardrail = result.get("alignment_guardrail") or {
        "flag": "OK",
        "reasons": [],
        "start_offset_s": None,
        "overlap_seconds": None,
        "threshold_s": MISMATCH_THRESHOLD_S,
    }

    _write_text(
        reports_dir / "paired_mywhoosh_vs_fr265.md",
        f"""# Paired MyWhoosh vs Garmin-Native Comparison

Inputs:

- MyWhoosh: `{result['inputs']['mywhoosh']['path']}`
  - SHA-256: `{result['inputs']['mywhoosh']['sha256']}`
  - manufacturer={left_file_id.get('manufacturer')}, product={left_file_id.get('product')}, serial={left_file_id.get('serial_number')}
- Garmin native: `{result['inputs']['garmin_native']['path']}`
  - SHA-256: `{result['inputs']['garmin_native']['sha256']}`
  - manufacturer={right_file_id.get('manufacturer')}, product={right_file_id.get('product')}, serial={right_file_id.get('serial_number')}

Garmin FIT SDK errors (MyWhoosh): {result['inputs']['mywhoosh']['sdk_errors'] or 'none'}
Garmin FIT SDK errors (Garmin native): {result['inputs']['garmin_native']['sdk_errors'] or 'none'}

**Alignment guardrail:** `{guardrail['flag']}`{(' - ' + '; '.join(guardrail['reasons'])) if guardrail['reasons'] else ''}.

## Timestamp Alignment

{_records_section(result['records'])}

## Session Summary

{_session_section(result['session'])}

## Sport / Sub-sport

- MyWhoosh sport message: `{result['sport']['left']}`
- Garmin native sport message: `{result['sport']['right']}`
- Session sport: MyWhoosh=`{result['sport']['session_sport_left']}` vs Garmin=`{result['sport']['session_sport_right']}`
- Session sub_sport: MyWhoosh=`{result['sport']['session_sub_sport_left']}` vs Garmin=`{result['sport']['session_sub_sport_right']}`

## Device Info

{_device_info_section(result['device_info'])}

## User Profile and Zone Targets

{_profile_section(result['user_profile_and_zones'])}

## Events / Laps / Activity

- MyWhoosh events: {len(result['events_laps_activity']['events_left'])}; Garmin native events: {len(result['events_laps_activity']['events_right'])}
- MyWhoosh laps: {len(result['events_laps_activity']['laps_left'])}; Garmin native laps: {len(result['events_laps_activity']['laps_right'])}
- MyWhoosh activity messages: {len(result['events_laps_activity']['activity_left'])}; Garmin native activity messages: {len(result['events_laps_activity']['activity_right'])}

Full per-message detail lives in `outputs/paired_comparison.json`.

## Quick Read

- HR matched pairs: {hr['matched_pairs']} (mean abs diff {_format_optional(hr['mean_abs_diff'])} bpm)
- Power matched pairs: {power['matched_pairs']} (mean abs diff {_format_optional(power['mean_abs_diff'])} W)
- Cadence matched pairs: {cadence['matched_pairs']} (mean abs diff {_format_optional(cadence['mean_abs_diff'])} rpm)
- Distance matched pairs: {distance['matched_pairs']}
- Speed matched pairs: {speed['matched_pairs']}
- Total overlap: {overlap['overlap_seconds']} s, start offset {_format_optional(overlap['start_offset_s'], ' s')}

Treat large start offsets as evidence the two recordings did not in fact cover the same
ride; revisit before drawing conclusions.
""",
    )

    if guardrail["flag"] != "OK":
        warning_block = (
            f"> **{guardrail['flag']}** - the two FITs do not describe the same ride.\n"
            f">\n"
            f"> Reasons: {'; '.join(guardrail['reasons'])}.\n"
            f">\n"
            f"> TE/load error numbers below are meaningless for estimator validation in\n"
            f"> this state. Structural and field-location evidence remains usable.\n"
        )
    else:
        warning_block = (
            "Alignment guardrail: OK "
            f"(start offset {guardrail['start_offset_s']} s, "
            f"overlap {guardrail['overlap_seconds']} s, threshold "
            f"{guardrail['threshold_s']} s).\n"
        )

    _write_text(
        reports_dir / "paired_metric_fields.md",
        f"""# Paired Proprietary Metric Field Comparison

This focuses on the Garmin proprietary session fields the donor pipeline targets.

{warning_block}

| Classification labels | |
|---|---|
| `aerobic_te` (session field 24) | exact FIT field location; **unknown/proprietary** calculation |
| `anaerobic_te` (session field 137) | exact FIT field location; **unknown/proprietary** calculation |
| `training_load` (session field 168) | exact FIT field location; **unknown/proprietary** calculation |

{_reverse_engineered_table(re_section)}

## Ground-truth interpretation

{_ground_truth_interpretation(result)}

## Limits

- A single paired recording is a single ground-truth sample. Treat agreement as
  encouraging but not as calibration.
- The FR265 may compute Training Effect and Load on the watch and not write all of them
  into the FIT until cloud sync. If a field is absent here it does not necessarily mean
  the watch did not compute it.
- These fields are **unknown/proprietary calculation** with **unproven upload behavior**
  from the donor pipeline's perspective.
""",
    )

    _write_text(
        reports_dir / "paired_conversion_recommendations.md",
        f"""# Paired Conversion Recommendations

Source: Garmin-native FR265 recording compared with the simultaneous MyWhoosh export.

## What the paired recording tells us

- Public ride metrics (HR, power, cadence, distance, speed) can be cross-validated:
  HR matched pairs {hr['matched_pairs']} mean abs diff {_format_optional(hr['mean_abs_diff'])},
  power matched pairs {power['matched_pairs']} mean abs diff {_format_optional(power['mean_abs_diff'])}.
- Sport/sub_sport on the Garmin native recording is the authoritative target for
  matching the watch's classification of the ride.
- `device_info` on the Garmin side shows the actual watch device chain. Do **not** copy
  Garmin-native `device_info` content into MyWhoosh variants verbatim - it would assert
  another piece of hardware authored the file. Continue using the donor's structural
  schema only.
- `zones_target` and `user_profile` on the Garmin recording can be compared with the
  donor's copied values to confirm the donor pipeline is not writing stale or conflicting
  personal data.

## How this changes the upload-test plan

The current matrix in `test_matrix.md` orders structural variants first and the
reverse-engineered attempt last. The paired recording does not change that order:

1. structural_only
2. structural + HR zones
3. structural + power zones
4. structural + time-in-zone
5. reverse-engineered metrics attempt

It does suggest two extra checks once a paired recording is available:

- Before upload, confirm that the MyWhoosh variant's `total_timer_time` and
  `total_distance` agree with the Garmin-native recording within sensor tolerance.
- After upload, compare Garmin Connect's reported Training Effect / Load for the
  MyWhoosh variant with the Garmin-native ride. The Garmin-native value is the
  closest available proxy for "correct".

## Out of scope

The paired comparison cannot recover Recovery Time, Load Focus, or Training Status
from the FIT alone - those live in the Garmin cloud and on the watch.
""",
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare a MyWhoosh FIT with a Garmin-native recording of the same ride."
    )
    parser.add_argument("--mywhoosh", required=True, type=Path)
    parser.add_argument("--garmin-native", required=True, type=Path)
    parser.add_argument("--outputs", type=Path, default=Path("outputs"))
    parser.add_argument(
        "--notes",
        type=Path,
        default=Path("outputs/pipeline_notes.json"),
        help="Optional pipeline_notes.json with the reverse-engineered estimate.",
    )
    arguments = parser.parse_args(argv)
    run_paired_comparison(
        arguments.mywhoosh,
        arguments.garmin_native,
        arguments.outputs,
        arguments.notes,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
