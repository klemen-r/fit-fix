"""Repair MyWhoosh FIT timestamp metadata."""

from __future__ import annotations

import argparse
import os
import struct
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import NamedTuple, Optional, Sequence

__version__ = "1.3.0"

FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)
FIT_SIGNATURE = b".FIT"
U32_INVALID = 0xFFFFFFFF
MAX_TZ_OFFSET = 15 * 3600
UNIX_FIT_EPOCH_OFFSET = 631_065_600

MSG_FILE_ID = 0
MSG_SESSION = 18
MSG_DEVICE_INFO = 23
MSG_ACTIVITY = 34

F_TIMESTAMP = 253
F_SESSION_START_TIME = 2
F_SESSION_TOTAL_ELAPSED = 7
F_SESSION_TOTAL_TIMER = 8
F_SESSION_SPORT = 5
F_SESSION_SUB_SPORT = 6
F_ACTIVITY_LOCAL_TS = 5
F_FILE_ID_MANUFACTURER = 1
F_FILE_ID_PRODUCT = 2
F_FILE_ID_SERIAL = 3
F_DEVICE_INDEX = 0
F_DEVICE_MANUFACTURER = 2
F_DEVICE_PRODUCT = 4
F_DEVICE_PRODUCT_NAME = 27

BT_UINT8 = 0x02
BT_ENUM = 0x00
BT_UINT16 = 0x04
BT_UINT32 = 0x06
BT_UINT32Z = 0x0C

ZWIFT_MANUFACTURER = 260
MYWHOOSH_MANUFACTURER = 331
GARMIN_MANUFACTURER = 1
EDGE_530_PRODUCT = 3121
SPORT_CYCLING = 2
SUB_SPORT_VIRTUAL_ACTIVITY = 58

_INTERESTING = frozenset({MSG_FILE_ID, MSG_SESSION, MSG_DEVICE_INFO, MSG_ACTIVITY})


def _crc_table() -> tuple[int, ...]:
    out = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
        out.append(c)
    return tuple(out)


_CRC = _crc_table()


def fit_crc(data, crc: int = 0) -> int:
    t = _CRC
    for b in data:
        crc = (crc >> 8) ^ t[(crc ^ b) & 0xFF]
    return crc


class FitError(Exception):
    pass


class _Def(NamedTuple):
    global_num: int
    size: int
    endian: str
    offsets: dict[int, tuple[int, int, int]]


class _Msg(NamedTuple):
    global_num: int
    endian: str
    offsets: dict[int, tuple[int, int, int]]
    end: int


@dataclass
class FixReport:
    end_utc: datetime
    end_local: datetime
    utc_offset: timedelta
    fields_patched: int
    messages_added: int
    sessions: int
    activities: int
    input_path: Optional[Path] = None
    output_path: Optional[Path] = None
    wrote_output: bool = False

    @property
    def was_already_correct(self) -> bool:
        return self.fields_patched == 0 and self.messages_added == 0


def _u32(buf, off: int, endian: str) -> int:
    return struct.unpack_from(endian + "I", buf, off)[0]


def _msg_at(definition: _Def, pos: int) -> _Msg:
    return _Msg(
        definition.global_num,
        definition.endian,
        {
            field_num: (pos + offset, size, base_type)
            for field_num, (offset, size, base_type) in definition.offsets.items()
        },
        pos + definition.size,
    )


