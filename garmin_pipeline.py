"""Empirical Garmin donor/template pipeline for MyWhoosh FIT activities."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import struct
import zipfile
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence
from xml.etree import ElementTree

from profile_config import ProfileConfig, load_profile_config

from fix_fit import (
    FIT_EPOCH,
    F_MESSAGE_INDEX,
    F_TIMESTAMP,
    MSG_ACTIVITY,
    MSG_DEVICE_INFO,
    MSG_EVENT,
    MSG_FILE_CREATOR,
    MSG_FILE_ID,
    MSG_LAP,
    MSG_RECORD,
    MSG_SESSION,
    MSG_SPORT,
    TYPE_ENUM,
    TYPE_SINT32,
    TYPE_UINT8,
    TYPE_UINT16,
    TYPE_UINT32,
    Message,
    RawField,
    _dedupe,
    _encode_fit,
    _local_timestamp,
    _normalized_power,
    _pack_field,
    _parse_fit,
    _read_uint,
    _timed_record_total,
    _vertical_totals,
    convert_fit_bytes,
    fit_crc,
)

try:
    from garmin_fit_sdk import Decoder, Profile, Stream
except ImportError:  # pragma: no cover - validation reports the missing SDK
    Decoder = None
    Profile = None
    Stream = None


MSG_DEVICE_SETTINGS = 2
MSG_USER_PROFILE = 3
MSG_ZONES_TARGET = 7
MSG_HR_ZONE = 8
MSG_POWER_ZONE = 9
MSG_TRAINING_SETTINGS = 13
MSG_TIME_IN_ZONE = 216

VARIANTS = (
    "structural_only",
    "structural_plus_hr_zones",
    "structural_plus_power_zones",
    "structural_plus_time_in_zone",
    "reverse_engineered_metrics_attempt",
    "07_profile_zones_injected",
)

PUBLIC_SESSION_FIELDS = {
    0,
    1,
    2,
    3,
    4,
    5,
    6,
    7,
    8,
    9,
    10,
    11,
    14,
    15,
    16,
    17,
    18,
    19,
    20,
    21,
    22,
    23,
    25,
    26,
    28,
    29,
    30,
    31,
    32,
    34,
    38,
    39,
    48,
    110,
    124,
    125,
    126,
    127,
    128,
    139,
    253,
    254,
}
PUBLIC_LAP_FIELDS = {
    0,
    1,
    2,
    3,
    4,
    5,
    6,
    7,
    8,
    9,
    10,
    11,
    13,
    14,
    15,
    16,
    17,
    18,
    19,
    20,
    21,
    22,
    23,
    24,
    25,
    26,
    33,
    39,
    41,
    42,
    43,
    62,
    110,
    111,
    112,
    113,
    114,
    121,
    253,
    254,
}
HR_PROFILE_FIELDS = {1, 2, 3, 4, 8, 9, 10, 11, 12, 17}
POWER_PROFILE_FIELDS = HR_PROFILE_FIELDS | {16}
HR_TARGET_FIELDS = {1, 2, 5, 254}
POWER_TARGET_FIELDS = HR_TARGET_FIELDS | {3, 7}
REVERSE_ENGINEERED_SESSION_FIELDS = {24, 137, 168}
INJECTED_DEFAULT_SUB_SPORT = 7  # indoor_cycling - matches Garmin-native indoor rides


@dataclass(frozen=True)
class FitInput:
    label: str
    source: Path
    data: bytes
    container: Optional[Path] = None


@dataclass(frozen=True)
class RideMetrics:
    start: int
    end: int
    elapsed_ms: int
    timer_ms: int
    distance_raw: Optional[int]
    cycles: Optional[int]
    calories: Optional[int]
    ascent: Optional[int]
    descent: Optional[int]
    total_work: Optional[int]
    normalized_power: Optional[int]
    avg_speed_raw: Optional[int]
    max_speed_raw: Optional[int]
    avg_hr: Optional[int]
    max_hr: Optional[int]
    avg_cadence: Optional[int]
    max_cadence: Optional[int]
    avg_power: Optional[int]
    max_power: Optional[int]


@dataclass(frozen=True)
class DonorEvidence:
    label: str
    timestamp: Optional[int]
    duration_s: Optional[float]
    avg_hr: Optional[int]
    max_hr: Optional[int]
    hr_zone_fractions: tuple[float, ...]
    aerobic_te: Optional[float]
    anaerobic_te: Optional[float]
    training_load: Optional[float]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _message_name(number: int) -> str:
    if Profile is not None and number in Profile["messages"]:
        return Profile["messages"][number]["name"]
    return f"unknown_{number}"


def _field_name(message_number: int, field_number: int) -> str:
    if Profile is not None:
        message = Profile["messages"].get(message_number)
        if message and field_number in message["fields"]:
            return message["fields"][field_number]["name"]
    return f"unknown_{field_number}"


def _field_classification(message_number: int, field_number: int) -> str:
    if message_number == MSG_SESSION and field_number in REVERSE_ENGINEERED_SESSION_FIELDS:
        return "unknown/proprietary calculation; exact FIT field location"
    if _message_name(message_number).startswith("unknown_") or _field_name(
        message_number, field_number
    ).startswith("unknown_"):
        return "unknown/proprietary"
    return "exact/public FIT profile field"


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def _write_json(path: Path, value: Any) -> None:
    _write_text(path, json.dumps(_json_safe(value), indent=2, sort_keys=True))


def _load_zip_fit_files(directory: Path) -> list[FitInput]:
    inputs: list[FitInput] = []
    for archive in sorted(directory.glob("*.zip")):
        with zipfile.ZipFile(archive) as source:
            for name in sorted(source.namelist()):
                if name.lower().endswith(".fit"):
                    inputs.append(
                        FitInput(
                            label=Path(name).stem,
                            source=Path(name),
                            data=source.read(name),
                            container=archive,
                        )
                    )
    return inputs


def _load_fit(path: Path) -> FitInput:
    return FitInput(path.stem, path, path.read_bytes())


def _sdk_decode(data: bytes) -> tuple[dict[str, list[dict[str, Any]]], list[str], list[int], list[dict[str, Any]]]:
    if Decoder is None or Stream is None:
        return {}, ["garmin-fit-sdk is not installed"], [], []
    order: list[int] = []
    definitions: list[dict[str, Any]] = []
    messages, errors = Decoder(Stream.from_byte_array(data)).read(
        mesg_listener=lambda number, _message: order.append(number),
        mesg_definition_listener=lambda definition: definitions.append(definition),
    )
    return messages, [str(error) for error in errors], order, definitions


def _sdk_raw_decode(data: bytes) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    if Decoder is None or Stream is None:
        return {}, ["garmin-fit-sdk is not installed"]
    messages, errors = Decoder(Stream.from_byte_array(data)).read(
        apply_scale_and_offset=False,
        convert_datetimes_to_dates=False,
        convert_types_to_strings=True,
        expand_components=False,
        merge_heart_rates=False,
    )
    return messages, [str(error) for error in errors]


def _rle(numbers: Iterable[int]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for number in numbers:
        if output and output[-1]["message_number"] == number:
            output[-1]["count"] += 1
        else:
            output.append(
                {
                    "message_number": number,
                    "message_name": _message_name(number),
                    "count": 1,
                }
            )
    return output


def _timestamp_iso(value: Optional[int]) -> Optional[str]:
    if value is None:
        return None
    return (FIT_EPOCH + (datetime.fromtimestamp(value, timezone.utc) - datetime.fromtimestamp(0, timezone.utc))).isoformat()


def _field_schema(message: Message) -> list[dict[str, Any]]:
    return [
        {
            "number": field.number,
            "name": _field_name(message.global_message, field.number),
            "size": field.size,
            "base_type": f"0x{field.base_type:02x}",
        }
        for field in message.fields
    ]


def analyze_fit(source: FitInput) -> dict[str, Any]:
    header, raw_messages = _parse_fit(source.data)
    decoded, sdk_errors, order, definitions = _sdk_decode(source.data)
    counts = Counter(message.global_message for message in raw_messages)
    schemas: dict[int, Counter[tuple[tuple[int, int, int], ...]]] = {}
    timestamp_ranges: dict[int, list[int]] = {}
    developer_counts: Counter[int] = Counter()
    for message in raw_messages:
        schema = tuple(
            (field.number, field.size, field.base_type) for field in message.fields
        )
        schemas.setdefault(message.global_message, Counter())[schema] += 1
        timestamp = _read_uint(message, F_TIMESTAMP)
        if timestamp is not None:
            timestamp_ranges.setdefault(message.global_message, []).append(timestamp)
        developer_counts[message.global_message] += len(message.developer_fields)

    message_types = []
    for number, count in sorted(counts.items()):
        representatives = schemas[number].most_common(5)
        message_types.append(
            {
                "number": number,
                "name": _message_name(number),
                "known_to_sdk_profile": not _message_name(number).startswith("unknown_"),
                "count": count,
                "developer_field_count": developer_counts[number],
                "timestamp_first": (
                    _timestamp_iso(min(timestamp_ranges[number]))
                    if number in timestamp_ranges
                    else None
                ),
                "timestamp_last": (
                    _timestamp_iso(max(timestamp_ranges[number]))
                    if number in timestamp_ranges
                    else None
                ),
                "schemas": [
                    {
                        "count": schema_count,
                        "fields": [
                            {
                                "number": field_number,
                                "name": _field_name(number, field_number),
                                "size": size,
                                "base_type": f"0x{base_type:02x}",
                                "classification": _field_classification(
                                    number, field_number
                                ),
                            }
                            for field_number, size, base_type in schema
                        ],
                    }
                    for schema, schema_count in representatives
                ],
            }
        )

    selected_decoded = {}
    for key in (
        "file_id_mesgs",
        "file_creator_mesgs",
        "device_info_mesgs",
        "device_settings_mesgs",
        "user_profile_mesgs",
        "sport_mesgs",
        "training_settings_mesgs",
        "zones_target_mesgs",
        "hr_zone_mesgs",
        "power_zone_mesgs",
        "lap_mesgs",
        "time_in_zone_mesgs",
        "session_mesgs",
        "activity_mesgs",
    ):
        if key in decoded:
            selected_decoded[key] = decoded[key]

    unknown_samples = {}
    for key, values in decoded.items():
        if key.isdigit():
            unknown_samples[key] = {
                "count": len(values),
                "first": values[0] if values else None,
                "last": values[-1] if values else None,
            }

    return {
        "label": source.label,
        "source": str(source.source),
        "container": str(source.container) if source.container else None,
        "sha256": _sha256(source.data),
        "size": len(source.data),
        "header_size": len(header),
        "header_profile_version": struct.unpack_from("<H", header, 2)[0],
        "crc": "valid",
        "sdk_errors": sdk_errors,
        "message_count": len(raw_messages),
        "message_types": message_types,
        "message_order_rle": _rle(order or [message.global_message for message in raw_messages]),
        "definition_count": len(definitions),
        "decoded_selected": selected_decoded,
        "unknown_message_samples": unknown_samples,
    }


def analyze_robert_gpx(archive: Path) -> list[dict[str, Any]]:
    reports = []
    with zipfile.ZipFile(archive) as source:
        for name in sorted(source.namelist()):
            if not name.lower().endswith(".gpx"):
                continue
            data = source.read(name)
            root = ElementTree.fromstring(data)
            local_names = Counter(element.tag.rsplit("}", 1)[-1] for element in root.iter())
            reports.append(
                {
                    "file": name,
                    "sha256": _sha256(data),
                    "size": len(data),
                    "trackpoints": local_names["trkpt"],
                    "heart_rate_samples": local_names["hr"],
                    "cadence_samples": local_names["cad"],
                    "power_samples": local_names["power"] + local_names["watts"],
                    "temperature_samples": local_names["atemp"],
                    "note": "GPX cannot expose Garmin FIT message structure or proprietary FIT fields.",
                }
            )
    return reports


def _messages_of(messages: Sequence[Message], number: int) -> list[Message]:
    return [message for message in messages if message.global_message == number]


def _one(messages: Sequence[Message], number: int) -> Message:
    found = _messages_of(messages, number)
    if len(found) != 1:
        raise ValueError(f"expected one message {number}, found {len(found)}")
    return found[0]


def _set_fields(message: Message, replacements: dict[int, RawField]) -> Message:
    output = []
    used = set()
    for field in message.fields:
        if field.number in replacements and field.number not in used:
            output.append(replacements[field.number])
            used.add(field.number)
        elif field.number not in replacements:
            output.append(field)
    output.extend(
        field for number, field in replacements.items() if number not in used
    )
    return replace(message, fields=tuple(output), developer_fields=())


def _set_uint_fields(message: Message, values: dict[int, tuple[int, int]]) -> Message:
    return _set_fields(
        message,
        {
            number: _pack_field("<", number, base_type, value)
            for number, (base_type, value) in values.items()
            if value is not None
        },
    )


def _subset(message: Message, field_numbers: set[int]) -> Message:
    return replace(
        _dedupe(message),
        fields=tuple(
            field for field in message.fields if field.number in field_numbers
        ),
        developer_fields=(),
    )


def _invalid_data(field: RawField) -> bytes:
    kind = field.base_type & 0x1F
    element_size = {
        0: 1,
        1: 1,
        2: 1,
        3: 2,
        4: 2,
        5: 4,
        6: 4,
        7: 1,
        8: 4,
        9: 8,
        10: 1,
        11: 2,
        12: 4,
        13: 1,
        14: 8,
        15: 8,
        16: 8,
    }.get(kind, 1)
    invalid = {
        0: b"\xff",
        1: b"\x7f",
        2: b"\xff",
        3: b"\xff\x7f",
        4: b"\xff\xff",
        5: b"\xff\xff\xff\x7f",
        6: b"\xff\xff\xff\xff",
        7: b"\x00",
        8: b"\xff\xff\xff\xff",
        9: b"\xff" * 8,
        10: b"\x00",
        11: b"\x00\x00",
        12: b"\x00\x00\x00\x00",
        13: b"\xff",
        14: b"\xff" * 7 + b"\x7f",
        15: b"\xff" * 8,
        16: b"\x00" * 8,
    }.get(kind, b"\xff")
    return (invalid * math.ceil(field.size / element_size))[: field.size]


def _invalid_field(field: RawField) -> RawField:
    return replace(field, data=_invalid_data(field))


def _reschema(
    template: Message,
    source: Message,
    *,
    allowed_source_fields: Optional[set[int]] = None,
    append_source_fields: bool = True,
) -> Message:
    source_fields = {
        field.number: field
        for field in _dedupe(source).fields
        if allowed_source_fields is None or field.number in allowed_source_fields
    }
    fields = []
    used = set()
    for target in _dedupe(template).fields:
        source_field = source_fields.get(target.number)
        if (
            source_field is not None
            and source_field.size == target.size
            and (source_field.base_type & 0x1F) == (target.base_type & 0x1F)
        ):
            fields.append(replace(source_field, base_type=target.base_type))
            used.add(target.number)
        else:
            fields.append(_invalid_field(target))
    if append_source_fields:
        fields.extend(field for number, field in source_fields.items() if number not in used)
    return Message(template.global_message, "<", tuple(fields))


def _array_field(number: int, base_type: int, values: Sequence[int]) -> RawField:
    kind = base_type & 0x1F
    if kind in (TYPE_ENUM, TYPE_UINT8):
        data = bytes(values)
    elif kind == (TYPE_UINT16 & 0x1F):
        data = struct.pack("<" + "H" * len(values), *values)
    elif kind == (TYPE_UINT32 & 0x1F):
        data = struct.pack("<" + "I" * len(values), *values)
    else:
        raise ValueError(f"unsupported array base type {base_type:#x}")
    return RawField(number, len(data), base_type, data)


def _unpack_uint_array(message: Message, number: int) -> list[int]:
    field = message.first(number)
    if field is None:
        return []
    kind = field.base_type & 0x1F
    if kind in (TYPE_ENUM, TYPE_UINT8):
        return list(field.data)
    if kind == (TYPE_UINT16 & 0x1F):
        return list(struct.unpack(message.endian + "H" * (field.size // 2), field.data))
    if kind == (TYPE_UINT32 & 0x1F):
        return list(struct.unpack(message.endian + "I" * (field.size // 4), field.data))
    return []


def _valid_record_values(records: Sequence[Message], number: int) -> list[int]:
    return [
        value
        for record in records
        if (value := _read_uint(record, number)) is not None
    ]


def calculate_metrics(messages: Sequence[Message]) -> RideMetrics:
    records = _messages_of(messages, MSG_RECORD)
    session = _one(messages, MSG_SESSION)
    timestamps = _valid_record_values(records, F_TIMESTAMP)
    if len(timestamps) != len(records) or not records:
        raise ValueError("records need valid timestamps")
    intervals = [
        current - previous
        for previous, current in zip(timestamps, timestamps[1:])
        if 0 < current - previous <= 60
    ]
    final_interval = round(statistics.median(intervals)) if intervals else 1
    start = timestamps[0]
    end = timestamps[-1] + final_interval
    elapsed_ms = (end - start) * 1000
    timer_ms = elapsed_ms

    distances = _valid_record_values(records, 5)
    speeds = _valid_record_values(records, 6)
    heart_rates = _valid_record_values(records, 3)
    cadences = _valid_record_values(records, 4)
    powers = _valid_record_values(records, 7)
    ascent, descent = _vertical_totals(records)
    work = _timed_record_total(records, 7, end)
    cadence_total = _timed_record_total(records, 4, end)
    complete_one_hz_power = len(powers) == len(records) and all(
        current - previous == 1
        for previous, current in zip(timestamps, timestamps[1:])
    )
    distance_raw = distances[-1] if distances else None
    return RideMetrics(
        start=start,
        end=end,
        elapsed_ms=elapsed_ms,
        timer_ms=timer_ms,
        distance_raw=distance_raw,
        cycles=round(cadence_total / 60) if cadence_total is not None else None,
        calories=_read_uint(session, 11),
        ascent=ascent,
        descent=descent,
        total_work=work,
        normalized_power=_normalized_power(powers) if complete_one_hz_power else None,
        avg_speed_raw=(
            round(distance_raw * 10_000 / timer_ms)
            if distance_raw is not None and timer_ms
            else None
        ),
        max_speed_raw=max(speeds) if speeds else None,
        avg_hr=round(sum(heart_rates) / len(heart_rates)) if heart_rates else None,
        max_hr=max(heart_rates) if heart_rates else None,
        avg_cadence=round(sum(cadences) / len(cadences)) if cadences else None,
        max_cadence=max(cadences) if cadences else None,
        avg_power=round(sum(powers) / len(powers)) if powers else None,
        max_power=max(powers) if powers else None,
    )


def _public_summary_values(metrics: RideMetrics, lap: bool) -> dict[int, tuple[int, int]]:
    common = {
        253: (TYPE_UINT32, metrics.end),
        2: (TYPE_UINT32, metrics.start),
        7: (TYPE_UINT32, metrics.elapsed_ms),
        8: (TYPE_UINT32, metrics.timer_ms),
        9: (TYPE_UINT32, metrics.distance_raw),
        10: (TYPE_UINT32, metrics.cycles),
        11: (TYPE_UINT16, metrics.calories),
    }
    if lap:
        common.update(
            {
                13: (TYPE_UINT16, metrics.avg_speed_raw),
                14: (TYPE_UINT16, metrics.max_speed_raw),
                15: (TYPE_UINT8, metrics.avg_hr),
                16: (TYPE_UINT8, metrics.max_hr),
                17: (TYPE_UINT8, metrics.avg_cadence),
                18: (TYPE_UINT8, metrics.max_cadence),
                19: (TYPE_UINT16, metrics.avg_power),
                20: (TYPE_UINT16, metrics.max_power),
                21: (TYPE_UINT16, metrics.ascent),
                22: (TYPE_UINT16, metrics.descent),
                33: (TYPE_UINT16, metrics.normalized_power),
                41: (TYPE_UINT32, metrics.total_work),
            }
        )
    else:
        common.update(
            {
                14: (TYPE_UINT16, metrics.avg_speed_raw),
                15: (TYPE_UINT16, metrics.max_speed_raw),
                16: (TYPE_UINT8, metrics.avg_hr),
                17: (TYPE_UINT8, metrics.max_hr),
                18: (TYPE_UINT8, metrics.avg_cadence),
                19: (TYPE_UINT8, metrics.max_cadence),
                20: (TYPE_UINT16, metrics.avg_power),
                21: (TYPE_UINT16, metrics.max_power),
                22: (TYPE_UINT16, metrics.ascent),
                23: (TYPE_UINT16, metrics.descent),
                34: (TYPE_UINT16, metrics.normalized_power),
                48: (TYPE_UINT32, metrics.total_work),
            }
        )
    return {number: value for number, value in common.items() if value[1] is not None}


def recalculate_core(messages: Sequence[Message]) -> tuple[list[Message], RideMetrics]:
    metrics = calculate_metrics(messages)
    output = []
    for message in messages:
        if message.global_message == MSG_LAP:
            output.append(_set_uint_fields(message, _public_summary_values(metrics, True)))
        elif message.global_message == MSG_SESSION:
            output.append(_set_uint_fields(message, _public_summary_values(metrics, False)))
        elif message.global_message == MSG_ACTIVITY:
            output.append(
                _set_uint_fields(
                    message,
                    {
                        253: (TYPE_UINT32, metrics.end),
                        0: (TYPE_UINT32, metrics.timer_ms),
                        5: (TYPE_UINT32, _local_timestamp(metrics.end)),
                    },
                )
            )
        elif message.global_message == MSG_EVENT:
            event_type = _read_uint(message, 1)
            output.append(
                _set_uint_fields(
                    message,
                    {
                        253: (
                            TYPE_UINT32,
                            metrics.start if event_type == 0 else metrics.end,
                        )
                    },
                )
            )
        elif message.global_message == MSG_DEVICE_INFO:
            timestamp = _read_uint(message, F_TIMESTAMP)
            output.append(
                _set_uint_fields(
                    message,
                    {
                        253: (
                            TYPE_UINT32,
                            metrics.start
                            if timestamp is None or timestamp <= metrics.start + 60
                            else metrics.end,
                        )
                    },
                )
            )
        else:
            output.append(message)
    return output, metrics


def _most_common_template(messages: Sequence[Message], number: int) -> Message:
    found = _messages_of(messages, number)
    if not found:
        raise ValueError(f"donor has no message {number}")
    schemas = Counter(
        tuple((field.number, field.size, field.base_type) for field in message.fields)
        for message in found
    )
    schema = schemas.most_common(1)[0][0]
    return next(
        message
        for message in found
        if tuple((field.number, field.size, field.base_type) for field in message.fields)
        == schema
    )


def _shift_donor_device_info(
    donor_messages: Sequence[Message], donor_start: int, metrics: RideMetrics
) -> tuple[list[Message], list[Message]]:
    start_messages = []
    end_messages = []
    for message in _messages_of(donor_messages, MSG_DEVICE_INFO):
        timestamp = _read_uint(message, F_TIMESTAMP)
        is_start = timestamp is None or timestamp <= donor_start + 60
        shifted = _set_uint_fields(
            _dedupe(message),
            {253: (TYPE_UINT32, metrics.start if is_start else metrics.end)},
        )
        (start_messages if is_start else end_messages).append(shifted)
    return start_messages, end_messages


def _hr_boundaries(donor_messages: Sequence[Message]) -> list[int]:
    session_zones = [
        message
        for message in _messages_of(donor_messages, MSG_TIME_IN_ZONE)
        if _read_uint(message, 0) == MSG_SESSION
    ]
    if session_zones:
        return _unpack_uint_array(session_zones[-1], 6)
    return []


def _time_in_zones(
    records: Sequence[Message], boundaries: Sequence[int], end: int, field_number: int
) -> list[int]:
    totals = [0] * (len(boundaries) + 1)
    for index, record in enumerate(records):
        timestamp = _read_uint(record, F_TIMESTAMP)
        value = _read_uint(record, field_number)
        if timestamp is None or value is None:
            continue
        next_timestamp = (
            _read_uint(records[index + 1], F_TIMESTAMP)
            if index + 1 < len(records)
            else end
        )
        if next_timestamp is None:
            continue
        interval = next_timestamp - timestamp
        if 0 < interval <= 60:
            totals[bisect_right(boundaries, value)] += interval * 1000
    return totals


def _make_time_in_zone(
    template: Message,
    reference_message: int,
    metrics: RideMetrics,
    records: Sequence[Message],
    boundaries: Sequence[int],
    *,
    power_boundaries: Optional[Sequence[int]] = None,
    profile_config: Optional[ProfileConfig] = None,
) -> Message:
    replacements = {
        253: _pack_field("<", 253, TYPE_UINT32, metrics.end),
        0: _pack_field("<", 0, TYPE_UINT16, reference_message),
        1: _pack_field("<", 1, TYPE_UINT16, 0),
        2: _array_field(2, TYPE_UINT32, _time_in_zones(records, boundaries, metrics.end, 3)),
        6: _array_field(6, TYPE_UINT8, boundaries),
    }
    if power_boundaries:
        replacements[5] = _array_field(
            5, TYPE_UINT32, _time_in_zones(records, power_boundaries, metrics.end, 7)
        )
        replacements[9] = _array_field(9, TYPE_UINT16, power_boundaries)
    if profile_config is not None:
        if profile_config.max_hr is not None:
            replacements[11] = _pack_field("<", 11, TYPE_UINT8, profile_config.max_hr)
        if profile_config.resting_hr is not None:
            replacements[12] = _pack_field("<", 12, TYPE_UINT8, profile_config.resting_hr)
        if profile_config.ftp is not None:
            replacements[15] = _pack_field("<", 15, TYPE_UINT16, profile_config.ftp)
    return _set_fields(_dedupe(template), replacements)


def _donor_evidence(source: FitInput) -> DonorEvidence:
    decoded, errors, _, _ = _sdk_decode(source.data)
    if errors:
        raise ValueError(f"SDK decode failed for {source.label}: {errors}")
    session = decoded.get("session_mesgs", [{}])[0]
    zone_messages = decoded.get("time_in_zone_mesgs", [])
    session_zone = next(
        (
            message
            for message in reversed(zone_messages)
            if message.get("reference_mesg") == "session"
        ),
        {},
    )
    zone_times = session_zone.get("time_in_hr_zone") or []
    zone_total = sum(value for value in zone_times if value is not None)
    fractions = tuple(
        (value or 0) / zone_total if zone_total else 0 for value in zone_times
    )
    timestamp = session.get("timestamp")
    if isinstance(timestamp, datetime):
        timestamp_value = round(
            (timestamp - FIT_EPOCH).total_seconds()
        )
    else:
        timestamp_value = timestamp
    return DonorEvidence(
        label=source.label,
        timestamp=timestamp_value,
        duration_s=session.get("total_timer_time"),
        avg_hr=session.get("avg_heart_rate"),
        max_hr=session.get("max_heart_rate"),
        hr_zone_fractions=fractions,
        aerobic_te=session.get("total_training_effect"),
        anaerobic_te=session.get("total_anaerobic_training_effect"),
        training_load=session.get("training_load_peak"),
    )


def select_donor(inputs: Sequence[FitInput]) -> tuple[FitInput, list[dict[str, Any]]]:
    ranked = []
    for source in inputs:
        decoded, errors, _, _ = _sdk_decode(source.data)
        session = decoded.get("session_mesgs", [{}])[0]
        score = 0
        reasons = []
        if not errors:
            score += 100
            reasons.append("clean official SDK decode")
        if session.get("sport") == "cycling":
            score += 30
            reasons.append("cycling session")
        if decoded.get("user_profile_mesgs"):
            score += 20
            reasons.append("user profile")
        if decoded.get("zones_target_mesgs"):
            score += 20
            reasons.append("zone targets")
        if decoded.get("time_in_zone_mesgs"):
            score += 20
            reasons.append("time-in-zone structure")
        if session.get("avg_heart_rate") is not None:
            score += 10
            reasons.append("heart-rate summary")
        timestamp = session.get("timestamp")
        timestamp_sort = timestamp.timestamp() if isinstance(timestamp, datetime) else 0
        ranked.append(
            {
                "source": source,
                "score": score,
                "timestamp_sort": timestamp_sort,
                "reasons": reasons,
            }
        )
    ranked.sort(key=lambda item: (item["score"], item["timestamp_sort"]), reverse=True)
    public_ranking = [
        {
            "label": item["source"].label,
            "score": item["score"],
            "reasons": item["reasons"],
            "recency_tiebreaker": datetime.fromtimestamp(
                item["timestamp_sort"], timezone.utc
            ).isoformat()
            if item["timestamp_sort"]
            else None,
        }
        for item in ranked
    ]
    return ranked[0]["source"], public_ranking


def _infer_metrics(
    evidence: Sequence[DonorEvidence],
    metrics: RideMetrics,
    hr_zone_times_ms: Sequence[int],
    max_hr: Optional[int],
) -> dict[str, Any]:
    usable = [
        item
        for item in evidence
        if item.aerobic_te is not None
        and item.anaerobic_te is not None
        and item.training_load is not None
        and item.avg_hr is not None
        and item.max_hr is not None
        and item.duration_s
    ]
    if not usable or metrics.avg_hr is None or metrics.max_hr is None or not max_hr:
        return {"written": False, "reason": "insufficient HR/profile donor evidence"}
    zone_total = sum(hr_zone_times_ms)
    fractions = tuple(value / zone_total if zone_total else 0 for value in hr_zone_times_ms)
    distances = []
    for item in usable:
        zone_count = max(len(fractions), len(item.hr_zone_fractions))
        zone_distance = sum(
            abs(
                (fractions[index] if index < len(fractions) else 0)
                - (
                    item.hr_zone_fractions[index]
                    if index < len(item.hr_zone_fractions)
                    else 0
                )
            )
            for index in range(zone_count)
        )
        distance = (
            abs(math.log((metrics.timer_ms / 1000) / item.duration_s))
            + 3 * abs(metrics.avg_hr / max_hr - item.avg_hr / max_hr)
            + abs(metrics.max_hr / max_hr - item.max_hr / max_hr)
            + 2 * zone_distance
        )
        distances.append((max(distance, 0.05), item))
    distances.sort(key=lambda pair: pair[0])
    neighbors = distances[: min(3, len(distances))]
    weights = [1 / distance for distance, _item in neighbors]

    def weighted(attribute: str) -> float:
        return sum(
            weight * float(getattr(item, attribute))
            for weight, (_distance, item) in zip(weights, neighbors)
        ) / sum(weights)

    return {
        "written": True,
        "method": "inverse-distance weighted nearest donors using duration, avg/max HR, and HR-zone distribution",
        "aerobic_te": round(weighted("aerobic_te"), 1),
        "anaerobic_te": round(weighted("anaerobic_te"), 1),
        "training_load": round(weighted("training_load"), 3),
        "neighbors": [
            {"label": item.label, "distance": round(distance, 5)}
            for distance, item in neighbors
        ],
        "confidence": "low; empirical only; upload behavior unproven",
    }


def _apply_inferred_metrics(session: Message, inferred: dict[str, Any]) -> Message:
    if not inferred.get("written"):
        return session
    replacements = {
        24: _pack_field("<", 24, TYPE_UINT8, round(inferred["aerobic_te"] * 10)),
        137: _pack_field(
            "<", 137, TYPE_UINT8, round(inferred["anaerobic_te"] * 10)
        ),
        168: _pack_field(
            "<", 168, TYPE_SINT32, round(inferred["training_load"] * 65536)
        ),
    }
    return _set_fields(session, replacements)


def _fit_timestamp(message: Message, number: int = F_TIMESTAMP) -> Optional[int]:
    return _read_uint(message, number)


def _override_zones_target(message: Message, config: ProfileConfig) -> Message:
    overrides: dict[int, tuple[int, int]] = {}
    if config.max_hr is not None:
        overrides[1] = (TYPE_UINT8, config.max_hr)
    if config.ftp is not None:
        overrides[3] = (TYPE_UINT16, config.ftp)
    return _set_uint_fields(_dedupe(message), overrides) if overrides else _dedupe(message)


def _override_user_profile(message: Message, config: ProfileConfig) -> Message:
    overrides: dict[int, tuple[int, int]] = {}
    if config.max_hr is not None:
        overrides[10] = (TYPE_UINT8, config.max_hr)
        overrides[11] = (TYPE_UINT8, config.max_hr)
    if config.resting_hr is not None:
        overrides[8] = (TYPE_UINT8, config.resting_hr)
    if config.weight_kg is not None:
        overrides[4] = (TYPE_UINT16, round(config.weight_kg * 10))
    return _set_uint_fields(_dedupe(message), overrides) if overrides else _dedupe(message)


def _build_hr_zone_messages(boundaries: Sequence[int]) -> list[Message]:
    messages: list[Message] = []
    for index, high_bpm in enumerate(boundaries):
        messages.append(
            Message(
                MSG_HR_ZONE,
                "<",
                (
                    _pack_field("<", F_MESSAGE_INDEX, TYPE_UINT16, index),
                    _pack_field("<", 1, TYPE_UINT8, high_bpm),
                ),
            )
        )
    return messages


def _build_power_zone_messages(boundaries: Sequence[int]) -> list[Message]:
    messages: list[Message] = []
    for index, high_value in enumerate(boundaries):
        messages.append(
            Message(
                MSG_POWER_ZONE,
                "<",
                (
                    _pack_field("<", F_MESSAGE_INDEX, TYPE_UINT16, index),
                    _pack_field("<", 1, TYPE_UINT16, high_value),
                ),
            )
        )
    return messages


def build_variants(
    mywhoosh: FitInput,
    donor: FitInput,
    all_donors: Sequence[FitInput],
    profile_config: Optional[ProfileConfig] = None,
) -> tuple[dict[str, bytes], dict[str, Any]]:
    normalized_data = convert_fit_bytes(mywhoosh.data)
    _normalized_header, normalized_messages = _parse_fit(normalized_data)
    normalized_messages, metrics = recalculate_core(normalized_messages)
    donor_header, donor_messages = _parse_fit(donor.data)

    donor_session = _one(donor_messages, MSG_SESSION)
    donor_start = _read_uint(donor_session, 2)
    if donor_start is None:
        raise ValueError("selected donor has no session start time")
    start_devices, end_devices = _shift_donor_device_info(
        donor_messages, donor_start, metrics
    )

    donor_file_id = _set_uint_fields(
        _dedupe(_one(donor_messages, MSG_FILE_ID)),
        {4: (TYPE_UINT32, metrics.start)},
    )
    donor_creator = _messages_of(donor_messages, MSG_FILE_CREATOR)
    source_events = _messages_of(normalized_messages, MSG_EVENT)
    source_sport = _one(normalized_messages, MSG_SPORT)
    source_records = _messages_of(normalized_messages, MSG_RECORD)
    source_lap = _one(normalized_messages, MSG_LAP)
    source_session = _one(normalized_messages, MSG_SESSION)
    source_activity = _one(normalized_messages, MSG_ACTIVITY)

    donor_record_template = _most_common_template(donor_messages, MSG_RECORD)
    donor_lap_template = _most_common_template(donor_messages, MSG_LAP)
    donor_session_template = _one(donor_messages, MSG_SESSION)
    donor_activity_template = _one(donor_messages, MSG_ACTIVITY)
    donor_time_in_zone = _messages_of(donor_messages, MSG_TIME_IN_ZONE)
    time_in_zone_template = (
        donor_time_in_zone[-1] if donor_time_in_zone else None
    )

    records = [
        _reschema(donor_record_template, record, append_source_fields=True)
        for record in source_records
    ]
    lap = _reschema(
        donor_lap_template,
        source_lap,
        allowed_source_fields=PUBLIC_LAP_FIELDS,
        append_source_fields=True,
    )
    session = _reschema(
        donor_session_template,
        source_session,
        allowed_source_fields=PUBLIC_SESSION_FIELDS,
        append_source_fields=True,
    )
    activity = _reschema(donor_activity_template, source_activity, append_source_fields=True)

    device_settings = [
        _dedupe(message)
        for message in _messages_of(donor_messages, MSG_DEVICE_SETTINGS)
    ]
    training_settings = [
        _dedupe(message)
        for message in _messages_of(donor_messages, MSG_TRAINING_SETTINGS)
    ]
    user_profile = _messages_of(donor_messages, MSG_USER_PROFILE)
    zones_target = _messages_of(donor_messages, MSG_ZONES_TARGET)
    hr_zones = [
        _dedupe(message) for message in _messages_of(donor_messages, MSG_HR_ZONE)
    ]
    power_zones = [
        _dedupe(message) for message in _messages_of(donor_messages, MSG_POWER_ZONE)
    ]

    structural_prefix_before_profile = (
        [donor_file_id]
        + [_dedupe(message) for message in donor_creator]
        + source_events[:1]
        + start_devices
        + device_settings
    )
    hr_profile_messages = (
        [_subset(user_profile[0], HR_PROFILE_FIELDS)] if user_profile else []
    )
    hr_target_messages = (
        [_subset(zones_target[0], HR_TARGET_FIELDS)] if zones_target else []
    )
    power_profile_messages = (
        [_subset(user_profile[0], POWER_PROFILE_FIELDS)] if user_profile else []
    )
    power_target_messages = (
        [_subset(zones_target[0], POWER_TARGET_FIELDS)] if zones_target else []
    )
    boundaries = _hr_boundaries(donor_messages)
    time_in_zone_messages = []
    hr_zone_times = []
    if time_in_zone_template is not None and boundaries:
        hr_zone_times = _time_in_zones(records, boundaries, metrics.end, 3)
        time_in_zone_messages = [
            _make_time_in_zone(
                time_in_zone_template, MSG_LAP, metrics, records, boundaries
            ),
            _make_time_in_zone(
                time_in_zone_template, MSG_SESSION, metrics, records, boundaries
            ),
        ]

    evidence = [_donor_evidence(source) for source in all_donors]
    max_hr = boundaries[-1] if boundaries else metrics.max_hr
    inferred = _infer_metrics(evidence, metrics, hr_zone_times, max_hr)
    inferred_session = _apply_inferred_metrics(session, inferred)

    # Profile/zones override scaffolding for the injected variant.
    config = profile_config
    if config is not None and not config.is_empty:
        injected_zones_target = (
            [_override_zones_target(zones_target[0], config)] if zones_target else []
        )
        injected_user_profile = (
            [_override_user_profile(user_profile[0], config)] if user_profile else []
        )
        injected_power_profile_messages = (
            [_subset(injected_user_profile[0], POWER_PROFILE_FIELDS)]
            if injected_user_profile
            else []
        )
        injected_power_target_messages = (
            [_subset(injected_zones_target[0], POWER_TARGET_FIELDS)]
            if injected_zones_target
            else []
        )
        injected_hr_zones = (
            _build_hr_zone_messages(config.hr_zones)
            if config.hr_zones is not None
            else hr_zones
        )
        injected_power_zones = (
            _build_power_zone_messages(config.power_zones)
            if config.power_zones is not None
            else power_zones
        )
        profile_overrides_applied = config.as_dict()
    else:
        injected_power_profile_messages = power_profile_messages
        injected_power_target_messages = power_target_messages
        injected_hr_zones = hr_zones
        injected_power_zones = power_zones
        profile_overrides_applied = None

    injected_sub_sport = (
        config.sub_sport
        if config is not None and config.sub_sport is not None
        else INJECTED_DEFAULT_SUB_SPORT
    )
    injected_sport = _set_uint_fields(
        _dedupe(source_sport), {1: (TYPE_ENUM, injected_sub_sport)}
    )
    injected_session_base = (
        inferred_session if inferred.get("written") else session
    )
    injected_session = _set_uint_fields(
        injected_session_base, {6: (TYPE_ENUM, injected_sub_sport)}
    )

    injected_hr_boundaries = (
        list(config.hr_zones)
        if config is not None and config.hr_zones is not None
        else list(boundaries)
    )
    injected_power_boundaries = (
        list(config.power_zones)
        if config is not None and config.power_zones is not None
        else None
    )
    injected_hr_zone_times_ms: list[int] = []
    injected_power_zone_times_ms: list[int] = []
    injected_time_in_zone_messages: list[Message] = []
    if time_in_zone_template is not None and injected_hr_boundaries:
        injected_hr_zone_times_ms = _time_in_zones(
            records, injected_hr_boundaries, metrics.end, 3
        )
        if injected_power_boundaries:
            injected_power_zone_times_ms = _time_in_zones(
                records, injected_power_boundaries, metrics.end, 7
            )
        injected_time_in_zone_messages = [
            _make_time_in_zone(
                time_in_zone_template,
                MSG_LAP,
                metrics,
                records,
                injected_hr_boundaries,
                power_boundaries=injected_power_boundaries,
                profile_config=config,
            ),
            _make_time_in_zone(
                time_in_zone_template,
                MSG_SESSION,
                metrics,
                records,
                injected_hr_boundaries,
                power_boundaries=injected_power_boundaries,
                profile_config=config,
            ),
        ]

    def assemble(
        extra_profile: Sequence[Message],
        *,
        include_time_in_zone: bool = False,
        reverse_engineered_session: Optional[Message] = None,
        time_in_zone_override: Optional[Sequence[Message]] = None,
        sport_override: Optional[Message] = None,
    ) -> bytes:
        active_tiz = (
            list(time_in_zone_override)
            if time_in_zone_override is not None
            else time_in_zone_messages
        )
        sport_message = sport_override if sport_override is not None else source_sport
        prefix_after_profile = [sport_message] + training_settings
        tail = source_events[1:] + end_devices + [lap]
        if include_time_in_zone and active_tiz:
            tail.append(active_tiz[0])
        tail.append(reverse_engineered_session or session)
        if include_time_in_zone and active_tiz:
            tail.append(active_tiz[1])
        tail.append(activity)
        output_messages = (
            structural_prefix_before_profile
            + list(extra_profile)
            + prefix_after_profile
            + records
            + tail
        )
        data = _encode_fit(donor_header, output_messages)
        _parse_fit(data)
        return data

    variants = {
        "structural_only": assemble([]),
        "structural_plus_hr_zones": assemble(hr_profile_messages + hr_target_messages + hr_zones),
        "structural_plus_power_zones": assemble(
            power_profile_messages
            + power_target_messages
            + hr_zones
            + power_zones
        ),
        "structural_plus_time_in_zone": assemble(
            power_profile_messages
            + power_target_messages
            + hr_zones
            + power_zones,
            include_time_in_zone=True,
        ),
        "reverse_engineered_metrics_attempt": assemble(
            power_profile_messages
            + power_target_messages
            + hr_zones
            + power_zones,
            include_time_in_zone=True,
            reverse_engineered_session=inferred_session,
        ),
        "07_profile_zones_injected": assemble(
            injected_power_profile_messages
            + injected_power_target_messages
            + injected_hr_zones
            + injected_power_zones,
            include_time_in_zone=True,
            reverse_engineered_session=injected_session,
            time_in_zone_override=injected_time_in_zone_messages or None,
            sport_override=injected_sport,
        ),
    }
    notes = {
        "metrics": metrics.__dict__,
        "hr_boundaries": boundaries,
        "hr_time_in_zone_ms": hr_zone_times,
        "explicit_power_zone_messages": len(power_zones),
        "power_time_in_zone_written": False,
        "power_time_in_zone_reason": (
            "No explicit power_zone boundary messages exist in Klemen donor FITs."
        ),
        "reverse_engineered_metrics_attempt": inferred,
        "profile_zones_injected": {
            "label": "experimental",
            "profile_overrides_applied": profile_overrides_applied,
            "hr_boundaries_used": injected_hr_boundaries,
            "power_boundaries_used": injected_power_boundaries,
            "hr_zone_times_ms": injected_hr_zone_times_ms,
            "power_zone_times_ms": injected_power_zone_times_ms,
            "sub_sport_used": injected_sub_sport,
            "sub_sport_default": INJECTED_DEFAULT_SUB_SPORT,
            "note": (
                "Uses donor-templated structure with user-supplied max_hr/FTP/zones "
                "overrides where provided. time_in_zone messages are recomputed against "
                "the user-supplied boundaries (and power boundaries if present). "
                "sub_sport is forced to indoor_cycling (7) instead of virtual_activity "
                "(58) so the watch's Training Effect / Training Status engine treats "
                "the ride like a Garmin-native indoor ride. TE/load values, when "
                "written, come from the reverse-engineered estimator and remain "
                "experimental until upload-tested."
            ),
        },
    }
    return variants, notes


def validate_fit(data: bytes) -> dict[str, Any]:
    result: dict[str, Any] = {}
    try:
        header, messages = _parse_fit(data)
        result["internal_decode"] = "pass"
        result["crc"] = "pass"
        result["message_count"] = len(messages)
        result["file_crc_recomputed"] = fit_crc(data[:-2])
        result["file_crc_stored"] = struct.unpack_from("<H", data, len(data) - 2)[0]
        result["header_profile_version"] = struct.unpack_from("<H", header, 2)[0]
        session = _one(messages, MSG_SESSION)
        lap = _one(messages, MSG_LAP)
        activity = _one(messages, MSG_ACTIVITY)
        records = _messages_of(messages, MSG_RECORD)
        record_timestamps = [_read_uint(record, F_TIMESTAMP) for record in records]
        monotonic = all(
            previous is not None and current is not None and current > previous
            for previous, current in zip(record_timestamps, record_timestamps[1:])
        )
        start = _read_uint(session, 2)
        end = _read_uint(session, F_TIMESTAMP)
        consistency = {
            "record_timestamps_strictly_increasing": monotonic,
            "session_lap_activity_end_match": len(
                {
                    _read_uint(session, F_TIMESTAMP),
                    _read_uint(lap, F_TIMESTAMP),
                    _read_uint(activity, F_TIMESTAMP),
                }
            )
            == 1,
            "session_lap_start_match": _read_uint(session, 2) == _read_uint(lap, 2),
            "session_lap_timer_match": _read_uint(session, 8) == _read_uint(lap, 8),
            "records_inside_summary_window": bool(record_timestamps)
            and start is not None
            and end is not None
            and record_timestamps[0] is not None
            and record_timestamps[-1] is not None
            and start <= record_timestamps[0] <= record_timestamps[-1] <= end,
        }
        result["consistency"] = consistency
        result["consistency_result"] = (
            "pass" if all(consistency.values()) else "fail"
        )
    except Exception as error:
        result["internal_decode"] = "fail"
        result["crc"] = "fail"
        result["internal_error"] = f"{type(error).__name__}: {error}"

    decoded, errors, _order, _definitions = _sdk_decode(data)
    result["garmin_fit_sdk"] = "pass" if not errors else "fail"
    result["garmin_fit_sdk_errors"] = errors
    result["garmin_fit_sdk_message_types"] = {
        key: len(values) for key, values in decoded.items()
    }
    return result


def _session_summary(data: bytes) -> dict[str, Any]:
    decoded, errors, _order, _definitions = _sdk_decode(data)
    session = decoded.get("session_mesgs", [{}])[0]
    activity = decoded.get("activity_mesgs", [{}])[0]
    return {
        "errors": errors,
        "session": {
            key: session.get(key)
            for key in (
                "timestamp",
                "start_time",
                "sport",
                "sub_sport",
                "total_elapsed_time",
                "total_timer_time",
                "total_distance",
                "total_calories",
                "avg_heart_rate",
                "max_heart_rate",
                "avg_cadence",
                "max_cadence",
                "avg_power",
                "max_power",
                "normalized_power",
                "total_work",
                "total_ascent",
                "total_descent",
                "total_training_effect",
                "total_anaerobic_training_effect",
                "training_load_peak",
            )
        },
        "activity": activity,
        "message_counts": {key: len(value) for key, value in decoded.items()},
    }


def _metric_table(analyses: Sequence[dict[str, Any]]) -> str:
    rows = [
        "| FIT | Date | Timer s | Distance km | Avg/Max HR | Aerobic/An. TE | Load | FTP |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for analysis in analyses:
        decoded = analysis["decoded_selected"]
        session = (decoded.get("session_mesgs") or [{}])[0]
        target = (decoded.get("zones_target_mesgs") or [{}])[0]
        rows.append(
            "| {label} | {date} | {timer} | {distance} | {hr} | {te} | {load} | {ftp} |".format(
                label=analysis["label"],
                date=str(session.get("timestamp", ""))[:10],
                timer=session.get("total_timer_time", ""),
                distance=round((session.get("total_distance") or 0) / 1000, 2),
                hr=f"{session.get('avg_heart_rate', '-')}/{session.get('max_heart_rate', '-')}",
                te=f"{session.get('total_training_effect', '-')}/{session.get('total_anaerobic_training_effect', '-')}",
                load=round(session.get("training_load_peak"), 3)
                if session.get("training_load_peak") is not None
                else "-",
                ftp=target.get("functional_threshold_power", "-"),
            )
        )
    return "\n".join(rows)


def _write_reports(
    output_dir: Path,
    mywhoosh: FitInput,
    donor: FitInput,
    donor_ranking: Sequence[dict[str, Any]],
    klemen_analyses: Sequence[dict[str, Any]],
    robert_analysis: Sequence[dict[str, Any]],
    mywhoosh_analysis: dict[str, Any],
    variant_paths: dict[str, Path],
    validation: dict[str, Any],
    notes: dict[str, Any],
    hashes: dict[str, Any],
) -> None:
    reports = output_dir / "reports"
    _write_text(
        reports / "donor_analysis.md",
        f"""# Donor Analysis

