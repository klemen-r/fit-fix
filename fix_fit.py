"""Repair MyWhoosh FIT timestamp metadata."""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import NamedTuple, Optional, Sequence

__version__ = "2.0.1"

FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)
FIT_SIGNATURE = b".FIT"
U32_INVALID = 0xFFFFFFFF
MAX_TZ_OFFSET = 15 * 3600
UNIX_FIT_EPOCH_OFFSET = 631_065_600

MSG_FILE_ID = 0
MSG_SESSION = 18
MSG_DEVICE_INFO = 23
MSG_ACTIVITY = 34
MSG_FILE_CREATOR = 49
MSG_RECORD = 20

F_TIMESTAMP = 253
F_SESSION_START_TIME = 2
F_SESSION_TOTAL_ELAPSED = 7
F_SESSION_TOTAL_TIMER = 8
F_SESSION_SPORT = 5
F_SESSION_SUB_SPORT = 6
F_SESSION_NORMALIZED_POWER = 34
F_SESSION_TSS = 35
F_SESSION_IF = 36
F_ACTIVITY_LOCAL_TS = 5
F_FILE_ID_MANUFACTURER = 1
F_FILE_ID_PRODUCT = 2
F_FILE_ID_SERIAL = 3
F_DEVICE_INDEX = 0
F_DEVICE_MANUFACTURER = 2
F_DEVICE_PRODUCT = 4
F_DEVICE_PRODUCT_NAME = 27
F_FILE_CREATOR_SOFTWARE_VERSION = 0
F_RECORD_POSITION_LAT = 0
F_RECORD_POSITION_LONG = 1
F_RECORD_ALTITUDE = 2
F_RECORD_HEART_RATE = 3
F_RECORD_CADENCE = 4
F_RECORD_DISTANCE = 5
F_RECORD_SPEED = 6
F_RECORD_POWER = 7

F_SESSION_TOTAL_DISTANCE = 9
F_SESSION_TOTAL_CALORIES = 11
F_SESSION_AVG_HR = 16
F_SESSION_MAX_HR = 17
F_SESSION_AVG_CADENCE = 18
F_SESSION_MAX_CADENCE = 19
F_SESSION_AVG_POWER = 20
F_SESSION_MAX_POWER = 21
F_SESSION_TOTAL_ASCENT = 22

F_LAP_TIMESTAMP = 253
F_LAP_START_TIME = 2

F_FILE_ID_TIME_CREATED = 4
F_FILE_ID_TYPE = 0

F_ACTIVITY_TYPE = 2
F_ACTIVITY_EVENT = 3
F_ACTIVITY_EVENT_TYPE = 4

BT_UINT8 = 0x02
BT_ENUM = 0x00
BT_UINT16 = 0x04
BT_UINT32 = 0x06
BT_UINT32Z = 0x0C

ZWIFT_MANUFACTURER = 260
MYWHOOSH_MANUFACTURER = 331
GARMIN_MANUFACTURER = 1
TACX_MANUFACTURER = 89
ROUVY_MANUFACTURER = 267
WAHOO_MANUFACTURER = 32

EDGE_530_PRODUCT = 3121
EDGE_1030_PLUS_PRODUCT = 3570
FR_265_PRODUCT = 4257
EDGE_530_SOFTWARE_VERSION = 1140

SPORT_CYCLING = 2
SPORT_RUNNING = 1
SUB_SPORT_INDOOR_CYCLING = 6
SUB_SPORT_VIRTUAL_ACTIVITY = 58

MANUFACTURER_NAMES = {
    1: "garmin",
    32: "wahoo_fitness",
    89: "tacx",
    260: "zwift",
    263: "trainerroad",
    264: "the_sufferfest",
    266: "bkool",
    267: "rouvy",
    331: "mywhoosh",
}

SPORT_NAMES = {0: "generic", 1: "running", 2: "cycling", 4: "fitness_equipment", 5: "swimming"}
SUB_SPORT_NAMES = {
    0: "generic", 6: "indoor_cycling", 7: "road", 8: "mountain", 10: "recumbent",
    20: "strength_training", 26: "cardio_training", 45: "indoor_running",
    46: "gravel_cycling", 58: "virtual_activity",
}


@dataclass(frozen=True)
class Profile:
    name: str
    manufacturer: int
    product: int
    product_name: str
    software_version: int
    clear_serial: bool
    add_creator_device_info: bool


PROFILES: dict[str, Profile] = {
    "garmin-edge": Profile(
        name="garmin-edge",
        manufacturer=GARMIN_MANUFACTURER,
        product=EDGE_530_PRODUCT,
        product_name="Edge 530",
        software_version=EDGE_530_SOFTWARE_VERSION,
        clear_serial=False,
        add_creator_device_info=True,
    ),
    "garmin-edge-1030": Profile(
        name="garmin-edge-1030",
        manufacturer=GARMIN_MANUFACTURER,
        product=EDGE_1030_PLUS_PRODUCT,
        product_name="Edge 1030 Plus",
        software_version=1140,
        clear_serial=False,
        add_creator_device_info=True,
    ),
    "garmin-forerunner": Profile(
        name="garmin-forerunner",
        manufacturer=GARMIN_MANUFACTURER,
        product=FR_265_PRODUCT,
        product_name="Forerunner 265",
        software_version=900,
        clear_serial=False,
        add_creator_device_info=True,
    ),
    "zwift": Profile(
        name="zwift",
        manufacturer=ZWIFT_MANUFACTURER,
        product=0,
        product_name="Zwift",
        software_version=0,
        clear_serial=True,
        add_creator_device_info=True,
    ),
    "rouvy": Profile(
        name="rouvy",
        manufacturer=ROUVY_MANUFACTURER,
        product=0,
        product_name="Rouvy",
        software_version=0,
        clear_serial=True,
        add_creator_device_info=True,
    ),
    "tacx": Profile(
        name="tacx",
        manufacturer=TACX_MANUFACTURER,
        product=0,
        product_name="Tacx Training",
        software_version=0,
        clear_serial=True,
        add_creator_device_info=True,
    ),
}

NP_WINDOW_SECONDS = 30

_INTERESTING = frozenset({MSG_FILE_ID, MSG_SESSION, MSG_DEVICE_INFO, MSG_ACTIVITY, MSG_FILE_CREATOR})


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


@dataclass
class _DefRange:
    local_mt: int
    global_num: int
    start: int
    end: int
    field_count_pos: int
    field_descriptors_end: int
    record_size: int
    fields: tuple
    endian: str


@dataclass
class _DataRange:
    local_mt: int
    global_num: int
    start: int
    end: int
    regular_end: int
    endian: str
    offsets: dict