def _walk(buf) -> tuple[list[_Msg], int, int, set[int]]:
    if len(buf) < 14:
        raise FitError(f"file too small ({len(buf)} bytes)")
    header_size = buf[0]
    if header_size not in (12, 14):
        raise FitError(f"invalid header size {header_size}")
    if buf[8:12] != FIT_SIGNATURE:
        raise FitError("missing .FIT signature")
    data_size = struct.unpack_from("<I", buf, 4)[0]
    body_end = header_size + data_size
    if body_end + 2 > len(buf):
        raise FitError(f"truncated file: needs {body_end + 2} bytes, has {len(buf)}")
    trailing = len(buf) - (body_end + 2)
    if trailing > 0:
        raise FitError(
            f"{trailing} bytes after first FIT file (chained FIT containers not supported)"
        )
    if header_size == 14:
        stored_header_crc = struct.unpack_from("<H", buf, 12)[0]
        if stored_header_crc and fit_crc(memoryview(buf)[:12]) != stored_header_crc:
            raise FitError("header CRC mismatch")
    stored_file_crc = struct.unpack_from("<H", buf, body_end)[0]
    if fit_crc(memoryview(buf)[:body_end]) != stored_file_crc:
        raise FitError("file CRC mismatch")

    defs: dict[int, _Def] = {}
    msgs: list[_Msg] = []
    used_local: set[int] = set()
    pos = header_size

    while pos < body_end:
        hdr = buf[pos]
        pos += 1
        if hdr & 0x80:
            local_mt = (hdr >> 5) & 0x03
            used_local.add(local_mt)
            d = defs.get(local_mt)
            if d is None:
                raise FitError(
                    f"compressed-ts data without def (local_mt={local_mt}) at {pos - 1}"
                )
            if pos + d.size > body_end:
                raise FitError(f"truncated compressed-ts data message at {pos - 1}")
            if d.global_num in _INTERESTING:
                msgs.append(_msg_at(d, pos))
            pos += d.size
            continue

        if hdr & 0x10:
            raise FitError(f"reserved record-header bit set at {pos - 1}")
        local_mt = hdr & 0x0F
        used_local.add(local_mt)
        is_def = bool(hdr & 0x40)
        has_dev = bool(hdr & 0x20)
        if has_dev and not is_def:
            raise FitError(f"reserved data-header bit set at {pos - 1}")
        if is_def:
            if pos + 5 > body_end:
                raise FitError(f"truncated definition at {pos - 1}")
            if buf[pos] != 0:
                raise FitError(f"invalid definition reserved byte at {pos}")
            pos += 1
            arch = buf[pos]
            pos += 1
            if arch not in (0, 1):
                raise FitError(f"invalid architecture byte 0x{arch:02x}")
            endian = "<" if arch == 0 else ">"
            global_num = struct.unpack_from(endian + "H", buf, pos)[0]
            pos += 2
            n = buf[pos]
            pos += 1
            if pos + n * 3 > body_end:
                raise FitError(f"truncated field list at {pos}")
            offs: dict = {}
            total = 0
            for _ in range(n):
                fnum, fsz, ftype = buf[pos], buf[pos + 1], buf[pos + 2]
                if fnum in offs:
                    raise FitError(f"duplicate field {fnum} at {pos}")
                offs[fnum] = (total, fsz, ftype & 0x1F)
                total += fsz
                pos += 3
            if has_dev:
                if pos >= body_end:
                    raise FitError(f"truncated dev field count at {pos}")
                nd = buf[pos]
                pos += 1
                if pos + nd * 3 > body_end:
                    raise FitError(f"truncated dev field list at {pos}")
                for _ in range(nd):
                    total += buf[pos + 1]
                    pos += 3
            defs[local_mt] = _Def(global_num, total, endian, offs)
        else:
            d = defs.get(local_mt)
            if d is None:
                raise FitError(f"data without def (local_mt={local_mt}) at {pos - 1}")
            if pos + d.size > body_end:
                raise FitError(f"truncated data message at {pos - 1}")
            if d.global_num in _INTERESTING:
                msgs.append(_msg_at(d, pos))
            pos += d.size

    if pos != body_end:
        raise FitError(f"body parser stopped at {pos}, expected {body_end}")
    return msgs, body_end, header_size, used_local


def _field(
    m: _Msg,
    field_num: int,
    size: int,
    base_type: int,
) -> Optional[tuple[int, int, int]]:
    field = m.offsets.get(field_num)
    if not field or field[1] != size or field[2] != base_type:
        return None
    return field


def _set_u16(buf, m: _Msg, field_num: int, value: int) -> int:
    off = _field(m, field_num, 2, BT_UINT16)
    if not off:
        return 0
    cur = struct.unpack_from(m.endian + "H", buf, off[0])[0]
    if cur == value:
        return 0
    struct.pack_into(m.endian + "H", buf, off[0], value)
    return 1


