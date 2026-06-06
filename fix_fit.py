"""Normalize MyWhoosh FIT activities for Garmin watch compatibility."""

from __future__ import annotations

import argparse
import os
import struct
import sys
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Sequence

__version__ = "3.1.1"

FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)
FIT_SIGNATURE = b".FIT"

MSG_FILE_ID = 0
MSG_SPORT = 12
MSG_SESSION = 18
MSG_LAP = 19
MSG_RECORD = 20
MSG_EVENT = 21
MSG_DEVICE_INFO = 23
MSG_ACTIVITY = 34
MSG_FILE_CREATOR = 49
MSG_FIELD_DESCRIPTION = 206
MSG_DEVELOPER_DATA_ID = 207

F_TIMESTAMP = 253
F_MESSAGE_INDEX = 254

MYWHOOSH_MANUFACTURER = 331
GARMIN_MANUFACTURER = 1
EDGE_1050_PRODUCT = 4440

TYPE_ENUM = 0x00
TYPE_UINT8 = 0x02
TYPE_STRING = 0x07
TYPE_UINT16 = 0x84
TYPE_SINT32 = 0x85
TYPE_UINT32 = 0x86
TYPE_UINT32Z = 0x8C

U32_INVALID = 0xFFFFFFFF


class FitError(Exception):
    """Raised when a FIT file cannot be normalized safely."""


@dataclass(frozen=True)
class RawField:
    number: int
    size: int
    base_type: int
    data: bytes


@dataclass(frozen=True)
class DeveloperField:
    number: int
    size: int
    developer_index: int
    data: bytes


@dataclass(frozen=True)
class Definition:
    global_message: int
    endian: str
    fields: tuple[tuple[int, int, int], ...]
    developer_fields: tuple[tuple[int, int, int], ...]

    @property
    def size(self) -> int:
        return sum(field[1] for field in self.fields) + sum(
            field[1] for field in self.developer_fields
        )


@dataclass(frozen=True)
class Message:
    global_message: int
    endian: str
    fields: tuple[RawField, ...]
    developer_fields: tuple[DeveloperField, ...] = ()

    def first(self, number: int) -> Optional[RawField]:
        return next((field for field in self.fields if field.number == number), None)


def _crc_table() -> tuple[int, ...]:
    table = []
    for value in range(256):
        crc = value
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
        table.append(crc)
    return tuple(table)


CRC_TABLE = _crc_table()


def fit_crc(data, crc: int = 0) -> int:
    """Return the FIT protocol CRC for bytes-like data."""
    for byte in data:
        crc = (crc >> 8) ^ CRC_TABLE[(crc ^ byte) & 0xFF]
    return crc


def _require_available(position: int, size: int, end: int, label: str) -> None:
    if position + size > end:
        raise FitError(f"truncated {label}")


def _read_definition(
    data: bytes,
    position: int,
    body_end: int,
    has_developer_fields: bool,
) -> tuple[Definition, int]:
    _require_available(position, 5, body_end, "definition")
    if data[position] != 0:
        raise FitError("definition uses a non-zero reserved byte")
    position += 1
    architecture = data[position]
    position += 1
    if architecture not in (0, 1):
        raise FitError(f"invalid architecture byte 0x{architecture:02x}")
    endian = "<" if architecture == 0 else ">"
    global_message = struct.unpack_from(endian + "H", data, position)[0]
    position += 2
    field_count = data[position]
    position += 1

    _require_available(position, field_count * 3, body_end, "field definitions")
    fields = []
    for _ in range(field_count):
        fields.append(tuple(data[position : position + 3]))
        position += 3

    developer_fields = []
    if has_developer_fields:
        _require_available(position, 1, body_end, "developer field count")
        developer_field_count = data[position]
        position += 1
        _require_available(
            position,
            developer_field_count * 3,
            body_end,
            "developer field definitions",
        )
        for _ in range(developer_field_count):
            developer_fields.append(tuple(data[position : position + 3]))
            position += 3

    return (
        Definition(global_message, endian, tuple(fields), tuple(developer_fields)),
        position,
    )