Classification labels used throughout: **exact/public calculation**, **copied Garmin
structure**, **empirically inferred**, **unknown/proprietary**, and **unproven upload
behavior**.

## Klemen Garmin FITs

{_metric_table(klemen_analyses)}

- All six inputs pass the repository CRC/parser check and official Garmin FIT SDK decode.
- All contain Garmin user profile and cycling zone-target messages.
- None contains record power samples or explicit `power_zone` messages.
- FTP exists in `zones_target` (`142 W`), but explicit power-zone boundaries do not.
- Comprehensive per-file schemas, unknown fields, ordering, timestamps, profiles, zones,
  devices, laps, sessions, activities, and samples are in `outputs/analysis/*.json`.

## Robert Archive

- The supplied archive contains {len(robert_analysis)} GPX files and **zero FIT files**.
- GPX can expose trackpoints/HR/cadence but cannot expose Garmin FIT message ordering,
  developer fields, unknown FIT fields, Training Effect, Training Load, or Firstbeat state.
- Power samples found in the supplied GPX files: {sum(item['power_samples'] for item in robert_analysis)}.

See `outputs/analysis/robert_gpx_analysis.json` for per-file counts.
""",
    )
    ranking_lines = "\n".join(
        f"- `{item['label']}`: score {item['score']} ({', '.join(item['reasons'])})"
        for item in donor_ranking
    )
    _write_text(
        reports / "selected_donor.md",
        f"""# Selected Donor