def _get_u8(
    buf,
    m: _Msg,
    field_num: int,
    base_type: int = BT_UINT8,
) -> Optional[int]:
    off = _field(m, field_num, 1, base_type)
    return buf[off[0]] if off else None


def _set_u32(
    buf,
    m: _Msg,
    field_num: int,
    value: int,
    base_type: int = BT_UINT32,
) -> int:
    off = _field(m, field_num, 4, base_type)
    if not off:
        return 0
    cur = struct.unpack_from(m.endian + "I", buf, off[0])[0]
    if cur == value:
        return 0
    struct.pack_into(m.endian + "I", buf, off[0], value)
    return 1


def _anchors(m: _Msg, buf) -> Optional[tuple[int, int]]:
    start = _field(m, F_SESSION_START_TIME, 4, BT_UINT32)
    if not start:
        return None
    s = _u32(buf, start[0], m.endian)
    if s in (0, U32_INVALID):
        return None
    e = None
    for field_num in (F_SESSION_TOTAL_ELAPSED, F_SESSION_TOTAL_TIMER):
        elapsed = _field(m, field_num, 4, BT_UINT32)
        if elapsed:
            value = _u32(buf, elapsed[0], m.endian)
            if value != U32_INVALID:
                e = value
                break
    if e is None:
        return None
    end = s + e // 1000
    if end >= U32_INVALID:
        raise FitError("session end timestamp out of range")
    return s, end


def _has_unix_epoch_local_timestamp(m: _Msg, buf, activity_end: int) -> bool:
    local = _field(m, F_ACTIVITY_LOCAL_TS, 4, BT_UINT32)
    if not local:
        return False
    local_value = _u32(buf, local[0], m.endian)
    if local_value in (0, U32_INVALID):
        return False
    return (
        abs((int(local_value) - int(activity_end)) - UNIX_FIT_EPOCH_OFFSET)
        <= MAX_TZ_OFFSET
    )


def _creator_device_info_record(
    local_mt: int,
    manufacturer: int,
    product: int,
    product_name: str,
) -> bytes:
    encoded_name = product_name.encode("ascii") + b"\0"
    if len(encoded_name) > 255:
        raise FitError("creator product name is too long")
    return bytes(
        [
            0x40 | local_mt,
            0,
            0,
            MSG_DEVICE_INFO,
            0,
            4,
            F_DEVICE_INDEX,
            1,
            0x02,
            F_DEVICE_MANUFACTURER,
            2,
            0x84,
            F_DEVICE_PRODUCT,
            2,
            0x84,
            F_DEVICE_PRODUCT_NAME,
            len(encoded_name),
            0x07,
            local_mt,
            0,
        ]
    ) + struct.pack("<HH", manufacturer, product) + encoded_name


def _add_creator_device(
    buf: bytearray,
    insert_at: int,
    body_end: int,
    header_size: int,
    local_mt: int,
    manufacturer: int,
    product: int,
    product_name: str,
) -> int:
    record = _creator_device_info_record(
        local_mt, manufacturer, product, product_name
    )
    buf[insert_at:insert_at] = record
    new_body_end = body_end + len(record)
    data_size = struct.unpack_from("<I", buf, 4)[0] + len(record)
    struct.pack_into("<I", buf, 4, data_size)
    if header_size == 14 and struct.unpack_from("<H", buf, 12)[0]:
        struct.pack_into("<H", buf, 12, fit_crc(memoryview(buf)[:12]))
    struct.pack_into("<H", buf, new_body_end, fit_crc(memoryview(buf)[:new_body_end]))
    return new_body_end


