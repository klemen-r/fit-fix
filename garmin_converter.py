"""Convert one MyWhoosh FIT using a Garmin-native activity as a template."""

from __future__ import annotations

import argparse
import os
import statistics
import tempfile
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Iterable, Sequence

from fix_fit import (
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
    TYPE_STRING,
    TYPE_UINT8,
    TYPE_UINT16,
    TYPE_UINT32,
    Message,
    RawField,
    _encode_fit,
    _local_timestamp,
    _normalized_power,
    _pack_field,
    _parse_fit,
    _read_uint,
    _timed_record_total,
    _vertical_totals,
    convert_fit_bytes,
)

CYCLING = 2
INDOOR_CYCLING = 6
GARMIN_MANUFACTURER = 1
MAX_FIT_BYTES = 128 * 1024 * 1024


def _read_fit(path: Path) -> bytes:
    if not path.is_file():
        raise ValueError(f"FIT file not found: {path}")
    size = path.stat().st_size
    if size > MAX_FIT_BYTES:
        raise ValueError(f"FIT file is larger than {MAX_FIT_BYTES // (1024 * 1024)} MiB")
    data = path.read_bytes()
    if len(data) > MAX_FIT_BYTES:
        raise ValueError(f"FIT file is larger than {MAX_FIT_BYTES // (1024 * 1024)} MiB")
    return data


def _messages_of(messages: Sequence[Message], number: int) -> list[Message]:
    return [message for message in messages if message.global_message == number]


def _one(messages: Sequence[Message], number: int) -> Message:
    found = _messages_of(messages, number)
    if len(found) != 1:
        raise ValueError(f"expected one message {number}, found {len(found)}")
    return found[0]


def _replace_fields(message: Message, replacements: dict[int, RawField]) -> Message:
    fields: list[RawField] = []
    used: set[int] = set()
    for field in message.fields:
        if field.number in replacements and field.number not in used:
            fields.append(replacements[field.number])
            used.add(field.number)
        else:
            fields.append(field)
    fields.extend(field for number, field in replacements.items() if number not in used)
    return replace(message, fields=tuple(fields))


def _replace_uint(message: Message, number: int, value: int) -> Message:
    field = message.first(number)
    if field is None:
        raise ValueError(f"message {message.global_message} has no field {number}")
    return _replace_fields(
        message,
        {number: _pack_field(message.endian, number, field.base_type, value)},
    )


def _replace_enum(message: Message, number: int, value: int) -> Message:
    return _replace_fields(
        message, {number: _pack_field(message.endian, number, TYPE_ENUM, value)}
    )


def _set_uint_fields(
    message: Message, values: dict[int, tuple[int, int | None]]
) -> Message:
    return _replace_fields(
        message,
        {
            number: _pack_field("<", number, base_type, value)
            for number, (base_type, value) in values.items()
            if value is not None
        },
    )


def _valid_values(messages: Sequence[Message], field: int) -> list[int]:
    return [
        value
        for message in messages
        if (value := _read_uint(message, field)) is not None
    ]