Selected: `{donor.label}`

The donor is selected only from Klemen's Garmin FIT files. Selection prioritizes clean SDK
decode, cycling activity structure, user profile, zone targets, time-in-zone structure,
heart-rate summary, then recency as a tiebreaker.

{ranking_lines}

Robert's files are excluded from donor selection because they are GPX, and his personal
values must not be copied.
""",
    )
    _write_text(
        reports / "mywhoosh_analysis.md",
        f"""# MyWhoosh Analysis

Input: `{mywhoosh.source}`

- SHA-256: `{mywhoosh_analysis['sha256']}`
- Size: {mywhoosh_analysis['size']} bytes
- CRC/internal decode: pass
- Official Garmin FIT SDK errors: {mywhoosh_analysis['sdk_errors'] or 'none'}
- Message count: {mywhoosh_analysis['message_count']}
- Comprehensive schema/order/unknown/developer-field dump:
  `outputs/analysis/{mywhoosh.label}_analysis.json`

The baseline normalizer repairs the shifted `local_timestamp`, malformed summary
timestamps, events, and public summaries before donor templating.
""",
    )
    comparison = {
        "original_mywhoosh": _session_summary(mywhoosh.data),
        **{
            name: _session_summary(path.read_bytes())
            for name, path in variant_paths.items()
        },
    }
    _write_json(reports / "original_vs_converted_comparison.json", comparison)
    _write_text(
        reports / "original_vs_converted_comparison.md",
        """# Original vs Converted Comparison