def fix_fit_bytes(
    data: bytes,
    tz: Optional[tzinfo] = None,
    *,
    mimic_zwift: bool = False,
    mimic_garmin: bool = False,
) -> tuple[bytes, FixReport]:
    if mimic_zwift and mimic_garmin:
        raise FitError("--mimic-zwift and --mimic-garmin cannot be used together")

    buf = bytearray(data)
    msgs, body_end, header_size, used_local = _walk(buf)
    file_ids = [m for m in msgs if m.global_num == MSG_FILE_ID]
    sessions = [m for m in msgs if m.global_num == MSG_SESSION]
    device_infos = [m for m in msgs if m.global_num == MSG_DEVICE_INFO]
    activities = [m for m in msgs if m.global_num == MSG_ACTIVITY]
    if not sessions:
        raise FitError("no session message")
    if not activities:
        raise FitError("no activity message")

    anchors: list[tuple[int, int]] = []
    for session in sessions:
        anchor = _anchors(session, buf)
        if anchor is None:
            raise FitError("session missing start_time or elapsed/timer time")
        anchors.append(anchor)

    activity_end = max(end for _, end in anchors)
    end_utc = FIT_EPOCH + timedelta(seconds=activity_end)
    aware = end_utc.astimezone(tz) if tz else end_utc.astimezone()
    offset = aware.utcoffset() or timedelta(0)
    local_end = activity_end + int(offset.total_seconds())
    if not 0 <= local_end < U32_INVALID:
        raise FitError("local end timestamp out of range")
    broken_activities = [
        activity
        for activity in activities
        if _has_unix_epoch_local_timestamp(activity, buf, activity_end)
    ]

    patches = 0
    messages_added = 0
    if broken_activities:
        for session, (_, end_v) in zip(sessions, anchors):
            patches += _set_u32(buf, session, F_TIMESTAMP, end_v)

    for activity in broken_activities:
        patches += _set_u32(buf, activity, F_TIMESTAMP, activity_end)
        patches += _set_u32(buf, activity, F_ACTIVITY_LOCAL_TS, local_end)

    target = None
    if mimic_zwift:
        target = (ZWIFT_MANUFACTURER, 0, "Zwift", True)
    elif mimic_garmin:
        for session in sessions:
            sport = _get_u8(buf, session, F_SESSION_SPORT, BT_ENUM)
            sub_sport = _get_u8(buf, session, F_SESSION_SUB_SPORT, BT_ENUM)
            if sport != SPORT_CYCLING or sub_sport != SUB_SPORT_VIRTUAL_ACTIVITY:
                raise FitError(
                    "--mimic-garmin only accepts cycling / virtual_activity files"
                )
        target = (GARMIN_MANUFACTURER, EDGE_530_PRODUCT, "Edge 530", False)

    if target is not None:
        target_manufacturer, target_product, target_name, clear_serial = target
        add_creator_at = None
        for fi in file_ids:
            manufacturer = _field(fi, F_FILE_ID_MANUFACTURER, 2, BT_UINT16)
            if (
                manufacturer
                and struct.unpack_from(fi.endian + "H", buf, manufacturer[0])[0]
                == MYWHOOSH_MANUFACTURER
            ):
                add_creator_at = fi.end
                patches += _set_u16(
                    buf, fi, F_FILE_ID_MANUFACTURER, target_manufacturer
                )
                patches += _set_u16(buf, fi, F_FILE_ID_PRODUCT, target_product)
                if clear_serial:
                    patches += _set_u32(buf, fi, F_FILE_ID_SERIAL, 0, BT_UINT32Z)

        has_creator = False
        if add_creator_at is not None:
            for device in device_infos:
                if _get_u8(buf, device, F_DEVICE_INDEX) != 0:
                    continue
                has_creator = True
                manufacturer = _field(device, F_DEVICE_MANUFACTURER, 2, BT_UINT16)
                if (
                    manufacturer
                    and struct.unpack_from(device.endian + "H", buf, manufacturer[0])[0]
                    == MYWHOOSH_MANUFACTURER
                ):
                    patches += _set_u16(
                        buf, device, F_DEVICE_MANUFACTURER, target_manufacturer
                    )
                    patches += _set_u16(
                        buf, device, F_DEVICE_PRODUCT, target_product
                    )

        if add_creator_at is not None and not has_creator:
            local_mt = next((i for i in range(16) if i not in used_local), None)
            if local_mt is None:
                local_mt = 15
                add_creator_at = body_end
            body_end = _add_creator_device(
                buf,
                add_creator_at,
                body_end,
                header_size,
                local_mt,
                target_manufacturer,
                target_product,
                target_name,
            )
            messages_added = 1

    if patches and not messages_added:
        struct.pack_into("<H", buf, body_end, fit_crc(memoryview(buf)[:body_end]))
        out = bytes(buf)
    elif messages_added:
        out = bytes(buf)
    else:
        out = data if isinstance(data, bytes) else bytes(data)

    return out, FixReport(
        end_utc=end_utc,
        end_local=aware,
        utc_offset=offset,
        fields_patched=patches,
        messages_added=messages_added,
        sessions=len(sessions),
        activities=len(activities),
    )