def _read_message(data: bytes, position: int, definition: Definition) -> Message:
    fields = []
    for number, size, base_type in definition.fields:
        fields.append(RawField(number, size, base_type, data[position : position + size]))
        position += size
    developer_fields = []
    for number, size, developer_index in definition.developer_fields:
        developer_fields.append(
            DeveloperField(
                number, size, developer_index, data[position : position + size]
            )
        )
        position += size
    return Message(
        definition.global_message,
        definition.endian,
        tuple(fields),
        tuple(developer_fields),
    )


def _parse_fit(data: bytes) -> tuple[bytes, list[Message]]:
    if len(data) < 14:
        raise FitError("file is too small")
    header_size = data[0]
    if header_size not in (12, 14):
        raise FitError(f"invalid header size {header_size}")
    if data[8:12] != FIT_SIGNATURE:
        raise FitError("missing .FIT signature")

    data_size = struct.unpack_from("<I", data, 4)[0]
    body_end = header_size + data_size
    if len(data) != body_end + 2:
        raise FitError("file is truncated or has trailing data")
    if header_size == 14:
        stored_header_crc = struct.unpack_from("<H", data, 12)[0]
        if stored_header_crc and fit_crc(memoryview(data)[:12]) != stored_header_crc:
            raise FitError("header CRC mismatch")
    stored_file_crc = struct.unpack_from("<H", data, body_end)[0]
    if fit_crc(memoryview(data)[:body_end]) != stored_file_crc:
        raise FitError("file CRC mismatch")

    definitions: dict[int, Definition] = {}
    messages = []
    position = header_size
    while position < body_end:
        record_header = data[position]
        position += 1
        if record_header & 0x80:
            raise FitError("compressed timestamp records are not supported")
        if record_header & 0x10:
            raise FitError("record header uses a reserved bit")

        local_message = record_header & 0x0F
        is_definition = bool(record_header & 0x40)
        has_developer_fields = bool(record_header & 0x20)
        if is_definition:
            definition, position = _read_definition(
                data, position, body_end, has_developer_fields
            )
            definitions[local_message] = definition
            continue
        if has_developer_fields:
            raise FitError("data record uses a reserved bit")

        definition = definitions.get(local_message)
        if definition is None:
            raise FitError("data record has no definition")
        _require_available(position, definition.size, body_end, "data record")
        messages.append(_read_message(data, position, definition))
        position += definition.size

    return data[:header_size], messages


def _dedupe(message: Message) -> Message:
    seen = set()
    fields = []
    numbers = {field.number for field in message.fields}
    for field in message.fields:
        if message.global_message == MSG_RECORD:
            if field.number == 78 and 2 in numbers:
                continue
            if field.number == 73 and 6 in numbers:
                continue
        if field.number not in seen:
            seen.add(field.number)
            fields.append(field)
    return replace(message, fields=tuple(fields), developer_fields=())


def _base_type(field: RawField) -> int:
    return field.base_type & 0x1F


def _read_uint(message: Message, number: int) -> Optional[int]:
    field = message.first(number)
    if field is None:
        return None
    base_type = _base_type(field)
    formats = {
        (TYPE_ENUM, 1): "B",
        (TYPE_UINT8, 1): "B",
        (TYPE_UINT16 & 0x1F, 2): "H",
        (TYPE_UINT32 & 0x1F, 4): "I",
        (TYPE_UINT32Z & 0x1F, 4): "I",
    }
    fmt = formats.get((base_type, field.size))
    return struct.unpack(message.endian + fmt, field.data)[0] if fmt else None


def _read_sint32(message: Message, number: int) -> Optional[int]:
    field = message.first(number)
    if field is None or _base_type(field) != (TYPE_SINT32 & 0x1F) or field.size != 4:
        return None
    return struct.unpack(message.endian + "i", field.data)[0]