The machine-readable comparison is in `original_vs_converted_comparison.json`.

- Public ride metrics are recalculated from MyWhoosh records where possible.
- Calories are retained from the MyWhoosh session because no exact public physiology-based
  calorie formula is available from these inputs.
- Garmin device/profile/zone values come only from Klemen's selected donor.
- Robert's profile, calibration, zones, and personal values are never copied.

| Output value group | Classification |
|---|---|
| elapsed/timer, distance, HR, power, NP, work, cadence, ascent/descent, summaries | exact/public calculation |
| calories | copied MyWhoosh summary; exact calculation unavailable |
| file/device/profile/zone schemas and values | copied Garmin structure from Klemen |
| attempted TE/anaerobic TE/load | empirically inferred, attempt variant only |
| Recovery, Load Focus, Training Status, undocumented messages | unknown/proprietary; not written |
| Garmin Connect/watch effects | unproven upload behavior |
""",
    )
    inferred = notes["reverse_engineered_metrics_attempt"]
    _write_text(
        reports / "reverse_engineering_findings.md",
        f"""# Reverse-Engineering Findings

## Stable Public/Profile Fields

| Metric | FIT location | Scaling | Classification |
|---|---|---:|---|
| Aerobic Training Effect | session field `24` | stored value / 10 | exact field location; proprietary calculation |
| Anaerobic Training Effect | session field `137` | stored value / 10 | exact field location; proprietary calculation |
| Training Load Peak | session field `168` | signed raw value / 65536 | exact field location; proprietary calculation |
| HR time-in-zone | message `216`, field `2` | milliseconds / 1000 | exact/public aggregation |
| HR boundaries | message `216`, field `6` | bpm | copied from Klemen donor |
| FTP target | message `7`, field `3` | watts | copied from Klemen donor |