def _atomic_write(dst: Path, data: bytes) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=dst.name + ".", suffix=".tmp", dir=str(dst.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, dst)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _resolve_output(src: Path, output, in_place: bool, overwrite: bool) -> Path:
    if in_place and output is not None:
        raise FitError("--output and --in-place cannot be used together")
    if in_place:
        return src
    if output is not None:
        dst = Path(output)
        try:
            same_file = dst.exists() and os.path.samefile(src, dst)
        except OSError:
            same_file = src.resolve() == dst.resolve()
        if same_file:
            raise FitError("output is the input file; use --in-place")
        if dst.exists() and not overwrite:
            raise FitError(f"output already exists: {dst} (use --overwrite)")
        return dst
    dst = src.with_name(src.stem + "_fixed" + src.suffix)
    if not dst.exists() or overwrite or dst == src:
        return dst
    base = dst.stem[: -len("_fixed")] if dst.stem.endswith("_fixed") else dst.stem
    for i in range(2, 1000):
        cand = dst.with_name(f"{base}_fixed_{i}{dst.suffix}")
        if not cand.exists():
            return cand
    raise FitError(f"too many output collisions for {src.name}")


def fix_fit(
    input_path,
    output_path=None,
    *,
    tz: Optional[tzinfo] = None,
    overwrite: bool = False,
    in_place: bool = False,
    write_when_unchanged: bool = False,
    mimic_zwift: bool = False,
    mimic_garmin: bool = False,
) -> FixReport:
    src = Path(input_path)
    if not src.is_file():
        raise FitError(f"input file not found: {src}")
    if in_place and output_path is not None:
        raise FitError("--output and --in-place cannot be used together")
    fixed, report = fix_fit_bytes(
        src.read_bytes(),
        tz=tz,
        mimic_zwift=mimic_zwift,
        mimic_garmin=mimic_garmin,
    )
    report.input_path = src
    if report.was_already_correct and not write_when_unchanged and output_path is None:
        report.output_path = src
        return report
    dst = _resolve_output(src, output_path, in_place, overwrite)
    _atomic_write(dst, fixed)
    report.output_path = dst
    report.wrote_output = True
    return report


