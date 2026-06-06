"""Tests for Garmin-compatible MyWhoosh normalization."""

from __future__ import annotations

import struct
from collections import Counter
from pathlib import Path

import pytest

from fix_fit import (
    EDGE_1050_PRODUCT,
    GARMIN_MANUFACTURER,
    MSG_ACTIVITY,
    MSG_DEVELOPER_DATA_ID,
    MSG_DEVICE_INFO,
    MSG_EVENT,
    MSG_FIELD_DESCRIPTION,
    MSG_FILE_CREATOR,
    MSG_FILE_ID,
    MSG_LAP,
    MSG_RECORD,
    MSG_SESSION,
    MSG_SPORT,
    MYWHOOSH_MANUFACTURER,
    TYPE_ENUM,
    TYPE_SINT32,
    TYPE_UINT8,
    TYPE_UINT16,
    TYPE_UINT32,
    TYPE_UINT32Z,
    DeveloperField,
    FitError,
    Message,
    _encode_fit,
    _pack_field,
    _parse_fit,
    _read_uint,
    convert_file,
    convert_fit_bytes,
    main,
)

START = 1_100_000_000
END = START + 3


def _header(size: int = 14) -> bytes:
    header = bytearray(size)
    header[0] = size
    header[1] = 0x20
    struct.pack_into("<H", header, 2, 21166)
    header[8:12] = b".FIT"
    return bytes(header)


def _m(global_message: int, specs: list[tuple[int, int, int]], endian: str) -> Message:
    return Message(
        global_message,
        endian,
        tuple(_pack_field(endian, number, base_type, value) for number, base_type, value in specs),
    )


def _broken_activity(
    *,
    manufacturer: int = MYWHOOSH_MANUFACTURER,
    product: int = 3570,
    endian: str = "<",
    header_size: int = 14,
    redundant_enhanced_fields: bool = True,
    lap_timestamp: int = END,
    summary_timestamp: int = START,
    local_timestamp: int = START + 631_065_600,
) -> bytes:
    messages = [
        _m(
            MSG_FILE_ID,
            [
                (0, TYPE_ENUM, 4),
                (1, TYPE_UINT16, manufacturer),
                (2, TYPE_UINT16, product),
                (3, TYPE_UINT32Z, 123456789),
                (4, TYPE_UINT32, START),
            ],
            endian,
        ),
        _m(MSG_FILE_CREATOR, [(0, TYPE_UINT16, 29)], endian),
        _m(MSG_DEVELOPER_DATA_ID, [(3, TYPE_UINT8, 0)], endian),
        _m(MSG_FIELD_DESCRIPTION, [(0, TYPE_UINT8, 0)], endian),
        _m(MSG_EVENT, [(253, TYPE_UINT32, START)], endian),
    ]
    for index in range(3):
        fields = [
            (253, TYPE_UINT32, START + index),
            (3, TYPE_UINT8, 100 + index),
            (5, TYPE_UINT32, 1000 + index * 500),
            (4, TYPE_UINT8, 80 + index),
            (2, TYPE_UINT16, 2500 + index),
            (0, TYPE_SINT32, 500_000_000 + index),
            (1, TYPE_SINT32, 100_000_000 + index),
            (7, TYPE_UINT16, 150 + index),
            (6, TYPE_UINT16, 8000 + index),
        ]
        if redundant_enhanced_fields:
            fields += [(78, TYPE_UINT32, 2500 + index), (73, TYPE_UINT32, 8000 + index)]
        messages.append(_m(MSG_RECORD, fields, endian))

    messages += [
        _m(
            MSG_LAP,
            [
                (2, TYPE_UINT32, START),
                (253, TYPE_UINT32, lap_timestamp),
                (8, TYPE_UINT32, 3000),
                (7, TYPE_UINT32, 3000),
                (23, TYPE_ENUM, 0),
                (25, TYPE_ENUM, 2),
                (39, TYPE_ENUM, 58),
                (24, TYPE_ENUM, 7),
            ],
            endian,
        ),
        _m(
            MSG_EVENT,
            [(253, TYPE_UINT32, END), (0, TYPE_ENUM, 8), (1, TYPE_ENUM, 9)],
            endian,
        ),
        Message(
            MSG_SESSION,
            endian,
            tuple(
                _pack_field(endian, number, base_type, value)
                for number, base_type, value in [
                    (253, TYPE_UINT32, summary_timestamp),
                    (2, TYPE_UINT32, START),
                    (8, TYPE_UINT32, 3000),
                    (7, TYPE_UINT32, 3000),
                    (5, TYPE_ENUM, 2),
                    (6, TYPE_ENUM, 58),
                    (11, TYPE_UINT16, 30),
                    (26, TYPE_UINT16, 1),
                    (22, TYPE_UINT16, 7),
                    (18, TYPE_UINT8, 81),
                    (16, TYPE_UINT8, 101),
                    (19, TYPE_UINT8, 82),
                    (20, TYPE_UINT16, 151),
                    (21, TYPE_UINT16, 152),
                    (14, TYPE_UINT16, 8001),
                    (17, TYPE_UINT8, 102),
                    (9, TYPE_UINT32, 2000),
                ]
            ),
            (DeveloperField(0, 4, 0, b"test"),),
        ),
        _m(
            MSG_ACTIVITY,
            [
                (253, TYPE_UINT32, summary_timestamp),
                (0, TYPE_UINT32, 3000),
                (1, TYPE_UINT16, 1),
                (2, TYPE_ENUM, 0),
                (3, TYPE_ENUM, 26),
                (4, TYPE_ENUM, 1),
                (5, TYPE_UINT32, local_timestamp),
            ],
            endian,
        ),
    ]
    return _encode_fit(_header(header_size), messages)