Unknown Garmin messages `79`, `104`, `113`, `140`, `141`, `147`, and `288` are dumped in
the per-file analyses. Their changing values and undocumented semantics are
**unknown/proprietary**; the pipeline does not write guessed replacements.

Across Klemen's files, the same aerobic TE of `5.0` appears with training loads near
`284.083`, `340.114`, and `325.900`, demonstrating that TE alone does not determine load.
The available files are sufficient to confirm locations/scaling, but not an exact
Firstbeat relationship or a reliable identification of Recovery Time/Load Focus fields.

## Reverse-Engineered Attempt

The attempt variant uses only Klemen's Garmin evidence. Method:
`{inferred.get('method', inferred.get('reason'))}`.

Attempted values: aerobic TE `{inferred.get('aerobic_te')}`, anaerobic TE
`{inferred.get('anaerobic_te')}`, training load `{inferred.get('training_load')}`.

Evidence neighbors: `{inferred.get('neighbors')}`.

Classification: **empirically inferred**, low confidence, and **unproven upload behavior**.
No Recovery Time, Load Focus, Training Status, or undocumented message values are invented.
""",
    )
    validation_rows = [
        "| Variant | Internal/CRC | Garmin FIT SDK | Consistency |",
        "|---|---|---|---|",
    ]
    for name in VARIANTS:
        item = validation[name]
        validation_rows.append(
            f"| `{name}` | {item.get('internal_decode')}/{item.get('crc')} | "
            f"{item.get('garmin_fit_sdk')} | {item.get('consistency_result')} |"
        )
    _write_text(
        reports / "validation_results.md",
        "# Validation Results\n\n"
        + "\n".join(validation_rows)
        + "\n\nDetailed results: `outputs/reports/validation_results.json`.\n\n"
        + "Garmin Connect acceptance and post-watch-sync load/recovery effects remain "
        + "**unproven upload behavior** until manually uploaded and synced.",
    )
    _write_json(reports / "validation_results.json", validation)
    _write_text(
        reports / "test_matrix.md",
        """# Upload Test Matrix

