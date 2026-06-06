"""Minimal MyWhoosh to Garmin Edge 1050 FIT converter."""

from __future__ import annotations

import argparse
import os
import struct
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

__version__ = "3.0.0"

FIT_SIGNATURE = b".FIT"
FILE_ID_MESSAGE = 0
MANUFACTURER_FIELD = 1
PRODUCT_FIELD = 2
UINT16_BASE_TYPE = 0x04

MYWHOOSH_MANUFACTURER = 331
GARMIN_MANUFACTURER = 1
EDGE_1050_PRODUCT = 4440


class FitError(Exception):
    """Raised when a FIT file cannot be converted safely."""


@dataclass(frozen=True)
class Definition:
    global_message: int
    size: int
    endian: str
    fields: dict[int, tuple[int, int, int]]


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
    data: bytes | bytearray,
    position: int,
    body_end: int,
    has_developer_fields: bool,
) -> tuple[Definition, int]:
    _require_available(position, 5, body_end, "definition")
    position += 1  # reserved byte
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
    fields: dict[int, tuple[int, int, int]] = {}
    record_size = 0
    for _ in range(field_count):
        field_number, field_size, base_type = data[position : position + 3]
        fields[field_number] = (record_size, field_size, base_type & 0x1F)
        record_size += field_size
        position += 3

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
            record_size += data[position + 1]
            position += 3

    return Definition(global_message, record_size, endian, fields), position


def _find_file_id(
    data: bytes | bytearray,
) -> tuple[int, Definition, int]:
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
    file_id: Optional[tuple[int, Definition]] = None
    position = header_size

    while position < body_end:
        record_header = data[position]
        position += 1

        if record_header & 0x80:
            local_message = (record_header >> 5) & 0x03
            definition = definitions.get(local_message)
            if definition is None:
                raise FitError("compressed timestamp record has no definition")
            _require_available(position, definition.size, body_end, "data record")
            position += definition.size
            continue

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
        if definition.global_message == FILE_ID_MESSAGE and file_id is None:
            file_id = (position, definition)
        position += definition.size

    if file_id is None:
        raise FitError("file has no file_id message")
    return file_id[0], file_id[1], body_end


def _field_position(
    record_position: int,
    definition: Definition,
    field_number: int,
) -> int:
    field = definition.fields.get(field_number)
    if field is None or field[1] != 2 or field[2] != UINT16_BASE_TYPE:
        raise FitError(f"file_id field {field_number} is missing or malformed")
    return record_position + field[0]


def convert_fit_bytes(data: bytes) -> bytes:
    """Change only MyWhoosh's file_id identity to Garmin Edge 1050."""
    buffer = bytearray(data)
    record_position, definition, body_end = _find_file_id(buffer)
    manufacturer_position = _field_position(
        record_position, definition, MANUFACTURER_FIELD
    )
    product_position = _field_position(record_position, definition, PRODUCT_FIELD)

    manufacturer = struct.unpack_from(
        definition.endian + "H", buffer, manufacturer_position
    )[0]
    product = struct.unpack_from(definition.endian + "H", buffer, product_position)[0]

    if manufacturer == GARMIN_MANUFACTURER and product == EDGE_1050_PRODUCT:
        return data
    if manufacturer != MYWHOOSH_MANUFACTURER:
        raise FitError(f"not a MyWhoosh FIT file (manufacturer {manufacturer})")

    struct.pack_into(
        definition.endian + "H",
        buffer,
        manufacturer_position,
        GARMIN_MANUFACTURER,
    )
    struct.pack_into(
        definition.endian + "H", buffer, product_position, EDGE_1050_PRODUCT
    )
    struct.pack_into("<H", buffer, body_end, fit_crc(memoryview(buffer)[:body_end]))
    return bytes(buffer)


def _output_path(source: Path) -> Path:
    first = source.with_name(f"{source.stem}_edge1050{source.suffix}")
    if not first.exists():
        return first
    for number in range(2, 1000):
        candidate = source.with_name(
            f"{source.stem}_edge1050_{number}{source.suffix}"
        )
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
    """Convert one FIT file and write the result beside it."""
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
        function("MyWhoosh to Edge 1050", message)
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
        description="Convert MyWhoosh FIT files to Garmin Edge 1050 identity.",
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

    results: list[str] = []
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
