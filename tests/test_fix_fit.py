"""Focused tests for the minimal Edge 1050 conversion."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from fix_fit import (
    EDGE_1050_PRODUCT,
    GARMIN_MANUFACTURER,
    MYWHOOSH_MANUFACTURER,
    FitError,
    convert_file,
    convert_fit_bytes,
    fit_crc,
    main,
)


def _fit_file(
    manufacturer: int = MYWHOOSH_MANUFACTURER,
    product: int = 3570,
    *,
    header_size: int = 14,
    endian: str = "<",
) -> bytes:
    architecture = 0 if endian == "<" else 1
    definition = bytes(
        [
            0x40,
            0,
            architecture,
        ]
    )
    definition += struct.pack(endian + "H", 0)
    definition += bytes(
        [
            5,
            0,
            1,
            0x00,
            1,
            2,
            0x84,
            2,
            2,
            0x84,
            3,
            4,
            0x8C,
            4,
            4,
            0x86,
        ]
    )
    record = bytes([0])
    record += bytes([4])
    record += struct.pack(endian + "H", manufacturer)
    record += struct.pack(endian + "H", product)
    record += struct.pack(endian + "I", 123456789)
    record += struct.pack(endian + "I", 987654321)
    body = definition + record

    header = bytearray(header_size)
    header[0] = header_size
    header[1] = 0x20
    struct.pack_into("<H", header, 2, 1000)
    struct.pack_into("<I", header, 4, len(body))
    header[8:12] = b".FIT"
    if header_size == 14:
        struct.pack_into("<H", header, 12, fit_crc(header[:12]))

    output = bytes(header) + body
    return output + struct.pack("<H", fit_crc(output))


def _identity(data: bytes) -> tuple[int, int]:
    architecture = data[data[0] + 2]
    endian = "<" if architecture == 0 else ">"
    record_position = data[0] + 6 + (5 * 3) + 1
    return struct.unpack_from(endian + "HH", data, record_position + 1)


def test_conversion_changes_only_identity_and_file_crc() -> None:
    source = _fit_file()
    converted = convert_fit_bytes(source)

    assert _identity(converted) == (GARMIN_MANUFACTURER, EDGE_1050_PRODUCT)
    changed = {
        index
        for index, (before, after) in enumerate(zip(source, converted))
        if before != after
    }
    identity_start = source[0] + 6 + (5 * 3) + 2
    assert changed <= {
        identity_start,
        identity_start + 1,
        identity_start + 2,
        identity_start + 3,
        len(source) - 2,
        len(source) - 1,
    }
    stored_crc = struct.unpack_from("<H", converted, len(converted) - 2)[0]
    assert fit_crc(converted[:-2]) == stored_crc


@pytest.mark.parametrize("header_size", [12, 14])
@pytest.mark.parametrize("endian", ["<", ">"])
def test_supported_headers_and_architectures(header_size: int, endian: str) -> None:
    converted = convert_fit_bytes(_fit_file(header_size=header_size, endian=endian))
    assert _identity(converted) == (GARMIN_MANUFACTURER, EDGE_1050_PRODUCT)


def test_already_edge_1050_is_returned_unchanged() -> None:
    source = _fit_file(GARMIN_MANUFACTURER, EDGE_1050_PRODUCT)
    assert convert_fit_bytes(source) is source


def test_other_manufacturer_is_rejected() -> None:
    with pytest.raises(FitError, match="not a MyWhoosh"):
        convert_fit_bytes(_fit_file(1, 3121))


def test_bad_crc_is_rejected() -> None:
    source = bytearray(_fit_file())
    source[-1] ^= 0xFF
    with pytest.raises(FitError, match="file CRC mismatch"):
        convert_fit_bytes(bytes(source))


def test_trailing_data_is_rejected() -> None:
    with pytest.raises(FitError, match="trailing data"):
        convert_fit_bytes(_fit_file() + b"x")


def test_convert_file_preserves_source_and_avoids_collisions(tmp_path: Path) -> None:
    source = tmp_path / "ride.fit"
    original = _fit_file()
    source.write_bytes(original)

    first = convert_file(source)
    second = convert_file(source)

    assert source.read_bytes() == original
    assert first.name == "ride_edge1050.fit"
    assert second.name == "ride_edge1050_2.fit"
    assert _identity(first.read_bytes()) == (GARMIN_MANUFACTURER, EDGE_1050_PRODUCT)


def test_main_converts_multiple_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    first = tmp_path / "one.fit"
    second = tmp_path / "two.fit"
    first.write_bytes(_fit_file())
    second.write_bytes(_fit_file())

    assert main([str(first), str(second)]) == 0
    assert "one_edge1050.fit" in capsys.readouterr().out
    assert (tmp_path / "two_edge1050.fit").is_file()