def _pack_field(
    endian: str,
    number: int,
    base_type: int,
    value: int | str,
    size: Optional[int] = None,
) -> RawField:
    kind = base_type & 0x1F
    if kind in (TYPE_ENUM, TYPE_UINT8):
        data = struct.pack("B", int(value))
    elif kind == (TYPE_UINT16 & 0x1F):
        data = struct.pack(endian + "H", int(value))
    elif kind in ((TYPE_UINT32 & 0x1F), (TYPE_UINT32Z & 0x1F)):
        data = struct.pack(endian + "I", int(value))
    elif kind == (TYPE_SINT32 & 0x1F):
        data = struct.pack(endian + "i", int(value))
    elif kind == TYPE_STRING:
        encoded = str(value).encode("utf-8") + b"\0"
        length = size or len(encoded)
        data = encoded[:length].ljust(length, b"\0")
    else:
        raise FitError(f"unsupported generated base type 0x{base_type:02x}")
    return RawField(number, len(data), base_type, data)


def _message(global_message: int, fields: Sequence[RawField]) -> Message:
    return Message(global_message, "<", tuple(fields))


def _replace_uint(message: Message, number: int, value: int) -> Message:
    fields = []
    replaced = False
    for field in message.fields:
        if field.number == number and not replaced:
            kind = _base_type(field)
            if kind == (TYPE_UINT16 & 0x1F) and field.size == 2:
                data = struct.pack(message.endian + "H", value)
            elif kind in ((TYPE_UINT32 & 0x1F), (TYPE_UINT32Z & 0x1F)) and field.size == 4:
                data = struct.pack(message.endian + "I", value)
            else:
                raise FitError(f"field {number} has an incompatible type")
            fields.append(replace(field, data=data))
            replaced = True
        elif field.number != number:
            fields.append(field)
    if not replaced:
        raise FitError(f"required field {number} is missing")
    return replace(message, fields=tuple(fields), developer_fields=())


def _schema(message: Message) -> tuple:
    return (
        message.global_message,
        message.endian,
        tuple((field.number, field.size, field.base_type) for field in message.fields),
        tuple(
            (field.number, field.size, field.developer_index)
            for field in message.developer_fields
        ),
    )


def _encode_definition(message: Message) -> bytes:
    has_developer_fields = bool(message.developer_fields)
    header = 0x40 | (0x20 if has_developer_fields else 0)
    output = bytearray([header, 0, 0 if message.endian == "<" else 1])
    output += struct.pack(message.endian + "H", message.global_message)
    output.append(len(message.fields))
    for field in message.fields:
        output += bytes([field.number, field.size, field.base_type])
    if has_developer_fields:
        output.append(len(message.developer_fields))
        for field in message.developer_fields:
            output += bytes([field.number, field.size, field.developer_index])
    return bytes(output)


def _encode_fit(header: bytes, messages: Sequence[Message]) -> bytes:
    body = bytearray()
    active_schema = None
    for message in messages:
        schema = _schema(message)
        if schema != active_schema:
            body += _encode_definition(message)
            active_schema = schema
        body.append(0)
        for field in message.fields:
            body += field.data
        for field in message.developer_fields:
            body += field.data

    new_header = bytearray(header)
    struct.pack_into("<I", new_header, 4, len(body))
    if len(new_header) == 14:
        struct.pack_into("<H", new_header, 12, fit_crc(new_header[:12]))
    output = bytes(new_header) + bytes(body)
    return output + struct.pack("<H", fit_crc(output))


def _required(messages: Sequence[Message], global_message: int) -> Message:
    message = next(
        (message for message in messages if message.global_message == global_message),
        None,
    )
    if message is None:
        raise FitError(f"file has no global message {global_message}")
    return message


def _valid_u32(value: Optional[int]) -> bool:
    return value is not None and value not in (0, U32_INVALID)