def _walk_detailed(buf) -> tuple[list[_DefRange], list[_DataRange], int, int]:
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

    defs_by_local: dict[int, _DefRange] = {}
    defs: list[_DefRange] = []
    datas: list[_DataRange] = []
    pos = header_size

    while pos < body_end:
        msg_start = pos
        hdr = buf[pos]
        pos += 1
        if hdr & 0x80:
            local_mt = (hdr >> 5) & 0x03
            d = defs_by_local.get(local_mt)
            if d is None:
                raise FitError(f"compressed-ts data without def at {pos - 1}")
            data_end = pos + d.record_size
            if data_end > body_end:
                raise FitError(f"truncated compressed-ts data at {pos - 1}")
            offs: dict = {}
            o = pos
            for fnum, fsz, ftype in d.fields:
                offs[fnum] = (o, fsz, ftype)
                o += fsz
            datas.append(_DataRange(local_mt, d.global_num, msg_start, data_end, data_end, d.endian, offs))
            pos = data_end
            continue

        local_mt = hdr & 0x0F
        is_def = bool(hdr & 0x40)
        has_dev = bool(hdr & 0x20)
        if is_def:
            if pos + 5 > body_end:
                raise FitError(f"truncated definition at {pos - 1}")
            pos += 1
            arch = buf[pos]
            pos += 1
            if arch not in (0, 1):
                raise FitError(f"invalid architecture byte 0x{arch:02x}")
            endian = "<" if arch == 0 else ">"
            global_num = struct.unpack_from(endian + "H", buf, pos)[0]
            pos += 2
            field_count_pos = pos
            n = buf[pos]
            pos += 1
            if pos + n * 3 > body_end:
                raise FitError(f"truncated field list at {pos}")
            fields_list = []
            total_regular = 0
            for _ in range(n):
                fnum, fsz, ftype = buf[pos], buf[pos + 1], buf[pos + 2]
                fields_list.append((fnum, fsz, ftype & 0x1F))
                total_regular += fsz
                pos += 3
            field_descriptors_end = pos
            total_total = total_regular
            if has_dev:
                if pos >= body_end:
                    raise FitError(f"truncated dev field count at {pos}")
                nd = buf[pos]
                pos += 1
                if pos + nd * 3 > body_end:
                    raise FitError(f"truncated dev field list at {pos}")
                for _ in range(nd):
                    total_total += buf[pos + 1]
                    pos += 3
            d = _DefRange(
                local_mt=local_mt,
                global_num=global_num,
                start=msg_start,
                end=pos,
                field_count_pos=field_count_pos,
                field_descriptors_end=field_descriptors_end,
                record_size=total_total,
                fields=tuple(fields_list),
                endian=endian,
            )
            defs_by_local[local_mt] = d
            defs.append(d)
        else:
            d = defs_by_local.get(local_mt)
            if d is None:
                raise FitError(f"data without def (local_mt={local_mt}) at {pos - 1}")
            data_end = pos + d.record_size
            if data_end > body_end:
                raise FitError(f"truncated data at {pos - 1}")
            offs = {}
            o = pos
            regular_size = sum(sz for _, sz, _ in d.fields)
            for fnum, fsz, ftype in d.fields:
                offs[fnum] = (o, fsz, ftype)
                o += fsz
            datas.append(
                _DataRange(local_mt, d.global_num, msg_start, data_end, pos + regular_size, d.endian, offs)
            )
            pos = data_end

    if pos != body_end:
        raise FitError(f"body parser stopped at {pos}, expected {body_end}")
    return defs, datas, body_end, header_size


def _read_record_series(datas: list[_DataRange], buf) -> tuple[list[int], list[int]]:
    times: list[int] = []
    powers: list[int] = []
    for d in datas:
        if d.global_num != MSG_RECORD:
            continue
        ts_off = d.offsets.get(F_TIMESTAMP)
        if not ts_off or ts_off[1] != 4 or ts_off[2] != BT_UINT32:
            continue
        t = _u32(buf, ts_off[0], d.endian)
        if t in (0, U32_INVALID):
            continue
        times.append(t)
        pwr_off = d.offsets.get(F_RECORD_POWER)
        if pwr_off and pwr_off[1] == 2 and pwr_off[2] == BT_UINT16:
            p = struct.unpack_from(d.endian + "H", buf, pwr_off[0])[0]
            powers.append(0 if p == 0xFFFF else p)
        else:
            powers.append(0)
    return times, powers


def _normalized_power(power: list[int], window: int = NP_WINDOW_SECONDS) -> float:
    if not power:
        return 0.0
    if len(power) < window:
        return sum(power) / len(power)
    s = sum(power[:window])
    rolling = [s / window]
    for i in range(window, len(power)):
        s += power[i] - power[i - window]
        rolling.append(s / window)
    fourth = [r ** 4 for r in rolling]
    return (sum(fourth) / len(fourth)) ** 0.25


def _compute_metrics(
    times: list[int],
    powers: list[int],
    ftp: int,
) -> dict:
    if not times or ftp <= 0 or not any(p > 0 for p in powers):
        return {"np": 0, "if": 0.0, "tss": 0.0}
    duration_sec = max(1, times[-1] - times[0])
    np_value = _normalized_power(powers)
    if_value = np_value / ftp
    tss = (duration_sec * np_value * if_value) / (ftp * 3600) * 100
    return {
        "np": min(65535, max(0, int(round(np_value)))),
        "if": min(65.535, max(0.0, if_value)),
        "tss": min(6553.5, max(0.0, tss)),
    }


def _splice_session_metrics(
    buf: bytearray,
    defs: list[_DefRange],
    datas: list[_DataRange],
    body_end: int,
    header_size: int,
    metrics: dict,
) -> tuple[bytes, int]:
    sessions_def = [d for d in defs if d.global_num == MSG_SESSION]
    sessions_data = [d for d in datas if d.global_num == MSG_SESSION]
    if len(sessions_def) != 1 or len(sessions_data) != 1:
        raise FitError(
            f"expected 1 session def + 1 session data, got {len(sessions_def)} + {len(sessions_data)}"
        )
    sdef = sessions_def[0]
    sdata = sessions_data[0]
    if sdef.start > sdata.start:
        raise FitError("session data appears before session definition")

    existing = {f[0] for f in sdef.fields}
    spec = [
        (F_SESSION_NORMALIZED_POWER, 2, 0x84, metrics["np"]),
        (F_SESSION_TSS, 2, 0x84, int(round(metrics["tss"] * 10))),
        (F_SESSION_IF, 2, 0x84, int(round(metrics["if"] * 1000))),
    ]
    to_add = [t for t in spec if t[0] not in existing]
    if not to_add:
        return bytes(buf), 0

    new_def = bytearray(buf[sdef.start:sdef.end])
    new_def[sdef.field_count_pos - sdef.start] += len(to_add)
    desc = b"".join(struct.pack("BBB", f, sz, bt) for (f, sz, bt, _) in to_add)
    new_def[sdef.field_descriptors_end - sdef.start:sdef.field_descriptors_end - sdef.start] = desc

    new_data = bytearray(buf[sdata.start:sdata.end])
    val_bytes = b""
    endian = sdata.endian
    for (_, sz, _, v) in to_add:
        v = max(0, min(int(v), (1 << (sz * 8)) - 1))
        if sz == 1:
            val_bytes += struct.pack(endian + "B", v)
        elif sz == 2:
            val_bytes += struct.pack(endian + "H", v)
        else:
            val_bytes += struct.pack(endian + "I", v)
    new_data[sdata.regular_end - sdata.start:sdata.regular_end - sdata.start] = val_bytes

    new_body = (
        bytes(buf[header_size:sdef.start])
        + bytes(new_def)
        + bytes(buf[sdef.end:sdata.start])
        + bytes(new_data)
        + bytes(buf[sdata.end:body_end])
    )

    new_header = bytearray(buf[:header_size])
    struct.pack_into("<I", new_header, 4, len(new_body))
    if header_size == 14 and struct.unpack_from("<H", new_header, 12)[0]:
        struct.pack_into("<H", new_header, 12, fit_crc(memoryview(new_header)[:12]))
    pre_crc = bytes(new_header) + new_body
    return pre_crc + struct.pack("<H", fit_crc(pre_crc)), len(to_add)