def _messages(data: bytes) -> list[Message]:
    return _parse_fit(data)[1]


def _one(messages: list[Message], global_message: int) -> Message:
    return next(message for message in messages if message.global_message == global_message)


def test_normalizes_watch_facing_structure() -> None:
    normalized = convert_fit_bytes(_broken_activity())
    messages = _messages(normalized)
    counts = Counter(message.global_message for message in messages)

    assert counts == {
        MSG_FILE_ID: 1,
        MSG_FILE_CREATOR: 1,
        MSG_DEVICE_INFO: 1,
        MSG_SPORT: 1,
        MSG_EVENT: 2,
        MSG_RECORD: 3,
        MSG_LAP: 1,
        MSG_SESSION: 1,
        MSG_ACTIVITY: 1,
    }
    file_id = _one(messages, MSG_FILE_ID)
    assert _read_uint(file_id, 1) == GARMIN_MANUFACTURER
    assert _read_uint(file_id, 2) == EDGE_1050_PRODUCT

    events = [message for message in messages if message.global_message == MSG_EVENT]
    assert [(_read_uint(message, 0), _read_uint(message, 1)) for message in events] == [
        (0, 0),
        (0, 4),
    ]
    assert [_read_uint(message, 253) for message in events] == [START, END]

    lap = _one(messages, MSG_LAP)
    session = _one(messages, MSG_SESSION)
    activity = _one(messages, MSG_ACTIVITY)
    assert _read_uint(lap, 253) == END
    assert _read_uint(session, 253) == END
    assert _read_uint(activity, 253) == END
    assert _read_uint(lap, 9) == 2000
    assert _read_uint(lap, 11) == 30
    assert _read_uint(lap, 19) == 151
    assert _read_uint(session, 15) == 8002
    assert abs(_read_uint(activity, 5) - END) <= 15 * 60 * 60
    assert not session.developer_fields


def test_keeps_all_mywhoosh_time_fixes() -> None:
    poisoned = _broken_activity(
        lap_timestamp=START + 631_065_600,
        summary_timestamp=START,
        local_timestamp=END + 631_065_600,
    )
    original_records = [
        _read_uint(message, 253)
        for message in _messages(poisoned)
        if message.global_message == MSG_RECORD
    ]
    messages = _messages(convert_fit_bytes(poisoned))

    fixed_records = [
        _read_uint(message, 253)
        for message in messages
        if message.global_message == MSG_RECORD
    ]
    events = [message for message in messages if message.global_message == MSG_EVENT]
    lap = _one(messages, MSG_LAP)
    session = _one(messages, MSG_SESSION)
    activity = _one(messages, MSG_ACTIVITY)

    assert fixed_records == original_records
    assert [_read_uint(message, 253) for message in events] == [START, END]
    assert _read_uint(lap, 253) == END
    assert _read_uint(session, 253) == END
    assert _read_uint(activity, 253) == END
    assert abs(_read_uint(activity, 5) - END) <= 15 * 60 * 60
    assert _read_uint(activity, 5) != END + 631_065_600


def test_preserves_records_and_removes_redundant_enhanced_fields() -> None:
    source = _messages(_broken_activity())
    normalized = _messages(convert_fit_bytes(_broken_activity()))
    before = [message for message in source if message.global_message == MSG_RECORD]
    after = [message for message in normalized if message.global_message == MSG_RECORD]

    assert len(before) == len(after) == 3
    for original, fixed in zip(before, after):
        for field_number in (253, 3, 5, 4, 2, 0, 1, 7, 6):
            assert original.first(field_number).data == fixed.first(field_number).data
        assert fixed.first(78) is None
        assert fixed.first(73) is None


@pytest.mark.parametrize("header_size", [12, 14])
@pytest.mark.parametrize("endian", ["<", ">"])
def test_supported_headers_and_architectures(header_size: int, endian: str) -> None:
    normalized = convert_fit_bytes(
        _broken_activity(header_size=header_size, endian=endian)
    )
    assert _read_uint(_one(_messages(normalized), MSG_FILE_ID), 2) == EDGE_1050_PRODUCT


def test_normalization_is_idempotent() -> None:
    once = convert_fit_bytes(_broken_activity())
    assert convert_fit_bytes(once) == once


def test_other_garmin_product_is_rejected() -> None:
    with pytest.raises(FitError, match="not an Edge 1050"):
        convert_fit_bytes(_broken_activity(manufacturer=GARMIN_MANUFACTURER, product=3121))


def test_bad_crc_is_rejected() -> None:
    source = bytearray(_broken_activity())
    source[-1] ^= 0xFF
    with pytest.raises(FitError, match="file CRC mismatch"):
        convert_fit_bytes(bytes(source))


def test_convert_file_preserves_source_and_avoids_collisions(tmp_path: Path) -> None:
    source = tmp_path / "ride.fit"
    original = _broken_activity()
    source.write_bytes(original)

    first = convert_file(source)
    second = convert_file(source)

    assert source.read_bytes() == original
    assert first.name == "ride_garmin.fit"
    assert second.name == "ride_garmin_2.fit"


def test_main_converts_multiple_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    first = tmp_path / "one.fit"
    second = tmp_path / "two.fit"
    first.write_bytes(_broken_activity())
    second.write_bytes(_broken_activity())

    assert main([str(first), str(second)]) == 0
    assert "one_garmin.fit" in capsys.readouterr().out
    assert (tmp_path / "two_garmin.fit").is_file()