def _record_values(records: Sequence[Message], number: int, signed: bool = False) -> list[int]:
    reader = _read_sint32 if signed else _read_uint
    return [
        value
        for record in records
        if (value := reader(record, number)) is not None
    ]


def _summary_value(session: Message, number: int, fallback: Optional[int] = None) -> Optional[int]:
    value = _read_uint(session, number)
    return fallback if value in (None, U32_INVALID) else value


def _local_timestamp(end_timestamp: int) -> int:
    end_utc = FIT_EPOCH + timedelta(seconds=end_timestamp)
    offset = end_utc.astimezone().utcoffset() or timedelta(0)
    return end_timestamp + int(offset.total_seconds())


def _activity_times(
    session: Message,
    lap: Message,
    record_timestamps: Sequence[int],
) -> tuple[int, int, int, int]:
    """Repair MyWhoosh summary times without shifting the record stream."""
    start_timestamp = _read_uint(session, 2) or record_timestamps[0]
    elapsed_ms = _summary_value(session, 7)
    timer_ms = _summary_value(session, 8, elapsed_ms)
    if elapsed_ms is None or timer_ms is None:
        raise FitError("session has no elapsed/timer time")

    expected_end = start_timestamp + round(elapsed_ms / 1000)
    lap_end = _read_uint(lap, F_TIMESTAMP)
    end_candidates = [record_timestamps[-1], expected_end]
    if lap_end is not None and abs(lap_end - expected_end) <= 60:
        end_candidates.append(lap_end)
    end_timestamp = max(end_candidates)
    return start_timestamp, end_timestamp, elapsed_ms, timer_ms