def fix_fit_bytes(
    data: bytes,
    tz: Optional[tzinfo] = None,
    *,
    profile: Optional[str] = None,
    mimic_zwift: bool = False,
    mimic_garmin: bool = False,
    inject_metrics: bool = False,
    ftp: Optional[int] = None,
    force_cycling: bool = False,
) -> tuple[bytes, FixReport]:
    profile_name = profile
    if mimic_zwift and mimic_garmin:
        raise FitError("--mimic-zwift and --mimic-garmin cannot be used together")
    if profile_name and (mimic_zwift or mimic_garmin):
        raise FitError("--profile cannot be combined with --mimic-zwift/--mimic-garmin")
    if mimic_zwift:
        profile_name = "zwift"
    elif mimic_garmin:
        profile_name = "garmin-edge"
    profile_obj: Optional[Profile] = None
    if profile_name is not None:
        profile_obj = PROFILES.get(profile_name)
        if profile_obj is None:
            raise FitError(f"unknown profile: {profile_name}")

    buf = bytearray(data)
    msgs, body_end, header_size, used_local = _walk(buf)
    file_ids = [m for m in msgs if m.global_num == MSG_FILE_ID]
    sessions = [m for m in msgs if m.global_num == MSG_SESSION]
    device_infos = [m for m in msgs if m.global_num == MSG_DEVICE_INFO]
    activities = [m for m in msgs if m.global_num == MSG_ACTIVITY]
    file_creators = [m for m in msgs if m.global_num == MSG_FILE_CREATOR]
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

    if profile_obj is not None and profile_obj.manufacturer == GARMIN_MANUFACTURER:
        for session in sessions:
            sport_raw = _get_u8(buf, session, F_SESSION_SPORT, BT_ENUM)
            sport = None if sport_raw in (None, 0xFF) else sport_raw
            if sport == SPORT_CYCLING:
                continue
            if sport is None:
                if not force_cycling:
                    raise FitError(
                        f"--profile {profile_obj.name}: session sport is missing/unknown; "
                        "pass force_cycling=True (CLI: --force-cycling) to relabel"
                    )
            else:
                sport_label = SPORT_NAMES.get(sport, f"id-{sport}")
                if not force_cycling:
                    raise FitError(
                        f"--profile {profile_obj.name}: session sport is '{sport_label}', not cycling; "
                        "pass force_cycling=True (CLI: --force-cycling) to override (unsafe)"
                    )

    if profile_obj is not None:
        target_manufacturer = profile_obj.manufacturer
        target_product = profile_obj.product
        target_name = profile_obj.product_name
        clear_serial = profile_obj.clear_serial
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

        if profile_obj.software_version > 0:
            for fc in file_creators:
                patches += _set_u16(buf, fc, F_FILE_CREATOR_SOFTWARE_VERSION, profile_obj.software_version)

        if profile_obj.add_creator_device_info:
            pass  # handled below by add_creator_at logic

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

    inject_patches = 0
    if inject_metrics:
        if ftp is None or ftp <= 0:
            raise FitError("inject_metrics requires ftp (in watts)")
        if patches or messages_added:
            struct.pack_into("<H", buf, body_end, fit_crc(memoryview(buf)[:body_end]))
        defs_d, datas_d, body_end_d, header_size_d = _walk_detailed(buf)
        times, powers = _read_record_series(datas_d, buf)
        metrics = _compute_metrics(times, powers, ftp)
        spliced, inject_patches = _splice_session_metrics(
            buf, defs_d, datas_d, body_end_d, header_size_d, metrics
        )
        patches += inject_patches
        out = spliced
    elif patches and not messages_added:
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
    profile: Optional[str] = None,
    mimic_zwift: bool = False,
    mimic_garmin: bool = False,
    inject_metrics: bool = False,
    ftp: Optional[int] = None,
    force_cycling: bool = False,
) -> FixReport:
    src = Path(input_path)
    if not src.is_file():
        raise FitError(f"input file not found: {src}")
    if in_place and output_path is not None:
        raise FitError("--output and --in-place cannot be used together")
    fixed, report = fix_fit_bytes(
        src.read_bytes(),
        tz=tz,
        profile=profile,
        mimic_zwift=mimic_zwift,
        mimic_garmin=mimic_garmin,
        inject_metrics=inject_metrics,
        ftp=ftp,
        force_cycling=force_cycling,
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


def _read_value(buf, msg_offsets: dict, field_num: int, endian: str):
    f = msg_offsets.get(field_num)
    if not f:
        return None
    pos, size, base = f
    if base in (BT_ENUM, BT_UINT8) and size == 1:
        v = buf[pos]
        return None if v == 0xFF else v
    if base == BT_UINT16 and size == 2:
        v = struct.unpack_from(endian + "H", buf, pos)[0]
        return None if v == 0xFFFF else v
    if base == BT_UINT32 and size == 4:
        v = struct.unpack_from(endian + "I", buf, pos)[0]
        return None if v == U32_INVALID else v
    if base == BT_UINT32Z and size == 4:
        v = struct.unpack_from(endian + "I", buf, pos)[0]
        return None if v == 0 else v
    return None


def _detect_source(manufacturer: Optional[int], software_version: Optional[int]) -> str:
    if manufacturer is None:
        return "unknown"
    name = MANUFACTURER_NAMES.get(manufacturer, f"id-{manufacturer}")
    if manufacturer == MYWHOOSH_MANUFACTURER:
        return "mywhoosh"
    return name


def _fit_ts_to_iso(raw: Optional[int]) -> Optional[str]:
    if raw is None or raw == 0:
        return None
    try:
        return (FIT_EPOCH + timedelta(seconds=int(raw))).isoformat()
    except Exception:
        return None


def analyze_fit(data: bytes) -> dict:
    report: dict = {
        "file_size": len(data),
        "warnings": [],
        "errors": [],
    }
    try:
        buf_bytes = bytearray(data)
        defs, datas, body_end, header_size = _walk_detailed(buf_bytes)
    except FitError as e:
        report["errors"].append(str(e))
        return report

    report["header_size"] = header_size
    report["body_size"] = body_end - header_size

    by_global: dict[int, int] = {}
    for d in datas:
        by_global[d.global_num] = by_global.get(d.global_num, 0) + 1
    report["message_counts"] = dict(sorted(by_global.items()))

    file_ids = [d for d in datas if d.global_num == MSG_FILE_ID]
    fid_info: dict = {}
    if file_ids:
        fi = file_ids[0]
        manufacturer = _read_value(buf_bytes, fi.offsets, F_FILE_ID_MANUFACTURER, fi.endian)
        product = _read_value(buf_bytes, fi.offsets, F_FILE_ID_PRODUCT, fi.endian)
        serial = _read_value(buf_bytes, fi.offsets, F_FILE_ID_SERIAL, fi.endian)
        time_created = _read_value(buf_bytes, fi.offsets, F_FILE_ID_TIME_CREATED, fi.endian)
        file_type = _read_value(buf_bytes, fi.offsets, F_FILE_ID_TYPE, fi.endian)
        fid_info = {
            "manufacturer_id": manufacturer,
            "manufacturer": MANUFACTURER_NAMES.get(manufacturer, f"id-{manufacturer}") if manufacturer is not None else None,
            "product": product,
            "serial_number": serial,
            "time_created": _fit_ts_to_iso(time_created),
            "file_type": file_type,
        }
    report["file_id"] = fid_info

    creators = [d for d in datas if d.global_num == MSG_FILE_CREATOR]
    if creators:
        c = creators[0]
        sw = _read_value(buf_bytes, c.offsets, F_FILE_CREATOR_SOFTWARE_VERSION, c.endian)
        report["file_creator"] = {"software_version": sw}
    else:
        report["file_creator"] = None

    device_infos = []
    for di in [d for d in datas if d.global_num == MSG_DEVICE_INFO]:
        device_infos.append({
            "device_index": _read_value(buf_bytes, di.offsets, F_DEVICE_INDEX, di.endian),
            "manufacturer_id": _read_value(buf_bytes, di.offsets, F_DEVICE_MANUFACTURER, di.endian),
            "product": _read_value(buf_bytes, di.offsets, F_DEVICE_PRODUCT, di.endian),
        })
    report["device_infos"] = device_infos

    sessions_info = []
    sessions = [d for d in datas if d.global_num == MSG_SESSION]
    for s in sessions:
        start = _read_value(buf_bytes, s.offsets, F_SESSION_START_TIME, s.endian)
        end = _read_value(buf_bytes, s.offsets, F_TIMESTAMP, s.endian)
        elapsed = _read_value(buf_bytes, s.offsets, F_SESSION_TOTAL_ELAPSED, s.endian)
        timer = _read_value(buf_bytes, s.offsets, F_SESSION_TOTAL_TIMER, s.endian)
        sport = _read_value(buf_bytes, s.offsets, F_SESSION_SPORT, s.endian)
        sub_sport = _read_value(buf_bytes, s.offsets, F_SESSION_SUB_SPORT, s.endian)
        sessions_info.append({
            "start_time": _fit_ts_to_iso(start),
            "end_time": _fit_ts_to_iso(end),
            "total_elapsed_sec": (elapsed / 1000) if elapsed is not None else None,
            "total_timer_sec": (timer / 1000) if timer is not None else None,
            "sport": SPORT_NAMES.get(sport, str(sport)) if sport is not None else None,
            "sub_sport": SUB_SPORT_NAMES.get(sub_sport, str(sub_sport)) if sub_sport is not None else None,
            "avg_hr": _read_value(buf_bytes, s.offsets, F_SESSION_AVG_HR, s.endian),
            "max_hr": _read_value(buf_bytes, s.offsets, F_SESSION_MAX_HR, s.endian),
            "avg_power": _read_value(buf_bytes, s.offsets, F_SESSION_AVG_POWER, s.endian),
            "max_power": _read_value(buf_bytes, s.offsets, F_SESSION_MAX_POWER, s.endian),
            "avg_cadence": _read_value(buf_bytes, s.offsets, F_SESSION_AVG_CADENCE, s.endian),
            "total_distance_m": (_read_value(buf_bytes, s.offsets, F_SESSION_TOTAL_DISTANCE, s.endian) or 0) / 100 or None,
            "total_calories": _read_value(buf_bytes, s.offsets, F_SESSION_TOTAL_CALORIES, s.endian),
            "normalized_power": _read_value(buf_bytes, s.offsets, F_SESSION_NORMALIZED_POWER, s.endian),
            "intensity_factor": (_read_value(buf_bytes, s.offsets, F_SESSION_IF, s.endian) or 0) / 1000 or None,
            "training_stress_score": (_read_value(buf_bytes, s.offsets, F_SESSION_TSS, s.endian) or 0) / 10 or None,
        })
    report["sessions"] = sessions_info

    laps_info = []
    for lap in [d for d in datas if d.global_num == 19]:
        laps_info.append({
            "start_time": _fit_ts_to_iso(_read_value(buf_bytes, lap.offsets, F_LAP_START_TIME, lap.endian)),
            "end_time": _fit_ts_to_iso(_read_value(buf_bytes, lap.offsets, F_LAP_TIMESTAMP, lap.endian)),
        })
    report["laps"] = laps_info

    activities = [d for d in datas if d.global_num == MSG_ACTIVITY]
    activity_info = None
    if activities:
        a = activities[0]
        ts = _read_value(buf_bytes, a.offsets, F_TIMESTAMP, a.endian)
        local_ts_raw = _read_value(buf_bytes, a.offsets, F_ACTIVITY_LOCAL_TS, a.endian)
        local_ts_iso = _fit_ts_to_iso(local_ts_raw)
        unix_shifted = False
        suspicious_year = None
        if local_ts_raw is not None and ts is not None:
            unix_shifted = abs((int(local_ts_raw) - int(ts)) - UNIX_FIT_EPOCH_OFFSET) <= MAX_TZ_OFFSET
            try:
                year = (FIT_EPOCH + timedelta(seconds=int(local_ts_raw))).year
                if year >= 2040:
                    suspicious_year = year
            except Exception:
                pass
        activity_info = {
            "timestamp": _fit_ts_to_iso(ts),
            "local_timestamp": local_ts_iso,
            "local_timestamp_unix_shifted": unix_shifted,
            "suspicious_year": suspicious_year,
            "type": _read_value(buf_bytes, a.offsets, F_ACTIVITY_TYPE, a.endian),
        }
        if unix_shifted:
            report["warnings"].append(f"activity.local_timestamp appears Unix-shifted (year {suspicious_year})")
    report["activity"] = activity_info

    record_datas = [d for d in datas if d.global_num == MSG_RECORD]
    hr_count = power_count = cadence_count = distance_count = speed_count = altitude_count = position_count = 0
    first_ts = last_ts = None
    monotonic = True
    prev_ts = None
    for d in record_datas:
        ts_off = d.offsets.get(F_TIMESTAMP)
        if ts_off and ts_off[1] == 4:
            t = _u32(buf_bytes, ts_off[0], d.endian)
            if t != U32_INVALID and t != 0:
                if first_ts is None:
                    first_ts = t
                last_ts = t
                if prev_ts is not None and t < prev_ts:
                    monotonic = False
                prev_ts = t
        if d.offsets.get(F_RECORD_HEART_RATE):
            hr_count += 1
        if d.offsets.get(F_RECORD_POWER):
            power_count += 1
        if d.offsets.get(F_RECORD_CADENCE):
            cadence_count += 1
        if d.offsets.get(F_RECORD_DISTANCE):
            distance_count += 1
        if d.offsets.get(F_RECORD_SPEED):
            speed_count += 1
        if d.offsets.get(F_RECORD_ALTITUDE):
            altitude_count += 1
        if d.offsets.get(F_RECORD_POSITION_LAT):
            position_count += 1
    report["records"] = {
        "count": len(record_datas),
        "hr_present": hr_count,
        "power_present": power_count,
        "cadence_present": cadence_count,
        "distance_present": distance_count,
        "speed_present": speed_count,
        "altitude_present": altitude_count,
        "position_present": position_count,
        "monotonic_timestamps": monotonic,
        "first_timestamp": _fit_ts_to_iso(first_ts),
        "last_timestamp": _fit_ts_to_iso(last_ts),
    }
    if not monotonic:
        report["warnings"].append("record timestamps not monotonic")

    manuf = fid_info.get("manufacturer_id") if fid_info else None
    sw_version = report["file_creator"]["software_version"] if report.get("file_creator") else None
    report["source_heuristic"] = _detect_source(manuf, sw_version)

    return report


def compare_fits(paths: Sequence[Path]) -> str:
    rows: list[tuple[str, dict]] = []
    for p in paths:
        try:
            r = analyze_fit(Path(p).read_bytes())
        except FitError as e:
            r = {"errors": [str(e)]}
        rows.append((Path(p).name, r))

    lines = ["# FIT comparison\n"]
    keys = [
        ("source", lambda r: r.get("source_heuristic")),
        ("file_id.manufacturer", lambda r: (r.get("file_id") or {}).get("manufacturer")),
        ("file_id.product", lambda r: (r.get("file_id") or {}).get("product")),
        ("file_id.serial", lambda r: (r.get("file_id") or {}).get("serial_number")),
        ("file_id.time_created", lambda r: (r.get("file_id") or {}).get("time_created")),
        ("file_creator.sw_version", lambda r: (r.get("file_creator") or {}).get("software_version") if r.get("file_creator") else None),
        ("session.sport", lambda r: (r.get("sessions") or [{}])[0].get("sport")),
        ("session.sub_sport", lambda r: (r.get("sessions") or [{}])[0].get("sub_sport")),
        ("session.avg_hr", lambda r: (r.get("sessions") or [{}])[0].get("avg_hr")),
        ("session.max_hr", lambda r: (r.get("sessions") or [{}])[0].get("max_hr")),
        ("session.avg_power", lambda r: (r.get("sessions") or [{}])[0].get("avg_power")),
        ("session.normalized_power", lambda r: (r.get("sessions") or [{}])[0].get("normalized_power")),
        ("session.training_stress_score", lambda r: (r.get("sessions") or [{}])[0].get("training_stress_score")),
        ("records.count", lambda r: (r.get("records") or {}).get("count")),
        ("records.hr_present", lambda r: (r.get("records") or {}).get("hr_present")),
        ("records.power_present", lambda r: (r.get("records") or {}).get("power_present")),
        ("activity.local_ts_unix_shifted", lambda r: (r.get("activity") or {}).get("local_timestamp_unix_shifted")),
    ]
    header = "| field | " + " | ".join(n for n, _ in rows) + " |"
    sep = "|---" * (len(rows) + 1) + "|"
    lines.append(header)
    lines.append(sep)
    for label, getter in keys:
        row_vals = []
        for _name, r in rows:
            try:
                v = getter(r)
            except Exception:
                v = None
            row_vals.append("-" if v is None else str(v))
        lines.append(f"| {label} | " + " | ".join(row_vals) + " |")

    lines.append("\n## Message types\n")
    all_globals = set()
    for _n, r in rows:
        for g in (r.get("message_counts") or {}):
            all_globals.add(g)
    lines.append("| global_num | " + " | ".join(n for n, _ in rows) + " |")
    lines.append(sep)
    for g in sorted(all_globals):
        cells = [str((r.get("message_counts") or {}).get(g, "-")) for _n, r in rows]
        lines.append(f"| {g} | " + " | ".join(cells) + " |")

    return "\n".join(lines) + "\n"


def _validate_variant(out_path: Path, original_records: dict) -> dict:
    try:
        data = out_path.read_bytes()
    except OSError as e:
        return {"parse_ok": False, "crc_ok": False, "error": str(e)}
    try:
        _walk(bytearray(data))
    except FitError as e:
        return {"parse_ok": False, "crc_ok": False, "error": str(e)}

    a = analyze_fit(data)
    rec = a.get("records") or {}
    act = a.get("activity") or {}

    def preserved(key: str) -> bool:
        return rec.get(key, 0) >= original_records.get(key, 0)

    local_realistic = not bool(act.get("local_timestamp_unix_shifted"))
    sus_year = act.get("suspicious_year")
    return {
        "parse_ok": True,
        "crc_ok": True,
        "hr_preserved": preserved("hr_present"),
        "power_preserved": preserved("power_present"),
        "cadence_preserved": preserved("cadence_present"),
        "speed_preserved": preserved("speed_present"),
        "distance_preserved": preserved("distance_present"),
        "record_count": rec.get("count", 0),
        "local_timestamp_realistic": local_realistic,
        "suspicious_year": sus_year,
    }


# (variant_filename, fix_fit_bytes kwargs, profile_label, recommended_test_order)
MATRIX_VARIANTS: list[tuple[str, dict, str, int]] = [
    ("01_timestamp_fixed_only", {}, "none", 6),
    ("02_garmin_edge_indoor", {"profile": "garmin-edge"}, "garmin-edge", 1),
    ("03_garmin_forerunner_indoor", {"profile": "garmin-forerunner"}, "garmin-forerunner", 2),
    ("04_zwift_virtual", {"profile": "zwift"}, "zwift", 3),
    ("05_rouvy_virtual", {"profile": "rouvy"}, "rouvy", 4),
    ("06_tacx_indoor", {"profile": "tacx"}, "tacx", 5),
]


def build_test_matrix(input_path: Path, out_dir: Path, ftp: Optional[int] = None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    src = Path(input_path)
    data = src.read_bytes()
    original_analysis = analyze_fit(data)
    original_records = original_analysis.get("records") or {}

    results: list[dict] = []
    for name, kwargs, profile_label, test_order in MATRIX_VARIANTS:
        out_path = out_dir / f"{name}.fit"
        entry: dict = {
            "variant": name,
            "output": out_path.name,
            "profile": profile_label,
            "test_order": test_order,
            "ok": False,
        }
        try:
            patched, rep = fix_fit_bytes(data, **kwargs)
            out_path.write_bytes(patched)
            entry["ok"] = True
            entry["fields_patched"] = rep.fields_patched
            entry["messages_added"] = rep.messages_added
            entry.update(_validate_variant(out_path, original_records))
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"
        results.append(entry)

    in_order = sorted(results, key=lambda r: r["test_order"])

    md = []
    md.append("# Garmin Connect controlled test matrix")
    md.append("")
    md.append(f"Source file: `{src.name}`")
    md.append("")
    md.append("This document is generated. Six patched variants have been written next to it. Each is the same ride with different `file_id` / creator `device_info` metadata so we can find out which spoof, if any, makes Garmin Connect run the file through its training-load pipeline.")
    md.append("")
    md.append("## Testing protocol")
    md.append("")
    md.append("**Upload ONLY ONE variant at a time. DO NOT upload all six variants together.**")
    md.append("")
    md.append("If you upload several at once, Garmin may detect duplicates, merge activities, or process only one version. The result is no longer trustworthy.")
    md.append("")
    md.append("For each variant, in the recommended order below:")
    md.append("")
    md.append("1. Upload one variant to Garmin Connect Web (drag and drop or Import).")
    md.append("2. Wait for the activity to appear in your Activities list.")
    md.append("3. Sync your Forerunner 265 to Garmin Connect **twice** (Sync, wait for it to finish, Sync again).")
    md.append("4. Check Connect and the watch:")
    md.append("   - Did Training Effect appear and look Garmin-processed?")
    md.append("   - Did Acute Load change?")
    md.append("   - Did Recovery Time on the watch change?")
    md.append("   - Did Training Status / Load Focus update?")
    md.append("5. If the variant fails (none of the above changed), **delete the activity from Garmin Connect before testing the next variant**. Garmin rejects duplicate uploads of the same time window.")
    md.append("6. Fill the result row in the table below.")
    md.append("")
    md.append("## Recommended test order")
    md.append("")
    md.append("Test the device-spoofing variants first. The plain timestamp-fixed-only variant is last on purpose: if Garmin ignores it, the result tells you little. The interesting question is whether device/source spoofing gets the file through Garmin's deeper training-load pipeline.")
    md.append("")
    md.append("| Order | Variant | Profile spoof | Generated |")
    md.append("|---|---|---|---|")
    for r in in_order:
        status = "ok" if r["ok"] else "FAIL: " + r.get("error", "")
        md.append(f"| {r['test_order']} | `{r['output']}` | {r['profile']} | {status} |")
    md.append("")
    md.append("## What counts as success")
    md.append("")
    md.append("Merely uploading and seeing the activity displayed in Garmin Connect is **not** success. The activity has to feed Garmin's physiological model.")
    md.append("")
    md.append("Success = at least one of:")
    md.append("")
    md.append("- Training Effect appears and looks Garmin-processed (not just a static number copied from the FIT file).")
    md.append("- Acute Load (today's Load value in Training Status) changes.")
    md.append("- Recovery Time on the Forerunner 265 changes.")
    md.append("- Training Status / Load Focus updates after sync.")
    md.append("")
    md.append("Strongest success signal:")
    md.append("")
    md.append("- Upload variant -> sync watch twice -> Recovery Time on the watch changes.")
    md.append("")
    md.append("That is the clearest indication that Garmin actually processed the activity through its training-load pipeline.")
    md.append("")
    md.append("## Results")
    md.append("")
    md.append("Fill this in as you go. Use yes / no / n/a.")
    md.append("")
    md.append("| Order | Variant | Uploaded | TE visible | Acute Load changed | Recovery Time changed | Training Status / Load Focus affected | Deleted after fail | Notes |")
    md.append("|---|---|---|---|---|---|---|---|---|")
    for r in in_order:
        if not r["ok"]:
            md.append(f"| {r['test_order']} | `{r['output']}` | n/a (generation failed) | - | - | - | - | - | {r.get('error','')} |")
            continue
        md.append(f"| {r['test_order']} | `{r['output']}` |  |  |  |  |  |  |  |")
    md.append("")
    md.append("## Disclaimer")
    md.append("")
    md.append("This tool makes no guarantee that any variant will be accepted by Garmin Connect for training-load metrics. The pipeline appears to gate on a certified-source allowlist (Garmin devices, Zwift, Rouvy, TrainerRoad, Tacx Training). File metadata alone may not be enough; Garmin may also require the activity to arrive through the partner's cloud API rather than via manual FIT upload.")
    md.append("")
    md.append("If none of the six variants triggers Acute Load or Recovery Time changes, the next experimental target (v2.1) is deeper structural matching against a real Garmin-native cycling FIT (event timer start/stop pairs, file_creator hardware fields, device_info ordering and device_index, session/lap summary completeness).")
    md.append("")

    (out_dir / "test_matrix.md").write_text("\n".join(md), encoding="utf-8")

    summary = {
        "total": len(results),
        "parse_ok_count": sum(1 for r in results if r.get("parse_ok")),
        "crc_ok_count": sum(1 for r in results if r.get("crc_ok")),
        "generation_ok_count": sum(1 for r in results if r.get("ok")),
        "hr_preserved_all": all(r.get("hr_preserved", False) for r in results if r.get("ok")),
        "power_preserved_all": all(r.get("power_preserved", False) for r in results if r.get("ok")),
        "cadence_preserved_all": all(r.get("cadence_preserved", False) for r in results if r.get("ok")),
        "speed_preserved_all": all(r.get("speed_preserved", False) for r in results if r.get("ok")),
        "distance_preserved_all": all(r.get("distance_preserved", False) for r in results if r.get("ok")),
        "timestamp_fixed_all": all(r.get("local_timestamp_realistic", False) for r in results if r.get("ok")),
        "test_matrix_path": str(out_dir / "test_matrix.md"),
    }
    return {"results": results, "out_dir": str(out_dir), "summary": summary}


def _config_path() -> Path:
    return Path(__file__).resolve().parent / "fit-fix.cfg"


def _load_config() -> dict:
    path = _config_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_config(data: dict) -> None:
    try:
        _config_path().write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def _gui_prompt_ftp() -> Optional[int]:
    try:
        import tkinter as tk
        from tkinter import simpledialog
    except Exception:
        return None
    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
        return simpledialog.askinteger(
            "fit-fix setup",
            "Your FTP in watts\n(Garmin Connect: User Settings -> Power Zones):",
            minvalue=50,
            maxvalue=600,
            parent=root,
        )
    except Exception:
        return None
    finally:
        try:
            root.destroy()
        except Exception:
            pass


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


def _add_patch_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("files", nargs="*", help="one or more .fit files")
    p.add_argument("-o", "--output", help="output path (single input only)")
    p.add_argument("--out-dir", help="output directory (writes <name>_fixed.fit per input)")
    p.add_argument("--in-place", action="store_true", help="overwrite input")
    p.add_argument("--overwrite", action="store_true", help="overwrite output if it exists")
    p.add_argument("--write-when-unchanged", action="store_true",
                   help="write a copy even if input is already correct")
    p.add_argument("--utc", action="store_true", help="use UTC for local_timestamp")
    p.add_argument("--profile", choices=list(PROFILES.keys()), default=None,
                   help="device profile to spoof in file_id and creator device_info")
    p.add_argument("--mimic-zwift", action="store_true", help="(legacy) shorthand for --profile zwift")
    p.add_argument("--mimic-garmin", action="store_true", help="(legacy) shorthand for --profile garmin-edge")
    p.add_argument("--inject-metrics", action="store_true",
                   help="compute and write Normalized Power, IF, TSS into the session")
    p.add_argument("--ftp", type=int, default=None,
                   help="FTP in watts (required for --inject-metrics; GUI prompts if unset)")
    p.add_argument("--force-cycling", action="store_true",
                   help="override the non-cycling refusal for Garmin profiles (UNSAFE: never relabel a run/swim as a ride unless you know what you are doing)")
    p.add_argument("--inject-te-approx", action="store_true",
                   help="ALSO write approximate aerobic Training Effect (HR-TRIMP based, NOT Garmin-native)")
    p.add_argument("--resting-hr", type=int, default=None, help="resting HR in bpm (used by --inject-te-approx)")
    p.add_argument("--max-hr", type=int, default=None, help="max HR in bpm (used by --inject-te-approx)")
    p.add_argument("--lthr", type=int, default=None, help="lactate threshold HR (used by --inject-te-approx)")
    p.add_argument("--no-gui", action="store_true", help="never show popups")


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fit-fix",
        description="Analyze and repair MyWhoosh FIT exports for Garmin Connect compatibility.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command")

    p_patch = sub.add_parser("patch", help="patch one or more FIT files (default action)")
    _add_patch_args(p_patch)

    p_analyze = sub.add_parser("analyze", help="emit a structured report on each FIT file")
    p_analyze.add_argument("files", nargs="+", help="one or more .fit files")
    p_analyze.add_argument("--json", dest="json_out", help="write a single JSON report instead of stdout text")
    p_analyze.add_argument("--no-gui", action="store_true", help="never show popups")

    p_compare = sub.add_parser("compare", help="compare 2+ FIT files side by side")
    p_compare.add_argument("files", nargs="+", help="two or more .fit files to compare")
    p_compare.add_argument("--md", dest="md_out", help="write markdown report to file instead of stdout")
    p_compare.add_argument("--json", dest="json_out", help="write JSON report")
    p_compare.add_argument("--no-gui", action="store_true", help="never show popups")

    p_matrix = sub.add_parser("matrix", help="generate a set of patched variants for manual Garmin testing")
    p_matrix.add_argument("file", help="MyWhoosh .fit file to use as source")
    p_matrix.add_argument("--out-dir", required=True, help="directory to write variants and test_matrix.md")
    p_matrix.add_argument("--ftp", type=int, default=None, help="optional FTP for metric variants")
    p_matrix.add_argument("--no-gui", action="store_true", help="never show popups")

    return p


def _format_analyze_text(report: dict) -> str:
    name = os.path.basename(report.get("file", "?"))
    lines = [f"=== analysis: {name} ==="]
    if report.get("errors"):
        for e in report["errors"]:
            lines.append(f"ERROR: {e}")
        return "\n".join(lines)
    lines.append(f"source heuristic: {report.get('source_heuristic')}")
    lines.append("")

    fid = report.get("file_id") or {}
    lines.append("file_id:")
    lines.append(f"  manufacturer    : {fid.get('manufacturer')} (id={fid.get('manufacturer_id')})")
    lines.append(f"  product         : {fid.get('product')}")
    lines.append(f"  serial_number   : {fid.get('serial_number')}")
    lines.append(f"  time_created    : {fid.get('time_created')}")

    fc = report.get("file_creator")
    lines.append("file_creator:")
    if fc:
        lines.append(f"  software_version: {fc.get('software_version')}")
    else:
        lines.append("  (no file_creator message)")

    lines.append("device_info:")
    if report.get("device_infos"):
        for d in report["device_infos"]:
            label = "creator" if d.get("device_index") == 0 else f"device_index={d.get('device_index')}"
            lines.append(f"  [{label}] manufacturer_id={d.get('manufacturer_id')} product={d.get('product')}")
    else:
        lines.append("  (no device_info messages)")

    lines.append("")
    for i, s in enumerate(report.get("sessions") or []):
        lines.append(f"session[{i}]:")
        lines.append(f"  sport / sub_sport : {s.get('sport')} / {s.get('sub_sport')}")
        lines.append(f"  start_time        : {s.get('start_time')}")
        lines.append(f"  end_time          : {s.get('end_time')}")
        dur = s.get("total_timer_sec")
        if dur is not None:
            m = int(dur) // 60
            sec = int(dur) % 60
            lines.append(f"  duration          : {dur:.0f} sec ({m}m {sec}s)")
        lines.append(f"  avg / max HR      : {s.get('avg_hr')} / {s.get('max_hr')} bpm")
        lines.append(f"  avg / max power   : {s.get('avg_power')} / {s.get('max_power')} W")
        lines.append(f"  NP / IF / TSS     : {s.get('normalized_power')} / {s.get('intensity_factor')} / {s.get('training_stress_score')}")

    a = report.get("activity") or {}
    lines.append("")
    lines.append("activity:")
    lines.append(f"  timestamp           : {a.get('timestamp')}")
    lines.append(f"  local_timestamp     : {a.get('local_timestamp')}")
    realistic = not bool(a.get("local_timestamp_unix_shifted"))
    note = ""
    if not realistic:
        sy = a.get("suspicious_year")
        note = f"  (Unix-shifted; year {sy})" if sy else "  (Unix-shifted)"
    lines.append(f"  local_ts realistic  : {'yes' if realistic else 'NO'}{note}")
    lines.append(f"  suspicious year     : {a.get('suspicious_year') if a.get('suspicious_year') else 'no'}")

    rec = report.get("records") or {}
    lines.append("")
    n = rec.get("count", 0)
    lines.append(f"records ({n} total):")

    def line(label: str, key: str) -> str:
        v = rec.get(key, 0)
        pct = f"{(v / n * 100):.0f}%" if n else "n/a"
        present = "yes" if v > 0 else "no"
        return f"  {label:<10}: {present} ({v} / {n}, {pct})"

    lines.append(line("HR", "hr_present"))
    lines.append(line("power", "power_present"))
    lines.append(line("cadence", "cadence_present"))
    lines.append(line("speed", "speed_present"))
    lines.append(line("distance", "distance_present"))
    lines.append(line("altitude", "altitude_present"))
    lines.append(line("position", "position_present"))
    lines.append(f"  monotonic : {'yes' if rec.get('monotonic_timestamps') else 'NO'}")

    warnings = report.get("warnings") or []
    if warnings:
        lines.append("")
        lines.append("warnings:")
        for w in warnings:
            lines.append(f"  - {w}")

    return "\n".join(lines)


def _cmd_analyze(args, use_gui: bool) -> int:
    reports = []
    ok = True
    for p in args.files:
        try:
            r = analyze_fit(Path(p).read_bytes())
            r["file"] = str(p)
        except Exception as e:
            ok = False
            r = {"file": str(p), "errors": [f"{type(e).__name__}: {e}"]}
        reports.append(r)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(reports, indent=2, default=str), encoding="utf-8")
        if use_gui:
            _gui_notify(ok, f"Wrote {args.json_out}")
        elif sys.stdout is not None:
            print(f"Wrote {args.json_out}")
        return 0 if ok else 1

    body = "\n\n".join(_format_analyze_text(r) for r in reports)
    if use_gui:
        _gui_notify(ok, body)
    else:
        stream = sys.stdout if ok else sys.stderr
        if stream is not None:
            print(body, file=stream)
    return 0 if ok else 1