def _recalculate_summaries(messages: Sequence[Message]) -> tuple[list[Message], int, int]:
    records = _messages_of(messages, MSG_RECORD)
    session = _one(messages, MSG_SESSION)
    timestamps = _valid_values(records, F_TIMESTAMP)
    if len(timestamps) != len(records) or not records:
        raise ValueError("MyWhoosh records need valid timestamps")
    intervals = [
        current - previous
        for previous, current in zip(timestamps, timestamps[1:])
        if 0 < current - previous <= 60
    ]
    final_interval = round(statistics.median(intervals)) if intervals else 1
    start = timestamps[0]
    end = timestamps[-1] + final_interval
    elapsed_ms = (end - start) * 1000

    distances = _valid_values(records, 5)
    speeds = _valid_values(records, 6)
    heart_rates = _valid_values(records, 3)
    cadences = _valid_values(records, 4)
    powers = _valid_values(records, 7)
    ascent, descent = _vertical_totals(records)
    work = _timed_record_total(records, 7, end)
    cadence_total = _timed_record_total(records, 4, end)
    complete_one_hz_power = len(powers) == len(records) and all(
        current - previous == 1
        for previous, current in zip(timestamps, timestamps[1:])
    )
    distance = distances[-1] if distances else None
    common = {
        253: (TYPE_UINT32, end),
        2: (TYPE_UINT32, start),
        7: (TYPE_UINT32, elapsed_ms),
        8: (TYPE_UINT32, elapsed_ms),
        9: (TYPE_UINT32, distance),
        10: (
            TYPE_UINT32,
            round(cadence_total / 60) if cadence_total is not None else None,
        ),
        11: (TYPE_UINT16, _read_uint(session, 11)),
    }
    lap_values = common | {
        13: (
            TYPE_UINT16,
            round(distance * 10_000 / elapsed_ms) if distance is not None else None,
        ),
        14: (TYPE_UINT16, max(speeds) if speeds else None),
        15: (
            TYPE_UINT8,
            round(sum(heart_rates) / len(heart_rates)) if heart_rates else None,
        ),
        16: (TYPE_UINT8, max(heart_rates) if heart_rates else None),
        17: (
            TYPE_UINT8,
            round(sum(cadences) / len(cadences)) if cadences else None,
        ),
        18: (TYPE_UINT8, max(cadences) if cadences else None),
        19: (TYPE_UINT16, round(sum(powers) / len(powers)) if powers else None),
        20: (TYPE_UINT16, max(powers) if powers else None),
        21: (TYPE_UINT16, ascent),
        22: (TYPE_UINT16, descent),
        33: (
            TYPE_UINT16,
            _normalized_power(powers) if complete_one_hz_power else None,
        ),
        41: (TYPE_UINT32, work),
    }
    session_values = common | {
        14: lap_values[13],
        15: lap_values[14],
        16: lap_values[15],
        17: lap_values[16],
        18: lap_values[17],
        19: lap_values[18],
        20: lap_values[19],
        21: lap_values[20],
        22: lap_values[21],
        23: lap_values[22],
        34: lap_values[33],
        48: lap_values[41],
    }

    output = []
    for message in messages:
        if message.global_message == MSG_LAP:
            output.append(_set_uint_fields(message, lap_values))
        elif message.global_message == MSG_SESSION:
            output.append(_set_uint_fields(message, session_values))
        elif message.global_message == MSG_ACTIVITY:
            output.append(
                _set_uint_fields(
                    message,
                    {
                        253: (TYPE_UINT32, end),
                        0: (TYPE_UINT32, elapsed_ms),
                        5: (TYPE_UINT32, _local_timestamp(end)),
                    },
                )
            )
        else:
            output.append(message)
    return output, start, end


def _target_sport(message: Message) -> Message:
    replacements = {
        0: _pack_field(message.endian, 0, TYPE_ENUM, CYCLING),
        1: _pack_field(message.endian, 1, TYPE_ENUM, INDOOR_CYCLING),
    }
    name = message.first(3)
    if name is not None:
        replacements[3] = _pack_field(
            message.endian, 3, TYPE_STRING, "INDOOR CYCLING", name.size
        )
    return _replace_fields(message, replacements)


def _target_lap(message: Message) -> Message:
    return _replace_enum(_replace_enum(message, 25, CYCLING), 39, INDOOR_CYCLING)


def _target_session(message: Message) -> Message:
    return _replace_enum(_replace_enum(message, 5, CYCLING), 6, INDOOR_CYCLING)


def _timer_events(
    template_messages: Sequence[Message], start: int, end: int
) -> tuple[Message, Message]:
    events = _messages_of(template_messages, MSG_EVENT)
    start_template = next(
        message
        for message in events
        if _read_uint(message, 0) == 0 and _read_uint(message, 1) == 0
    )
    stop_template = next(
        message
        for message in reversed(events)
        if _read_uint(message, 0) == 0 and _read_uint(message, 1) == 4
    )
    return (
        _replace_uint(start_template, F_TIMESTAMP, start),
        _replace_uint(stop_template, F_TIMESTAMP, end),
    )