def _build_standard_messages(messages: Sequence[Message]) -> list[Message]:
    file_id = _dedupe(_required(messages, MSG_FILE_ID))
    manufacturer = _read_uint(file_id, 1)
    product = _read_uint(file_id, 2)
    if manufacturer not in (MYWHOOSH_MANUFACTURER, GARMIN_MANUFACTURER):
        raise FitError(f"not a MyWhoosh/Garmin-converted FIT file (manufacturer {manufacturer})")
    if manufacturer == GARMIN_MANUFACTURER and product != EDGE_1050_PRODUCT:
        raise FitError(f"Garmin input is not an Edge 1050 conversion (product {product})")
    file_id = _replace_uint(file_id, 1, GARMIN_MANUFACTURER)
    file_id = _replace_uint(file_id, 2, EDGE_1050_PRODUCT)

    session = _required(messages, MSG_SESSION)
    lap = _required(messages, MSG_LAP)
    records = [_dedupe(message) for message in messages if message.global_message == MSG_RECORD]
    if not records:
        raise FitError("file has no record messages")

    record_timestamps = _record_values(records, F_TIMESTAMP)
    if not record_timestamps:
        raise FitError("records have no timestamps")
    start_timestamp, end_timestamp, elapsed_ms, timer_ms = _activity_times(
        session, lap, record_timestamps
    )

    distances = _record_values(records, 5)
    speeds = _record_values(records, 6)
    heart_rates = _record_values(records, 3)
    cadences = _record_values(records, 4)
    powers = _record_values(records, 7)
    latitudes = _record_values(records, 0, signed=True)
    longitudes = _record_values(records, 1, signed=True)

    total_distance = _summary_value(session, 9, distances[-1] if distances else None)
    avg_speed = _summary_value(
        session,
        14,
        round(total_distance * 10_000 / timer_ms)
        if total_distance is not None
        else None,
    )
    max_speed = _summary_value(session, 15, max(speeds) if speeds else None)
    total_cycles = _summary_value(
        session, 10, round(sum(cadences) / 60) if cadences else None
    )

    summary = {
        "calories": _summary_value(session, 11),
        "distance": total_distance,
        "cycles": total_cycles,
        "ascent": _summary_value(session, 22),
        "avg_speed": avg_speed,
        "max_speed": max_speed,
        "avg_hr": _summary_value(
            session, 16, round(sum(heart_rates) / len(heart_rates)) if heart_rates else None
        ),
        "max_hr": _summary_value(session, 17, max(heart_rates) if heart_rates else None),
        "avg_cadence": _summary_value(
            session, 18, round(sum(cadences) / len(cadences)) if cadences else None
        ),
        "max_cadence": _summary_value(session, 19, max(cadences) if cadences else None),
        "avg_power": _summary_value(
            session, 20, round(sum(powers) / len(powers)) if powers else None
        ),
        "max_power": _summary_value(session, 21, max(powers) if powers else None),
    }

    def add(
        fields: list[RawField],
        number: int,
        base_type: int,
        value: Optional[int],
    ) -> None:
        if value is not None:
            fields.append(_pack_field("<", number, base_type, value))

    event_start = _message(
        MSG_EVENT,
        [
            _pack_field("<", F_TIMESTAMP, TYPE_UINT32, start_timestamp),
            _pack_field("<", 0, TYPE_ENUM, 0),
            _pack_field("<", 1, TYPE_ENUM, 0),
            _pack_field("<", 4, TYPE_UINT8, 0),
        ],
    )
    event_stop = _message(
        MSG_EVENT,
        [
            _pack_field("<", F_TIMESTAMP, TYPE_UINT32, end_timestamp),
            _pack_field("<", 0, TYPE_ENUM, 0),
            _pack_field("<", 1, TYPE_ENUM, 4),
            _pack_field("<", 4, TYPE_UINT8, 0),
        ],
    )

    sport = _message(
        MSG_SPORT,
        [
            _pack_field("<", 3, TYPE_STRING, "VIRTUAL CYCLING", 16),
            _pack_field("<", 0, TYPE_ENUM, 2),
            _pack_field("<", 1, TYPE_ENUM, 58),
        ],
    )

    serial_number = _read_uint(file_id, 3)
    device_fields = [
        _pack_field("<", F_TIMESTAMP, TYPE_UINT32, start_timestamp),
        _pack_field("<", 2, TYPE_UINT16, GARMIN_MANUFACTURER),
        _pack_field("<", 4, TYPE_UINT16, EDGE_1050_PRODUCT),
        _pack_field("<", 0, TYPE_UINT8, 0),
        _pack_field("<", 25, TYPE_ENUM, 5),
    ]
    if serial_number is not None:
        device_fields.insert(1, _pack_field("<", 3, TYPE_UINT32Z, serial_number))
    device_info = _message(MSG_DEVICE_INFO, device_fields)

    lap_fields = [
        _pack_field("<", F_TIMESTAMP, TYPE_UINT32, end_timestamp),
        _pack_field("<", F_MESSAGE_INDEX, TYPE_UINT16, 0),
        _pack_field("<", 0, TYPE_ENUM, 9),
        _pack_field("<", 1, TYPE_ENUM, 1),
        _pack_field("<", 2, TYPE_UINT32, start_timestamp),
        _pack_field("<", 7, TYPE_UINT32, elapsed_ms),
        _pack_field("<", 8, TYPE_UINT32, timer_ms),
    ]
    if latitudes and longitudes:
        lap_fields += [
            _pack_field("<", 3, TYPE_SINT32, latitudes[0]),
            _pack_field("<", 4, TYPE_SINT32, longitudes[0]),
            _pack_field("<", 5, TYPE_SINT32, latitudes[-1]),
            _pack_field("<", 6, TYPE_SINT32, longitudes[-1]),
        ]
    add(lap_fields, 9, TYPE_UINT32, summary["distance"])
    add(lap_fields, 10, TYPE_UINT32, summary["cycles"])
    add(lap_fields, 11, TYPE_UINT16, summary["calories"])
    add(lap_fields, 13, TYPE_UINT16, summary["avg_speed"])
    add(lap_fields, 14, TYPE_UINT16, summary["max_speed"])
    add(lap_fields, 15, TYPE_UINT8, summary["avg_hr"])
    add(lap_fields, 16, TYPE_UINT8, summary["max_hr"])
    add(lap_fields, 17, TYPE_UINT8, summary["avg_cadence"])
    add(lap_fields, 18, TYPE_UINT8, summary["max_cadence"])
    add(lap_fields, 19, TYPE_UINT16, summary["avg_power"])
    add(lap_fields, 20, TYPE_UINT16, summary["max_power"])
    add(lap_fields, 21, TYPE_UINT16, summary["ascent"])
    lap_fields += [
        _pack_field("<", 23, TYPE_ENUM, 0),
        _pack_field("<", 24, TYPE_ENUM, 7),
        _pack_field("<", 25, TYPE_ENUM, 2),
        _pack_field("<", 26, TYPE_UINT8, 0),
        _pack_field("<", 39, TYPE_ENUM, 58),
    ]
    normalized_lap = _message(MSG_LAP, lap_fields)

    session_fields = [
        _pack_field("<", F_TIMESTAMP, TYPE_UINT32, end_timestamp),
        _pack_field("<", F_MESSAGE_INDEX, TYPE_UINT16, 0),
        _pack_field("<", 0, TYPE_ENUM, 9),
        _pack_field("<", 1, TYPE_ENUM, 1),
        _pack_field("<", 2, TYPE_UINT32, start_timestamp),
        _pack_field("<", 7, TYPE_UINT32, elapsed_ms),
        _pack_field("<", 8, TYPE_UINT32, timer_ms),
    ]
    if latitudes and longitudes:
        session_fields += [
            _pack_field("<", 3, TYPE_SINT32, latitudes[0]),
            _pack_field("<", 4, TYPE_SINT32, longitudes[0]),
            _pack_field("<", 29, TYPE_SINT32, max(latitudes)),
            _pack_field("<", 30, TYPE_SINT32, max(longitudes)),
            _pack_field("<", 31, TYPE_SINT32, min(latitudes)),
            _pack_field("<", 32, TYPE_SINT32, min(longitudes)),
        ]
    add(session_fields, 9, TYPE_UINT32, summary["distance"])
    add(session_fields, 10, TYPE_UINT32, summary["cycles"])
    add(session_fields, 11, TYPE_UINT16, summary["calories"])
    add(session_fields, 14, TYPE_UINT16, summary["avg_speed"])
    add(session_fields, 15, TYPE_UINT16, summary["max_speed"])
    add(session_fields, 16, TYPE_UINT8, summary["avg_hr"])
    add(session_fields, 17, TYPE_UINT8, summary["max_hr"])
    add(session_fields, 18, TYPE_UINT8, summary["avg_cadence"])
    add(session_fields, 19, TYPE_UINT8, summary["max_cadence"])
    add(session_fields, 20, TYPE_UINT16, summary["avg_power"])
    add(session_fields, 21, TYPE_UINT16, summary["max_power"])
    add(session_fields, 22, TYPE_UINT16, summary["ascent"])
    session_fields += [
        _pack_field("<", 5, TYPE_ENUM, 2),
        _pack_field("<", 6, TYPE_ENUM, 58),
        _pack_field("<", 25, TYPE_UINT16, 0),
        _pack_field("<", 26, TYPE_UINT16, 1),
        _pack_field("<", 28, TYPE_ENUM, 0),
    ]
    normalized_session = _message(MSG_SESSION, session_fields)

    activity = _message(
        MSG_ACTIVITY,
        [
            _pack_field("<", F_TIMESTAMP, TYPE_UINT32, end_timestamp),
            _pack_field("<", 0, TYPE_UINT32, timer_ms),
            _pack_field("<", 1, TYPE_UINT16, 1),
            _pack_field("<", 2, TYPE_ENUM, 0),
            _pack_field("<", 3, TYPE_ENUM, 26),
            _pack_field("<", 4, TYPE_ENUM, 1),
            _pack_field("<", 5, TYPE_UINT32, _local_timestamp(end_timestamp)),
        ],
    )

    file_creator = next(
        (
            _dedupe(message)
            for message in messages
            if message.global_message == MSG_FILE_CREATOR
        ),
        None,
    )
    prefix = [file_id]
    if file_creator is not None:
        prefix.append(file_creator)
    prefix += [device_info, sport, event_start]

    preserved = [
        _dedupe(message)
        for message in messages
        if message.global_message
        not in {
            MSG_FILE_ID,
            MSG_FILE_CREATOR,
            MSG_DEVICE_INFO,
            MSG_SPORT,
            MSG_EVENT,
            MSG_RECORD,
            MSG_LAP,
            MSG_SESSION,
            MSG_ACTIVITY,
            MSG_FIELD_DESCRIPTION,
            MSG_DEVELOPER_DATA_ID,
        }
    ]
    return (
        prefix
        + preserved
        + records
        + [event_stop, normalized_lap, normalized_session, activity]
    )