def _format_report(r: FixReport) -> str:
    name = r.input_path.name if r.input_path else "<unknown>"
    if r.was_already_correct and not r.wrote_output:
        return f"SKIP  {name}\n      (already correct)"
    out_name = r.output_path.name if r.output_path else "<unknown>"
    if r.was_already_correct:
        return f"COPY  {name}\n      -> {out_name}\n      (already correct)"
    secs = int(r.utc_offset.total_seconds())
    sign = "-" if secs < 0 else "+"
    h, m = divmod(abs(secs) // 60, 60)
    return (
        f"OK    {name}\n"
        f"      -> {out_name}\n"
        f"      end (UTC):   {r.end_utc:%Y-%m-%d %H:%M:%S}\n"
        f"      end (local): {r.end_local:%Y-%m-%d %H:%M:%S}  (UTC{sign}{h:02d}:{m:02d})\n"
        f"      fields patched: {r.fields_patched}\n"
        f"      messages added: {r.messages_added}"
    )


def _has_console() -> bool:
    return sys.stdout is not None or sys.stderr is not None


def _gui_notify(ok: bool, body: str) -> None:
    root = None
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        (messagebox.showinfo if ok else messagebox.showerror)("fit-fix", body)
        return
    except Exception:
        pass
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass
    stream = sys.stdout if ok else sys.stderr
    if stream is not None:
        try:
            print(body, file=stream)
        except Exception:
            pass


def _gui_pick_files() -> Optional[list[str]]:
    root = None
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        chosen = filedialog.askopenfilenames(
            title="Select .fit file(s)",
            filetypes=[("FIT files", "*.fit"), ("All files", "*.*")],
        )
        return list(chosen)
    except Exception:
        return None
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fit-fix",
        description="Repair MyWhoosh FIT metadata for Garmin Connect.",
    )
    p.add_argument("files", nargs="*", help="one or more .fit files")
    p.add_argument("-o", "--output", help="output path (single input only)")
    p.add_argument("--in-place", action="store_true", help="overwrite input")
    p.add_argument("--overwrite", action="store_true", help="overwrite output if it exists")
    p.add_argument("--write-when-unchanged", action="store_true",
                   help="write a copy even if input is already correct")
    p.add_argument("--utc", action="store_true", help="use UTC for local_timestamp")
    p.add_argument("--mimic-zwift", action="store_true",
                   help="rewrite MyWhoosh file_id as Zwift (260)")
    p.add_argument("--mimic-garmin", action="store_true",
                   help="rewrite virtual MyWhoosh creator as Garmin Edge 530")
    p.add_argument("--no-gui", action="store_true", help="never show popups")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_arg_parser()
    use_gui = not _has_console()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        code = int(e.code) if isinstance(e.code, int) else 2
        if use_gui and code != 0:
            _gui_notify(False, "Invalid arguments.")
        return code
    except Exception as e:
        if use_gui:
            _gui_notify(False, f"Argument parsing failed: {e}")
        return 2

    use_gui = use_gui and not args.no_gui
    files = list(args.files)
    if not files and use_gui:
        chosen = _gui_pick_files()
        if chosen is None:
            _gui_notify(False, "Could not open the file picker.")
            return 1
        if not chosen:
            return 0
        files = chosen
    if not files:
        if use_gui:
            _gui_notify(False, "No files selected.\n\nDrag .fit files onto the launcher.")
        elif sys.stderr is not None:
            try:
                parser.print_usage(sys.stderr)
            except Exception:
                pass
        return 2

    if args.output and len(files) != 1:
        msg = "--output requires exactly one input file"
        if use_gui:
            _gui_notify(False, msg)
        elif sys.stderr is not None:
            print(msg, file=sys.stderr)
        return 2
    if args.output and args.in_place:
        msg = "--output and --in-place cannot be used together"
        if use_gui:
            _gui_notify(False, msg)
        elif sys.stderr is not None:
            print(msg, file=sys.stderr)
        return 2
    if args.mimic_zwift and args.mimic_garmin:
        msg = "--mimic-zwift and --mimic-garmin cannot be used together"
        if use_gui:
            _gui_notify(False, msg)
        elif sys.stderr is not None:
            print(msg, file=sys.stderr)
        return 2

    tz = timezone.utc if args.utc else None
    lines: list[str] = []
    ok = True
    for path in files:
        try:
            r = fix_fit(
                path,
                output_path=args.output,
                tz=tz,
                overwrite=args.overwrite,
                in_place=args.in_place,
                write_when_unchanged=args.write_when_unchanged,
                mimic_zwift=args.mimic_zwift,
                mimic_garmin=args.mimic_garmin,
            )
            lines.append(_format_report(r))
        except Exception as e:
            ok = False
            name = os.path.basename(str(path))
            lines.append(f"FAIL  {name}\n      {type(e).__name__}: {e}")

    body = "\n\n".join(lines)
    if use_gui:
        _gui_notify(ok, body)
    else:
        stream = sys.stdout if ok else sys.stderr
        if stream is not None:
            try:
                print(body, file=stream)
            except Exception:
                pass
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