def _cmd_compare(args, use_gui: bool) -> int:
    if len(args.files) < 2:
        msg = "compare needs at least two files"
        if use_gui:
            _gui_notify(False, msg)
        elif sys.stderr is not None:
            print(msg, file=sys.stderr)
        return 2
    md = compare_fits([Path(p) for p in args.files])
    if args.md_out:
        Path(args.md_out).write_text(md, encoding="utf-8")
    if args.json_out:
        reports = [analyze_fit(Path(p).read_bytes()) | {"file": str(p)} for p in args.files]
        Path(args.json_out).write_text(json.dumps(reports, indent=2, default=str), encoding="utf-8")
    if use_gui:
        _gui_notify(True, f"Compared {len(args.files)} files.")
    elif sys.stdout is not None:
        print(md)
    return 0


def _format_matrix_summary(result: dict) -> str:
    s = result["summary"]
    lines = [
        f"Generated {s['generation_ok_count']}/{s['total']} Garmin test variants in {result['out_dir']}",
        "",
        "Validation:",
        f"- {s['parse_ok_count']}/{s['total']} files parse successfully",
        f"- {s['crc_ok_count']}/{s['total']} CRC checks passed",
        f"- HR stream preserved:        {'yes' if s['hr_preserved_all'] else 'no'}",
        f"- Power stream preserved:     {'yes' if s['power_preserved_all'] else 'no'}",
        f"- Cadence stream preserved:   {'yes' if s['cadence_preserved_all'] else 'no'}",
        f"- Speed stream preserved:     {'yes' if s['speed_preserved_all'] else 'no'}",
        f"- Distance stream preserved:  {'yes' if s['distance_preserved_all'] else 'no'}",
        f"- Suspicious local_timestamp fixed: {'yes' if s['timestamp_fixed_all'] else 'no'}",
        "",
        "Next:",
        f"Open {s['test_matrix_path']} and test ONE file at a time in Garmin Connect.",
    ]
    return "\n".join(lines)