def convert_fit_bytes(data: bytes) -> bytes:
    """Normalize a MyWhoosh or minimally-converted Edge 1050 FIT activity."""
    header, messages = _parse_fit(data)
    normalized = _encode_fit(header, _build_standard_messages(messages))
    _parse_fit(normalized)
    return normalized


def _output_path(source: Path) -> Path:
    first = source.with_name(f"{source.stem}_garmin.fit")
    if not first.exists():
        return first
    for number in range(2, 1000):
        candidate = source.with_name(f"{source.stem}_garmin_{number}.fit")
        if not candidate.exists():
            return candidate
    raise FitError(f"too many output files for {source.name}")


def _atomic_write(destination: Path, data: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=destination.name + ".", suffix=".tmp", dir=str(destination.parent)
    )
    try:
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def convert_file(path: str | os.PathLike[str]) -> Path:
    """Normalize one FIT file and write the result beside it."""
    source = Path(path)
    if not source.is_file():
        raise FitError(f"file not found: {source}")
    destination = _output_path(source)
    _atomic_write(destination, convert_fit_bytes(source.read_bytes()))
    return destination


def _has_console() -> bool:
    return sys.stdout is not None and sys.stderr is not None


def _show_message(success: bool, message: str) -> None:
    root = None
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        function = messagebox.showinfo if success else messagebox.showerror
        function("MyWhoosh to Garmin", message)
        return
    except Exception:
        stream = sys.stdout if success else sys.stderr
        if stream is not None:
            print(message, file=stream)
    finally:
        if root is not None:
            root.destroy()


def _pick_files() -> list[str]:
    root = None
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        return list(
            filedialog.askopenfilenames(
                title="Select MyWhoosh FIT files",
                filetypes=[("FIT files", "*.fit"), ("All files", "*.*")],
            )
        )
    except Exception:
        return []
    finally:
        if root is not None:
            root.destroy()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fit-fix",
        description="Normalize MyWhoosh FIT files for Garmin watches.",
    )
    parser.add_argument("files", nargs="*", help="one or more MyWhoosh .fit files")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    gui = not _has_console()

    try:
        arguments = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code) if isinstance(error.code, int) else 2

    files = list(arguments.files)
    if not files and gui:
        files = _pick_files()
        if not files:
            return 0
    if not files:
        parser.print_usage(sys.stderr)
        return 2

    results = []
    success = True
    for file_name in files:
        try:
            output = convert_file(file_name)
            results.append(f"OK: {output}")
        except Exception as error:
            success = False
            results.append(f"FAILED: {file_name}\n{type(error).__name__}: {error}")

    message = "\n\n".join(results)
    if gui:
        _show_message(success, message)
    else:
        print(message, file=sys.stdout if success else sys.stderr)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