def _device_messages(
    template_messages: Sequence[Message],
    template_start: int,
    target_start: int,
    target_end: int,
) -> tuple[list[Message], list[Message]]:
    start_devices: list[Message] = []
    end_devices: list[Message] = []
    for message in _messages_of(template_messages, MSG_DEVICE_INFO):
        timestamp = _read_uint(message, F_TIMESTAMP)
        is_start = timestamp is None or timestamp <= template_start + 1
        copied = (
            message
            if timestamp is None
            else _replace_uint(
                message, F_TIMESTAMP, target_start if is_start else target_end
            )
        )
        (start_devices if is_start else end_devices).append(copied)
    return start_devices, end_devices


def _payload(message: Message, excluded: set[int] | None = None) -> tuple[object, ...]:
    excluded = excluded or set()
    return (
        message.global_message,
        message.endian,
        tuple(
            (field.number, field.size, field.base_type, field.data)
            for field in message.fields
            if field.number not in excluded
        ),
        tuple(
            (field.number, field.size, field.developer_index, field.data)
            for field in message.developer_fields
        ),
    )


def _payloads(messages: Iterable[Message], excluded: set[int] | None = None) -> Counter:
    return Counter(_payload(message, excluded) for message in messages)


def _validate(
    output: bytes,
    source_messages: Sequence[Message],
    template_messages: Sequence[Message],
) -> None:
    _header, messages = _parse_fit(output)
    if _payloads(_messages_of(messages, MSG_RECORD)) != _payloads(
        _messages_of(source_messages, MSG_RECORD)
    ):
        raise ValueError("converted record stream does not match the MyWhoosh source")
    if _payloads(_messages_of(messages, MSG_DEVICE_INFO), {F_TIMESTAMP}) != _payloads(
        _messages_of(template_messages, MSG_DEVICE_INFO), {F_TIMESTAMP}
    ):
        raise ValueError("converted Garmin device metadata does not match the template")
    sport = _one(messages, MSG_SPORT)
    lap = _one(messages, MSG_LAP)
    session = _one(messages, MSG_SESSION)
    if not (
        _read_uint(sport, 0) == CYCLING
        and _read_uint(sport, 1) == INDOOR_CYCLING
        and _read_uint(lap, 25) == CYCLING
        and _read_uint(lap, 39) == INDOOR_CYCLING
        and _read_uint(session, 5) == CYCLING
        and _read_uint(session, 6) == INDOOR_CYCLING
    ):
        raise ValueError("converted FIT is not cycling / indoor_cycling")


def convert(source_path: Path, template_path: Path) -> bytes:
    normalized = convert_fit_bytes(_read_fit(source_path))
    _source_header, source_messages = _parse_fit(normalized)
    source_messages, start, end = _recalculate_summaries(source_messages)
    template_header, template_messages = _parse_fit(_read_fit(template_path))
    template_file_id = _one(template_messages, MSG_FILE_ID)
    if _read_uint(template_file_id, 1) != GARMIN_MANUFACTURER:
        raise ValueError("selected template was not recorded by a Garmin device")
    if not _messages_of(template_messages, MSG_DEVICE_INFO):
        raise ValueError("Garmin template has no device metadata")

    source_session = _target_session(_one(source_messages, MSG_SESSION))
    source_lap = _target_lap(_one(source_messages, MSG_LAP))
    source_activity = _one(source_messages, MSG_ACTIVITY)

    template_session = _one(template_messages, MSG_SESSION)
    template_start = _read_uint(template_session, 2)
    if template_start is None:
        raise ValueError("Garmin template has no session start time")

    file_id = _replace_uint(template_file_id, 4, start)
    creator = _messages_of(template_messages, MSG_FILE_CREATOR)
    event_start, event_stop = _timer_events(template_messages, start, end)
    start_devices, end_devices = _device_messages(
        template_messages, template_start, start, end
    )
    output_messages = (
        [file_id]
        + creator
        + [event_start]
        + start_devices
        + [_target_sport(_one(source_messages, MSG_SPORT))]
        + _messages_of(source_messages, MSG_RECORD)
        + [event_stop]
        + end_devices
        + [source_lap, source_session, source_activity]
    )
    output = _encode_fit(template_header, output_messages)
    _validate(output, source_messages, template_messages)
    return output


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert a MyWhoosh FIT for Garmin.")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--template", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()

    _atomic_write(arguments.output, convert(arguments.source, arguments.template))
    print(arguments.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