def _cmd_matrix(args, use_gui: bool) -> int:
    out_dir = Path(args.out_dir)
    try:
        result = build_test_matrix(Path(args.file), out_dir, ftp=args.ftp)
    except Exception as e:
        msg = f"matrix failed: {type(e).__name__}: {e}"
        if use_gui:
            _gui_notify(False, msg)
        elif sys.stderr is not None:
            print(msg, file=sys.stderr)
        return 1
    body = _format_matrix_summary(result)
    if use_gui:
        _gui_notify(True, body)
    elif sys.stdout is not None:
        print(body)
    return 0


def _resolve_ftp(args, use_gui: bool) -> tuple[Optional[int], Optional[str]]:
    ftp = args.ftp
    if not args.inject_metrics:
        return ftp, None
    if ftp is None:
        ftp = _load_config().get("ftp")
    if ftp is None and use_gui:
        ftp = _gui_prompt_ftp()
        if ftp is not None:
            _save_config({"ftp": int(ftp)})
    if ftp is None:
        return None, "--inject-metrics needs --ftp (or set it once via the GUI prompt)"
    return ftp, None


def _cmd_patch(args, use_gui: bool) -> int:
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
            print("no input files", file=sys.stderr)
        return 2

    if args.output and len(files) != 1:
        msg = "--output requires exactly one input file"
        if use_gui:
            _gui_notify(False, msg)
        elif sys.stderr is not None:
            print(msg, file=sys.stderr)
        return 2
    if args.output and args.out_dir:
        msg = "--output and --out-dir cannot be used together"
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

    ftp, err = _resolve_ftp(args, use_gui)
    if err is not None:
        if use_gui:
            _gui_notify(False, err)
        elif sys.stderr is not None:
            print(err, file=sys.stderr)
        return 2

    if args.inject_te_approx and not args.inject_metrics:
        msg = "--inject-te-approx requires --inject-metrics (and --ftp, --resting-hr, --max-hr)"
        if use_gui:
            _gui_notify(False, msg)
        elif sys.stderr is not None:
            print(msg, file=sys.stderr)
        return 2
    if args.inject_te_approx:
        for required in ("resting_hr", "max_hr"):
            if getattr(args, required) is None:
                msg = f"--inject-te-approx requires --{required.replace('_','-')}"
                if use_gui:
                    _gui_notify(False, msg)
                elif sys.stderr is not None:
                    print(msg, file=sys.stderr)
                return 2

    tz = timezone.utc if args.utc else None
    lines: list[str] = []
    ok = True
    out_dir = Path(args.out_dir) if args.out_dir else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    for path in files:
        try:
            output_path = args.output
            if out_dir is not None:
                src = Path(path)
                output_path = str(out_dir / (src.stem + "_fixed" + src.suffix))
            r = fix_fit(
                path,
                output_path=output_path,
                tz=tz,
                overwrite=args.overwrite,
                in_place=args.in_place,
                write_when_unchanged=args.write_when_unchanged,
                profile=args.profile,
                mimic_zwift=args.mimic_zwift,
                mimic_garmin=args.mimic_garmin,
                inject_metrics=args.inject_metrics,
                ftp=ftp,
                force_cycling=args.force_cycling,
            )
            if args.inject_te_approx:
                lines.append(_format_report(r) + "\n      (approximate TE injection not yet implemented in v2; TODO)")
            else:
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


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_arg_parser()
    use_gui = not _has_console()
    raw = list(sys.argv[1:]) if argv is None else list(argv)
    known = {"analyze", "patch", "compare", "matrix"}
    first_positional = next((a for a in raw if not a.startswith("-")), None)
    if first_positional not in known and not any(a in ("-h", "--help", "--version") for a in raw):
        raw = ["patch"] + raw
    elif not raw:
        raw = ["patch"]

    try:
        args = parser.parse_args(raw)
    except SystemExit as e:
        code = int(e.code) if isinstance(e.code, int) else 2
        if use_gui and code != 0:
            _gui_notify(False, "Invalid arguments.")
        return code
    except Exception as e:
        if use_gui:
            _gui_notify(False, f"Argument parsing failed: {e}")
        return 2

    use_gui = use_gui and not getattr(args, "no_gui", False)
    cmd = args.command or "patch"
    if cmd == "analyze":
        return _cmd_analyze(args, use_gui)
    if cmd == "compare":
        return _cmd_compare(args, use_gui)
    if cmd == "matrix":
        return _cmd_matrix(args, use_gui)
    return _cmd_patch(args, use_gui)


if __name__ == "__main__":
    raise SystemExit(main())