Upload one variant at a time. Record Garmin Connect acceptance, then sync the watch and
record whether Acute Load, Recovery Time, Training Status, or Load Focus changes.

| Variant | Garmin structure | HR profile/zones | Power target/zones | Computed HR TIZ | Inferred TE/load | Connect accepted | Watch synced | Acute Load | Recovery | Status/Focus |
|---|---|---|---|---|---|---|---|---|---|---|
| `structural_only` | copied donor schema | no | no | no | no | pending | pending | pending | pending | pending |
| `structural_plus_hr_zones` | copied donor schema | yes | no | no | no | pending | pending | pending | pending | pending |
| `structural_plus_power_zones` | copied donor schema | yes | FTP target only; no explicit boundaries | no | no | pending | pending | pending | pending | pending |
| `structural_plus_time_in_zone` | copied donor schema | yes | FTP target only; no power TIZ | yes | no | pending | pending | pending | pending | pending |
| `reverse_engineered_metrics_attempt` | copied donor schema | yes | FTP target only; no power TIZ | yes | yes, low-confidence | pending | pending | pending | pending | pending |

Do not upload multiple variants of the same activity without deleting the prior test, or
duplicate detection and accumulated load changes will confound the result.
""",
    )
    _write_json(output_dir / "hashes.json", hashes)


def run_pipeline(
    mywhoosh_path: Path,
    klemen_dir: Path,
    robert_zip: Path,
    output_dir: Path,
    profile_config: Optional[ProfileConfig] = None,
    overwrite: bool = False,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    analysis_dir = output_dir / "analysis"
    variants_dir = output_dir / "variants"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    variants_dir.mkdir(parents=True, exist_ok=True)

    klemen_inputs = _load_zip_fit_files(klemen_dir)
    if not klemen_inputs:
        raise ValueError(f"no Garmin FIT files found in {klemen_dir}")
    mywhoosh = _load_fit(mywhoosh_path)
    donor, ranking = select_donor(klemen_inputs)

    klemen_analyses = [analyze_fit(source) for source in klemen_inputs]
    for analysis in klemen_analyses:
        _write_json(analysis_dir / f"{analysis['label']}_analysis.json", analysis)
    mywhoosh_analysis = analyze_fit(mywhoosh)
    _write_json(
        analysis_dir / f"{mywhoosh.label}_analysis.json", mywhoosh_analysis
    )
    robert_analysis = analyze_robert_gpx(robert_zip)
    _write_json(analysis_dir / "robert_gpx_analysis.json", robert_analysis)

    variants, notes = build_variants(mywhoosh, donor, klemen_inputs, profile_config)
    variant_paths = {}
    validation = {}
    skipped = []
    for name, data in variants.items():
        path = variants_dir / f"{mywhoosh.label}_{name}.fit"
        if path.exists() and not overwrite:
            skipped.append(str(path))
            variant_paths[name] = path
            validation[name] = validate_fit(path.read_bytes())
            continue
        path.write_bytes(data)
        variant_paths[name] = path
        validation[name] = validate_fit(data)
    if skipped:
        print(
            "Skipped existing variant files (pass --overwrite to regenerate):\n  "
            + "\n  ".join(skipped)
        )

    hashes = {
        "inputs": {
            "mywhoosh": {
                "path": str(mywhoosh_path),
                "sha256": _sha256(mywhoosh.data),
            },
            "klemen_fits": [
                {
                    "label": source.label,
                    "container": str(source.container),
                    "sha256": _sha256(source.data),
                }
                for source in klemen_inputs
            ],
            "robert_archive": {
                "path": str(robert_zip),
                "sha256": _sha256(robert_zip.read_bytes()),
                "fit_files": 0,
            },
        },
        "outputs": {
            name: {"path": str(path), "sha256": _sha256(path.read_bytes())}
            for name, path in variant_paths.items()
        },
    }
    _write_reports(
        output_dir,
        mywhoosh,
        donor,
        ranking,
        klemen_analyses,
        robert_analysis,
        mywhoosh_analysis,
        variant_paths,
        validation,
        notes,
        hashes,
    )
    _write_json(output_dir / "pipeline_notes.json", notes)
    return variant_paths


def _add_build_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mywhoosh", required=True, type=Path, help="MyWhoosh FIT input")
    parser.add_argument(
        "--klemen-dir",
        type=Path,
        required=True,
        help="directory containing Garmin activity ZIPs",
    )
    parser.add_argument(
        "--robert-zip",
        type=Path,
        required=True,
        help="additional activity archive used only for structural research",
    )
    parser.add_argument(
        "--outputs", type=Path, default=Path("outputs"), help="output directory"
    )
    parser.add_argument(
        "--profile-config",
        type=Path,
        default=None,
        help="Optional JSON config with max_hr/ftp/weight_kg/hr_zones/power_zones overrides "
        "for the profile_zones_injected variant.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing variant FITs in outputs/variants/.",
    )


def _add_paired_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mywhoosh", required=True, type=Path)
    parser.add_argument("--garmin-native", required=True, type=Path)
    parser.add_argument("--outputs", type=Path, default=Path("outputs"))
    parser.add_argument(
        "--notes",
        type=Path,
        default=Path("outputs/pipeline_notes.json"),
        help="Optional pipeline_notes.json with the reverse-engineered estimate.",
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Empirical Garmin-template MyWhoosh FIT pipeline."
    )
    subparsers = parser.add_subparsers(dest="command")

    build_parser = subparsers.add_parser(
        "build",
        help="Build and validate Garmin-template variants (default).",
    )
    _add_build_arguments(build_parser)

    paired_parser = subparsers.add_parser(
        "compare-paired",
        help="Compare a MyWhoosh FIT with a Garmin-native recording of the same ride.",
    )
    _add_paired_arguments(paired_parser)

    rank_parser = subparsers.add_parser(
        "rank-variants",
        help="Rank variant FITs by structural readiness against a trusted reference.",
    )
    rank_parser.add_argument("--trusted", required=True, type=Path)
    rank_parser.add_argument(
        "--variants",
        required=True,
        nargs="+",
        help="One or more FIT file paths or glob patterns.",
    )
    rank_parser.add_argument("--outputs", type=Path, default=Path("outputs"))

    # Back-compat: invocations without a subcommand default to "build" so existing
    # `python garmin_pipeline.py --mywhoosh ...` calls keep working.
    import sys as _sys

    raw_args = list(argv) if argv is not None else _sys.argv[1:]
    known_commands = {"build", "compare-paired", "rank-variants", "-h", "--help"}
    if not raw_args or raw_args[0] not in known_commands:
        raw_args = ["build", *raw_args]
    arguments = parser.parse_args(raw_args)

    if arguments.command == "build":
        profile = load_profile_config(arguments.profile_config)
        paths = run_pipeline(
            arguments.mywhoosh,
            arguments.klemen_dir,
            arguments.robert_zip,
            arguments.outputs,
            profile_config=profile,
            overwrite=arguments.overwrite,
        )
        for name, path in paths.items():
            print(f"{name}: {path}")
        return 0
    if arguments.command == "compare-paired":
        from paired_compare import run_paired_comparison

        run_paired_comparison(
            arguments.mywhoosh,
            arguments.garmin_native,
            arguments.outputs,
            arguments.notes,
        )
        return 0
    if arguments.command == "rank-variants":
        from variant_rank import run_rank

        readiness = run_rank(arguments.trusted, arguments.variants, arguments.outputs)
        for index, item in enumerate(readiness, start=1):
            print(
                f"{index:>2}. {item.path.name}: {item.score}/{item.max_score} "
                f"({item.ratio:.0%}), {len(item.risks)} risk(s)"
            )
        return 0
    parser.error(f"unknown command {arguments.command!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
