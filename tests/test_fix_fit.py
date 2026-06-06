"""Tests for fix_fit. Uses synthetic FIT files so no personal data is required."""

from __future__ import annotations

import os
import struct
import sys
from datetime import timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fix_fit import (
    FIT_EPOCH,
    FitError,
    UNIX_FIT_EPOCH_OFFSET,
    fit_crc,
    fix_fit,
    fix_fit_bytes,
)


def _make_fit(
    *,
    session_start: int,
    session_elapsed_ms: int,
    session_ts: int,
    activity_ts: int,
    activity_local_ts: int,
    file_id: tuple[int, int, int] | None = None,
    activity_local_field_num: int = 5,
    activity_local_base_type: int = 0x86,
    architecture: int = 0,
) -> bytes:
    body = bytearray()
    endian = "<" if architecture == 0 else ">"

    if file_id is not None:
        manufacturer, product, serial = file_id
        body.append(0x42)
        body.append(0)
        body.append(architecture)
        body += struct.pack(endian + "H", 0)
        body.append(3)
        body += bytes([1, 2, 0x84])
        body += bytes([2, 2, 0x84])
        body += bytes([3, 4, 0x8C])
        body.append(0x02)
        body += struct.pack(endian + "HHI", manufacturer, product, serial)

    body.append(0x40)
    body.append(0)
    body.append(architecture)
    body += struct.pack(endian + "H", 18)
    body.append(5)
    body += bytes([253, 4, 0x86])
    body += bytes([2, 4, 0x86])
    body += bytes([7, 4, 0x86])
    body += bytes([5, 1, 0x00])
    body += bytes([6, 1, 0x00])

    body.append(0x00)
    body += struct.pack(
        endian + "IIIBB",
        session_ts,
        session_start,
        session_elapsed_ms,
        2,
        58,
    )

    body.append(0x41)
    body.append(0)
    body.append(architecture)
    body += struct.pack(endian + "H", 34)
    body.append(2)
    body += bytes([253, 4, 0x86])
    body += bytes([activity_local_field_num, 4, activity_local_base_type])

    body.append(0x01)
    body += struct.pack(endian + "II", activity_ts, activity_local_ts)

    header = bytearray(14)
    header[0] = 14
    header[1] = 32
    header[2:4] = struct.pack("<H", 2156)
    header[4:8] = struct.pack("<I", len(body))
    header[8:12] = b".FIT"
    header[12:14] = struct.pack("<H", fit_crc(bytes(header[:12])))

    full = bytes(header) + bytes(body)
    full += struct.pack("<H", fit_crc(full))
    return full


SESSION_START = 1_149_599_660
ELAPSED_MS = 4_859_000
SESSION_END = SESSION_START + ELAPSED_MS // 1000
UNIX_LOCAL_TS = 1_780_670_118


def _broken_mywhoosh_bytes() -> bytes:
    return _make_fit(
        session_start=SESSION_START,
        session_elapsed_ms=ELAPSED_MS,
        session_ts=SESSION_START,
        activity_ts=SESSION_START,
        activity_local_ts=UNIX_LOCAL_TS,
    )


def _correct_bytes() -> bytes:
    return _make_fit(
        session_start=SESSION_START,
        session_elapsed_ms=ELAPSED_MS,
        session_ts=SESSION_END,
        activity_ts=SESSION_END,
        activity_local_ts=SESSION_END + 7200,
    )


def _with_12_byte_header(src: bytes) -> bytes:
    out = bytearray(src[:12] + src[14:])
    out[0] = 12
    out[-2:] = struct.pack("<H", fit_crc(out[:-2]))
    return bytes(out)


def _with_timer_time_only(src: bytes) -> bytes:
    out = bytearray(src)
    body_start = out[0]
    out[body_start + 12] = 8
    out[-2:] = struct.pack("<H", fit_crc(out[:-2]))
    return bytes(out)


def _insert_records(src: bytes, body_offset: int, records: bytes) -> bytes:
    header_size = src[0]
    data_size = struct.unpack_from("<I", src, 4)[0]
    header = bytearray(src[:header_size])
    old_body = src[header_size:header_size + data_size]
    body = old_body[:body_offset] + records + old_body[body_offset:]
    header[4:8] = struct.pack("<I", len(body))
    if header_size == 14:
        header[12:14] = struct.pack("<H", fit_crc(header[:12]))
    out = bytes(header) + body
    return out + struct.pack("<H", fit_crc(out))


def _prepend_records(src: bytes, records: bytes) -> bytes:
    return _insert_records(src, 0, records)


def _parse(buf: bytes) -> dict[str, int]:
    from fix_fit import _walk, _u32, MSG_SESSION, MSG_ACTIVITY, F_TIMESTAMP, F_ACTIVITY_LOCAL_TS

    msgs, _body_end, _hs, _used_local = _walk(buf)
    s = next(m for m in msgs if m.global_num == MSG_SESSION)
    a = next(m for m in msgs if m.global_num == MSG_ACTIVITY)
    return {
        "session_ts": _u32(buf, s.offsets[F_TIMESTAMP][0], s.endian),
        "activity_ts": _u32(buf, a.offsets[F_TIMESTAMP][0], a.endian),
        "activity_local_ts": _u32(buf, a.offsets[F_ACTIVITY_LOCAL_TS][0], a.endian),
    }


def test_crc_known_vectors() -> None:
    assert fit_crc(b"") == 0
    assert fit_crc(b"\x00" * 8) == 0
    assert fit_crc(b"123456789") == 0xBB3D


def test_synthetic_fit_has_valid_crcs() -> None:
    buf = _broken_mywhoosh_bytes()
    hs = buf[0]
    ds = struct.unpack_from("<I", buf, 4)[0]
    assert fit_crc(buf[:hs + ds]) == struct.unpack_from("<H", buf, hs + ds)[0]
    assert fit_crc(buf[:12]) == struct.unpack_from("<H", buf, 12)[0]


def test_12_byte_header_is_supported() -> None:
    fixed, report = fix_fit_bytes(_with_12_byte_header(_broken_mywhoosh_bytes()))
    assert report.fields_patched == 3
    assert fixed[0] == 12
    assert fit_crc(fixed[:-2]) == struct.unpack_from("<H", fixed, len(fixed) - 2)[0]


def test_timer_time_is_used_when_elapsed_time_is_missing() -> None:
    fixed, report = fix_fit_bytes(_with_timer_time_only(_broken_mywhoosh_bytes()))
    assert report.fields_patched == 3
    assert _parse(fixed)["session_ts"] == SESSION_END


def test_zero_duration_session_is_supported() -> None:
    src = _make_fit(
        session_start=SESSION_START,
        session_elapsed_ms=0,
        session_ts=SESSION_START,
        activity_ts=SESSION_START,
        activity_local_ts=SESSION_START + UNIX_FIT_EPOCH_OFFSET,
    )
    fixed, report = fix_fit_bytes(src, tz=timezone.utc)
    assert report.fields_patched == 1
    assert _parse(fixed)["activity_local_ts"] == SESSION_START


def test_compressed_timestamp_record_is_skipped_correctly() -> None:
    records = bytes(
        [
            0x42, 0, 0, 20, 0, 1, 3, 1, 2,
            0xC1, 100,
        ]
    )
    fixed, report = fix_fit_bytes(_prepend_records(_broken_mywhoosh_bytes(), records))
    assert report.fields_patched == 3
    assert _parse(fixed)["session_ts"] == SESSION_END


def test_patches_broken_file() -> None:
    fixed, report = fix_fit_bytes(_broken_mywhoosh_bytes(), tz=timezone(timedelta(hours=2)))
    assert report.fields_patched == 3
    assert not report.was_already_correct
    assert report.sessions == 1
    assert report.activities == 1
    p = _parse(fixed)
    assert p["session_ts"] == SESSION_END
    assert p["activity_ts"] == SESSION_END
    assert p["activity_local_ts"] == SESSION_END + 7200


def test_patches_big_endian_file() -> None:
    src = _make_fit(
        session_start=SESSION_START,
        session_elapsed_ms=ELAPSED_MS,
        session_ts=SESSION_START,
        activity_ts=SESSION_START,
        activity_local_ts=UNIX_LOCAL_TS,
        architecture=1,
    )
    fixed, report = fix_fit_bytes(src, tz=timezone.utc)
    assert report.fields_patched == 3
    parsed = _parse(fixed)
    assert parsed["session_ts"] == SESSION_END
    assert parsed["activity_ts"] == SESSION_END
    assert parsed["activity_local_ts"] == SESSION_END


def test_patches_each_session_in_multi_session_file() -> None:
    first_elapsed = 1_000
    second_start = SESSION_START + 100
    second_elapsed = 2_000
    activity_end = second_start + second_elapsed // 1000
    src = _make_fit(
        session_start=SESSION_START,
        session_elapsed_ms=first_elapsed,
        session_ts=SESSION_START,
        activity_ts=SESSION_START,
        activity_local_ts=activity_end + UNIX_FIT_EPOCH_OFFSET,
    )
    second_session = b"\x00" + struct.pack(
        "<III", second_start, second_start, second_elapsed
    )
    second_session += bytes([2, 58])
    fixed, report = fix_fit_bytes(_insert_records(src, 36, second_session), tz=timezone.utc)
    from fix_fit import F_TIMESTAMP, MSG_SESSION, _u32, _walk

    messages, _, _, _ = _walk(fixed)
    session_times = [
        _u32(fixed, message.offsets[F_TIMESTAMP][0], message.endian)
        for message in messages
        if message.global_num == MSG_SESSION
    ]
    assert report.sessions == 2
    assert report.fields_patched == 4
    assert session_times == [SESSION_START + 1, activity_end]
    assert _parse(fixed)["activity_ts"] == activity_end
    assert _parse(fixed)["activity_local_ts"] == activity_end


def test_leaves_correct_file_alone() -> None:
    src = _correct_bytes()
    fixed, report = fix_fit_bytes(src)
    assert report.fields_patched == 0
    assert report.was_already_correct
    assert fixed == src


def test_leaves_garmin_style_activity_start_timestamp_alone() -> None:
    src = _make_fit(
        session_start=SESSION_START,
        session_elapsed_ms=ELAPSED_MS,
        session_ts=SESSION_END,
        activity_ts=SESSION_START,
        activity_local_ts=SESSION_START + 7200,
    )
    fixed, report = fix_fit_bytes(src, tz=timezone(timedelta(hours=2)))
    assert report.was_already_correct
    assert fixed == src


def test_leaves_long_activity_with_start_local_timestamp_alone() -> None:
    elapsed = 20 * 60 * 60 * 1000
    src = _make_fit(
        session_start=SESSION_START,
        session_elapsed_ms=elapsed,
        session_ts=SESSION_START + elapsed // 1000,
        activity_ts=SESSION_START,
        activity_local_ts=SESSION_START + 7200,
    )
    fixed, report = fix_fit_bytes(src, tz=timezone(timedelta(hours=2)))
    assert report.was_already_correct
    assert fixed == src


def test_leaves_unrelated_far_local_timestamp_alone() -> None:
    src = _make_fit(
        session_start=SESSION_START,
        session_elapsed_ms=ELAPSED_MS,
        session_ts=SESSION_END,
        activity_ts=SESSION_END,
        activity_local_ts=SESSION_END + 20 * 3600,
    )
    fixed, report = fix_fit_bytes(src)
    assert report.was_already_correct
    assert fixed == src


def test_leaves_wrong_base_type_alone() -> None:
    src = _make_fit(
        session_start=SESSION_START,
        session_elapsed_ms=ELAPSED_MS,
        session_ts=SESSION_END,
        activity_ts=SESSION_END,
        activity_local_ts=SESSION_END + UNIX_FIT_EPOCH_OFFSET,
        activity_local_base_type=0x88,
    )
    fixed, report = fix_fit_bytes(src)
    assert report.was_already_correct
    assert fixed == src


def test_repairs_future_summary_timestamps_after_epoch_bug_is_confirmed() -> None:
    src = _make_fit(
        session_start=SESSION_START,
        session_elapsed_ms=ELAPSED_MS,
        session_ts=SESSION_END + 100,
        activity_ts=SESSION_END + 100,
        activity_local_ts=SESSION_END + UNIX_FIT_EPOCH_OFFSET,
    )
    fixed, report = fix_fit_bytes(src, tz=timezone.utc)
    parsed = _parse(fixed)
    assert report.fields_patched == 3
    assert parsed["session_ts"] == SESSION_END
    assert parsed["activity_ts"] == SESSION_END
    assert parsed["activity_local_ts"] == SESSION_END


def test_fractional_elapsed_time_uses_floor() -> None:
    elapsed = 4_859_999
    end = SESSION_START + elapsed // 1000
    src = _make_fit(
        session_start=SESSION_START,
        session_elapsed_ms=elapsed,
        session_ts=end,
        activity_ts=end,
        activity_local_ts=UNIX_LOCAL_TS,
    )
    fixed, report = fix_fit_bytes(src, tz=timezone.utc)
    assert report.fields_patched == 1
    parsed = _parse(fixed)
    assert parsed["session_ts"] == end
    assert parsed["activity_ts"] == end
    assert parsed["activity_local_ts"] == end


def test_idempotent() -> None:
    once, _ = fix_fit_bytes(_broken_mywhoosh_bytes())
    twice, report = fix_fit_bytes(once)
    assert report.fields_patched == 0
    assert report.was_already_correct
    assert once == twice


def test_resulting_crc_is_valid() -> None:
    fixed, _ = fix_fit_bytes(_broken_mywhoosh_bytes())
    hs = fixed[0]
    ds = struct.unpack_from("<I", fixed, 4)[0]
    assert fit_crc(fixed[:hs + ds]) == struct.unpack_from("<H", fixed, hs + ds)[0]
    assert fit_crc(fixed[:12]) == struct.unpack_from("<H", fixed, 12)[0]


def test_utc_mode_zero_offset() -> None:
    fixed, report = fix_fit_bytes(_broken_mywhoosh_bytes(), tz=timezone.utc)
    p = _parse(fixed)
    assert p["activity_local_ts"] == SESSION_END
    assert report.utc_offset == timedelta(0)


def test_zero_local_ts_left_alone() -> None:
    src = _make_fit(
        session_start=SESSION_START,
        session_elapsed_ms=ELAPSED_MS,
        session_ts=SESSION_END,
        activity_ts=SESSION_END,
        activity_local_ts=0,
    )
    fixed, report = fix_fit_bytes(src)
    p = _parse(fixed)
    assert p["activity_local_ts"] == 0
    assert report.was_already_correct


def test_missing_signature_raises() -> None:
    with pytest.raises(FitError, match=r"missing \.FIT signature"):
        fix_fit_bytes(b"\x0e\x20\x00\x00\x00\x00\x00\x00BADSXXXX")


def test_truncated_file_raises() -> None:
    full = _broken_mywhoosh_bytes()
    with pytest.raises(FitError, match="truncated"):
        fix_fit_bytes(full[:-10])


def test_too_small_raises() -> None:
    with pytest.raises(FitError, match="too small"):
        fix_fit_bytes(b"\x0e\x20")


def test_invalid_header_size_raises() -> None:
    bad = bytearray(_broken_mywhoosh_bytes())
    bad[0] = 5
    with pytest.raises(FitError, match="invalid header size"):
        fix_fit_bytes(bytes(bad))


def test_reserved_record_header_bit_raises() -> None:
    bad = bytearray(_broken_mywhoosh_bytes())
    bad[bad[0]] |= 0x10
    bad[-2:] = struct.pack("<H", fit_crc(bad[:-2]))
    with pytest.raises(FitError, match="reserved record-header"):
        fix_fit_bytes(bytes(bad))


def test_reserved_data_header_bit_raises() -> None:
    bad = bytearray(_broken_mywhoosh_bytes())
    session_data_header = bad[0] + 21
    bad[session_data_header] |= 0x20
    bad[-2:] = struct.pack("<H", fit_crc(bad[:-2]))
    with pytest.raises(FitError, match="reserved data-header"):
        fix_fit_bytes(bytes(bad))


def test_nonzero_definition_reserved_byte_raises() -> None:
    bad = bytearray(_broken_mywhoosh_bytes())
    bad[bad[0] + 1] = 1
    bad[-2:] = struct.pack("<H", fit_crc(bad[:-2]))
    with pytest.raises(FitError, match="definition reserved"):
        fix_fit_bytes(bytes(bad))


def test_duplicate_native_field_raises() -> None:
    bad = _make_fit(
        session_start=SESSION_START,
        session_elapsed_ms=ELAPSED_MS,
        session_ts=SESSION_END,
        activity_ts=SESSION_END,
        activity_local_ts=SESSION_END,
        activity_local_field_num=253,
    )
    with pytest.raises(FitError, match="duplicate field"):
        fix_fit_bytes(bad)


def test_header_crc_mismatch_raises() -> None:
    bad = bytearray(_broken_mywhoosh_bytes())
    bad[1] ^= 1
    with pytest.raises(FitError, match="header CRC"):
        fix_fit_bytes(bytes(bad))


def test_zero_header_crc_is_valid_and_preserved() -> None:
    src = bytearray(_broken_mywhoosh_bytes())
    src[12:14] = b"\0\0"
    src[-2:] = struct.pack("<H", fit_crc(src[:-2]))
    fixed, report = fix_fit_bytes(bytes(src))
    assert report.fields_patched == 3
    assert fixed[12:14] == b"\0\0"
    assert fit_crc(fixed[:-2]) == struct.unpack_from("<H", fixed, len(fixed) - 2)[0]


def test_file_crc_mismatch_raises() -> None:
    bad = bytearray(_broken_mywhoosh_bytes())
    bad[bad[0] + 8] ^= 1
    with pytest.raises(FitError, match="file CRC"):
        fix_fit_bytes(bytes(bad))


def test_no_session_raises() -> None:
    body = bytearray()
    body.append(0x40)
    body.append(0)
    body.append(0)
    body += struct.pack("<H", 0)
    body.append(0)
    body.append(0x00)
    header = bytearray(14)
    header[0] = 14
    header[1] = 32
    header[2:4] = struct.pack("<H", 2156)
    header[4:8] = struct.pack("<I", len(body))
    header[8:12] = b".FIT"
    header[12:14] = struct.pack("<H", fit_crc(bytes(header[:12])))
    full = bytes(header) + bytes(body)
    full += struct.pack("<H", fit_crc(full))
    with pytest.raises(FitError, match="no session"):
        fix_fit_bytes(full)


def test_no_activity_raises() -> None:
    src = _broken_mywhoosh_bytes()
    header_size = src[0]
    header = bytearray(src[:header_size])
    body = src[header_size:header_size + 36]
    header[4:8] = struct.pack("<I", len(body))
    header[12:14] = struct.pack("<H", fit_crc(header[:12]))
    full = bytes(header) + body
    full += struct.pack("<H", fit_crc(full))
    with pytest.raises(FitError, match="no activity"):
        fix_fit_bytes(full)


def test_disk_roundtrip_creates_fixed_file(tmp_path: Path) -> None:
    src = tmp_path / "ride.fit"
    src.write_bytes(_broken_mywhoosh_bytes())
    report = fix_fit(src)
    assert report.output_path == tmp_path / "ride_fixed.fit"
    assert report.output_path.exists()
    assert report.wrote_output is True


def test_disk_skip_when_correct(tmp_path: Path) -> None:
    src = tmp_path / "ride.fit"
    src.write_bytes(_correct_bytes())
    report = fix_fit(src)
    assert report.wrote_output is False
    assert not (tmp_path / "ride_fixed.fit").exists()


def test_explicit_output_copies_correct_file(tmp_path: Path) -> None:
    src = tmp_path / "ride.fit"
    output = tmp_path / "out.fit"
    original = _correct_bytes()
    src.write_bytes(original)
    report = fix_fit(src, output)
    assert report.was_already_correct
    assert report.wrote_output is True
    assert report.output_path == output
    assert output.read_bytes() == original


def test_disk_in_place_overwrites(tmp_path: Path) -> None:
    src = tmp_path / "ride.fit"
    src.write_bytes(_broken_mywhoosh_bytes())
    report = fix_fit(src, in_place=True)
    assert report.output_path == src
    assert report.wrote_output is True
    p = _parse(src.read_bytes())
    assert p["session_ts"] == SESSION_END


def test_disk_in_place_skips_unchanged_file(tmp_path: Path) -> None:
    src = tmp_path / "ride.fit"
    original = _correct_bytes()
    src.write_bytes(original)
    report = fix_fit(src, in_place=True)
    assert report.wrote_output is False
    assert src.read_bytes() == original


def test_disk_collision_appends_suffix(tmp_path: Path) -> None:
    src = tmp_path / "ride.fit"
    src.write_bytes(_broken_mywhoosh_bytes())
    (tmp_path / "ride_fixed.fit").write_bytes(b"old")
    report = fix_fit(src)
    assert report.output_path == tmp_path / "ride_fixed_2.fit"


def test_disk_collision_with_overwrite(tmp_path: Path) -> None:
    src = tmp_path / "ride.fit"
    src.write_bytes(_broken_mywhoosh_bytes())
    pre = tmp_path / "ride_fixed.fit"
    pre.write_bytes(b"old")
    report = fix_fit(src, overwrite=True)
    assert report.output_path == pre
    assert pre.read_bytes() != b"old"


def test_explicit_output_does_not_overwrite_by_default(tmp_path: Path) -> None:
    src = tmp_path / "ride.fit"
    src.write_bytes(_broken_mywhoosh_bytes())
    output = tmp_path / "out.fit"
    output.write_bytes(b"keep")
    with pytest.raises(FitError, match="already exists"):
        fix_fit(src, output)
    assert output.read_bytes() == b"keep"


def test_explicit_output_cannot_be_input(tmp_path: Path) -> None:
    src = tmp_path / "ride.fit"
    original = _broken_mywhoosh_bytes()
    src.write_bytes(original)
    with pytest.raises(FitError, match="use --in-place"):
        fix_fit(src, src, overwrite=True)
    assert src.read_bytes() == original


def test_output_and_in_place_are_mutually_exclusive(tmp_path: Path) -> None:
    src = tmp_path / "ride.fit"
    src.write_bytes(_broken_mywhoosh_bytes())
    with pytest.raises(FitError, match="cannot be used together"):
        fix_fit(src, tmp_path / "out.fit", in_place=True)


def test_disk_missing_input_raises(tmp_path: Path) -> None:
    with pytest.raises(FitError, match="not found"):
        fix_fit(tmp_path / "nope.fit")


def test_main_cli_returns_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from fix_fit import main

    src = tmp_path / "ride.fit"
    src.write_bytes(_broken_mywhoosh_bytes())
    rc = main([str(src), "--no-gui"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "OK" in out


def test_main_cli_skip_message(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from fix_fit import main

    src = tmp_path / "ride.fit"
    src.write_bytes(_correct_bytes())
    rc = main([str(src), "--no-gui"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "SKIP" in out


def test_main_cli_reports_copy_for_correct_explicit_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from fix_fit import main

    src = tmp_path / "ride.fit"
    output = tmp_path / "out.fit"
    src.write_bytes(_correct_bytes())
    rc = main([str(src), "-o", str(output), "--no-gui"])
    assert rc == 0
    assert output.exists()
    assert "COPY" in capsys.readouterr().out


def test_main_cli_returns_nonzero_on_bad_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from fix_fit import main

    bad = tmp_path / "bad.fit"
    bad.write_bytes(b"not a fit file at all")
    rc = main([str(bad), "--no-gui"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "FAIL" in err


def test_main_cli_uses_console_when_output_is_redirected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    import fix_fit as module

    src = tmp_path / "ride.fit"
    src.write_bytes(_broken_mywhoosh_bytes())

    def fail_gui(*_args, **_kwargs):
        raise AssertionError("GUI should not be used")

    monkeypatch.setattr(module, "_gui_notify", fail_gui)
    rc = module.main([str(src)])
    assert rc == 0
    assert "OK" in capsys.readouterr().out


def test_main_gui_cancel_exits_cleanly(monkeypatch) -> None:
    import fix_fit as module

    def fail_notify(*_args, **_kwargs):
        raise AssertionError("cancel should be silent")

    monkeypatch.setattr(module, "_has_console", lambda: False)
    monkeypatch.setattr(module, "_gui_pick_files", lambda: [])
    monkeypatch.setattr(module, "_gui_notify", fail_notify)
    assert module.main([]) == 0


def test_main_gui_picker_failure_is_reported(monkeypatch) -> None:
    import fix_fit as module

    notifications = []
    monkeypatch.setattr(module, "_has_console", lambda: False)
    monkeypatch.setattr(module, "_gui_pick_files", lambda: None)
    monkeypatch.setattr(module, "_gui_notify", lambda ok, body: notifications.append((ok, body)))
    assert module.main([]) == 1
    assert notifications == [(False, "Could not open the file picker.")]


def test_end_utc_matches_session_end() -> None:
    _, report = fix_fit_bytes(_broken_mywhoosh_bytes())
    expected = FIT_EPOCH + timedelta(seconds=SESSION_END)
    assert report.end_utc == expected


def test_end_local_has_requested_timezone() -> None:
    tz = timezone(timedelta(hours=2))
    _, report = fix_fit_bytes(_broken_mywhoosh_bytes(), tz=tz)
    assert report.end_local.utcoffset() == timedelta(hours=2)
    assert report.end_local == report.end_utc.astimezone(tz)


def test_chained_fit_files_rejected() -> None:
    chained = _broken_mywhoosh_bytes() + _correct_bytes()
    with pytest.raises(FitError, match="chained FIT"):
        fix_fit_bytes(chained)


def test_invalid_arch_byte_rejected() -> None:
    bad = bytearray(_broken_mywhoosh_bytes())
    body_start = bad[0]
    bad[body_start + 2] = 0x7F
    bad[-2:] = struct.pack("<H", fit_crc(bytes(bad[:-2])))
    with pytest.raises(FitError, match="architecture"):
        fix_fit_bytes(bytes(bad))


def test_atomic_write_no_tmp_left_behind(tmp_path: Path) -> None:
    src = tmp_path / "ride.fit"
    src.write_bytes(_broken_mywhoosh_bytes())
    fix_fit(src)
    leftovers = [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def _parse_file_id(buf: bytes) -> tuple[int, int, int]:
    from fix_fit import (
        F_FILE_ID_MANUFACTURER,
        F_FILE_ID_PRODUCT,
        F_FILE_ID_SERIAL,
        MSG_FILE_ID,
        _walk,
    )

    msgs, _, _, _ = _walk(buf)
    fi = next(m for m in msgs if m.global_num == MSG_FILE_ID)
    return (
        struct.unpack_from(fi.endian + "H", buf, fi.offsets[F_FILE_ID_MANUFACTURER][0])[0],
        struct.unpack_from(fi.endian + "H", buf, fi.offsets[F_FILE_ID_PRODUCT][0])[0],
        struct.unpack_from(fi.endian + "I", buf, fi.offsets[F_FILE_ID_SERIAL][0])[0],
    )


def _parse_creator_device(buf: bytes) -> tuple[int, int, int]:
    from fix_fit import (
        F_DEVICE_INDEX,
        F_DEVICE_MANUFACTURER,
        F_DEVICE_PRODUCT,
        MSG_DEVICE_INFO,
        _walk,
    )

    msgs, _, _, _ = _walk(buf)
    device = next(m for m in msgs if m.global_num == MSG_DEVICE_INFO)
    return (
        buf[device.offsets[F_DEVICE_INDEX][0]],
        struct.unpack_from(
            device.endian + "H", buf, device.offsets[F_DEVICE_MANUFACTURER][0]
        )[0],
        struct.unpack_from(
            device.endian + "H", buf, device.offsets[F_DEVICE_PRODUCT][0]
        )[0],
    )


def _parse_creator_name(buf: bytes) -> str | None:
    from fix_fit import F_DEVICE_PRODUCT_NAME, MSG_DEVICE_INFO, _walk

    msgs, _, _, _ = _walk(buf)
    device = next(m for m in msgs if m.global_num == MSG_DEVICE_INFO)
    field = device.offsets.get(F_DEVICE_PRODUCT_NAME)
    if field is None:
        return None
    return buf[field[0]:field[0] + field[1]].split(b"\0", 1)[0].decode("ascii")


def test_mimic_zwift_patches_file_id() -> None:
    src = _make_fit(
        session_start=SESSION_START,
        session_elapsed_ms=ELAPSED_MS,
        session_ts=SESSION_END,
        activity_ts=SESSION_END,
        activity_local_ts=SESSION_END,
        file_id=(331, 3570, 3313379353),
    )
    fixed, report = fix_fit_bytes(src, mimic_zwift=True)
    assert report.fields_patched == 3
    assert report.messages_added == 1
    assert _parse_file_id(fixed) == (260, 0, 0)
    assert _parse_creator_device(fixed) == (0, 260, 0)
    assert _parse_creator_name(fixed) == "Zwift"
    assert fit_crc(fixed[:12]) == struct.unpack_from("<H", fixed, 12)[0]
    assert fit_crc(fixed[:-2]) == struct.unpack_from("<H", fixed, len(fixed) - 2)[0]


def test_mimic_zwift_patches_existing_creator_device() -> None:
    src = _make_fit(
        session_start=SESSION_START,
        session_elapsed_ms=ELAPSED_MS,
        session_ts=SESSION_END,
        activity_ts=SESSION_END,
        activity_local_ts=SESSION_END,
        file_id=(331, 3570, 3313379353),
    )
    device = bytes(
        [
            0x43,
            0,
            0,
            23,
            0,
            3,
            0,
            1,
            0x02,
            2,
            2,
            0x84,
            4,
            2,
            0x84,
            0x03,
            0,
        ]
    ) + struct.pack("<HH", 331, 3570)
    fixed, report = fix_fit_bytes(_insert_records(src, 24, device), mimic_zwift=True)
    assert report.fields_patched == 5
    assert report.messages_added == 0
    assert _parse_creator_device(fixed) == (0, 260, 0)


def test_mimic_zwift_does_not_clobber_used_local_message() -> None:
    src = _make_fit(
        session_start=SESSION_START,
        session_elapsed_ms=ELAPSED_MS,
        session_ts=SESSION_END,
        activity_ts=SESSION_END,
        activity_local_ts=SESSION_END,
        file_id=(331, 3570, 3313379353),
    )
    definition = bytes([0x4F, 0, 0, 20, 0, 1, 3, 1, 0x02])
    src = _prepend_records(src, definition)
    src = _insert_records(src, len(definition) + 24, bytes([0x0F, 99]))
    fixed, report = fix_fit_bytes(src, mimic_zwift=True)
    assert report.messages_added == 1
    assert _parse_creator_device(fixed) == (0, 260, 0)


def test_mimic_zwift_idempotent() -> None:
    src = _make_fit(
        session_start=SESSION_START,
        session_elapsed_ms=ELAPSED_MS,
        session_ts=SESSION_END,
        activity_ts=SESSION_END,
        activity_local_ts=SESSION_END,
        file_id=(260, 0, 0),
    )
    fixed, report = fix_fit_bytes(src, mimic_zwift=True)
    assert report.fields_patched == 0
    assert fixed == src


def test_mimic_zwift_leaves_other_manufacturers_alone() -> None:
    src = _make_fit(
        session_start=SESSION_START,
        session_elapsed_ms=ELAPSED_MS,
        session_ts=SESSION_END,
        activity_ts=SESSION_START,
        activity_local_ts=SESSION_START + 7200,
        file_id=(265, 101, 123),
    )
    device = bytes(
        [
            0x43,
            0,
            0,
            23,
            0,
            3,
            0,
            1,
            0x02,
            2,
            2,
            0x84,
            4,
            2,
            0x84,
            0x03,
            0,
        ]
    ) + struct.pack("<HH", 331, 3570)
    src = _insert_records(src, 24, device)
    fixed, report = fix_fit_bytes(src, mimic_zwift=True)
    assert report.was_already_correct
    assert fixed == src


def test_mimic_zwift_without_file_id_is_noop() -> None:
    src = _broken_mywhoosh_bytes()
    _, with_mimic = fix_fit_bytes(src, mimic_zwift=True)
    _, no_mimic = fix_fit_bytes(src, mimic_zwift=False)
    assert with_mimic.fields_patched == no_mimic.fields_patched


def test_mimic_zwift_combined_with_timestamp_fix() -> None:
    src = _make_fit(
        session_start=SESSION_START,
        session_elapsed_ms=ELAPSED_MS,
        session_ts=SESSION_START,
        activity_ts=SESSION_START,
        activity_local_ts=UNIX_LOCAL_TS,
        file_id=(331, 3570, 3313379353),
    )
    fixed, report = fix_fit_bytes(src, mimic_zwift=True)
    assert report.fields_patched == 6
    assert report.messages_added == 1
    assert _parse_file_id(fixed) == (260, 0, 0)


def test_mimic_garmin_patches_creator_as_edge_530() -> None:
    src = _make_fit(
        session_start=SESSION_START,
        session_elapsed_ms=ELAPSED_MS,
        session_ts=SESSION_START,
        activity_ts=SESSION_START,
        activity_local_ts=UNIX_LOCAL_TS,
        file_id=(331, 3570, 3313379353),
    )
    fixed, report = fix_fit_bytes(src, mimic_garmin=True)
    assert report.fields_patched == 5
    assert report.messages_added == 1
    assert _parse_file_id(fixed) == (1, 3121, 3313379353)
    assert _parse_creator_device(fixed) == (0, 1, 3121)
    assert _parse_creator_name(fixed) == "Edge 530"


def test_mimic_garmin_rejects_non_virtual_activity() -> None:
    src = bytearray(
        _make_fit(
            session_start=SESSION_START,
            session_elapsed_ms=ELAPSED_MS,
            session_ts=SESSION_START,
            activity_ts=SESSION_START,
            activity_local_ts=UNIX_LOCAL_TS,
            file_id=(331, 3570, 3313379353),
        )
    )
    session_data = src[0] + 24 + 21
    src[session_data + 14] = 0
    src[-2:] = struct.pack("<H", fit_crc(src[:-2]))
    with pytest.raises(FitError, match="only accepts cycling / virtual_activity"):
        fix_fit_bytes(bytes(src), mimic_garmin=True)


def test_mimic_modes_are_mutually_exclusive() -> None:
    with pytest.raises(FitError, match="cannot be used together"):
        fix_fit_bytes(_broken_mywhoosh_bytes(), mimic_zwift=True, mimic_garmin=True)


def test_atomic_write_keeps_original_on_failure(tmp_path: Path, monkeypatch) -> None:
    src = tmp_path / "ride.fit"
    src.write_bytes(_broken_mywhoosh_bytes())
    existing = tmp_path / "ride_fixed.fit"
    existing.write_bytes(b"original-content")

    def boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        fix_fit(src, overwrite=True)
    assert existing.read_bytes() == b"original-content"
    leftovers = [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_normalized_power_constant_input() -> None:
    from fix_fit import _normalized_power

    assert _normalized_power([200] * 600) == pytest.approx(200.0)
    assert _normalized_power([0] * 30) == 0.0
    assert _normalized_power([]) == 0.0


def test_normalized_power_short_series_uses_average() -> None:
    from fix_fit import _normalized_power

    assert _normalized_power([100, 200, 300]) == pytest.approx(200.0)


def test_compute_metrics_with_power_produces_coggan_values() -> None:
    from fix_fit import _compute_metrics

    times = list(range(3600))
    powers = [200] * 3600
    m = _compute_metrics(times, powers, ftp=250)
    assert m["np"] == 200
    assert m["if"] == pytest.approx(0.8, abs=0.01)
    assert m["tss"] == pytest.approx(64.0, abs=1.0)


def test_compute_metrics_no_power_returns_zeros() -> None:
    from fix_fit import _compute_metrics

    assert _compute_metrics([], [], ftp=250) == {"np": 0, "if": 0.0, "tss": 0.0}
    assert _compute_metrics(list(range(60)), [0] * 60, ftp=250) == {"np": 0, "if": 0.0, "tss": 0.0}


def test_compute_metrics_requires_positive_ftp() -> None:
    from fix_fit import _compute_metrics

    assert _compute_metrics(list(range(60)), [200] * 60, ftp=0) == {"np": 0, "if": 0.0, "tss": 0.0}


def test_inject_metrics_without_ftp_raises() -> None:
    with pytest.raises(FitError, match="ftp"):
        fix_fit_bytes(_broken_mywhoosh_bytes(), inject_metrics=True)


def test_profile_garmin_edge_matches_mimic_garmin() -> None:
    src = _make_fit(
        session_start=SESSION_START,
        session_elapsed_ms=ELAPSED_MS,
        session_ts=SESSION_END,
        activity_ts=SESSION_END,
        activity_local_ts=SESSION_END + 7200,
        file_id=(331, 3570, 3313379353),
    )
    a, _ = fix_fit_bytes(src, profile="garmin-edge")
    b, _ = fix_fit_bytes(src, mimic_garmin=True)
    assert a == b


def test_profile_zwift_matches_mimic_zwift() -> None:
    src = _make_fit(
        session_start=SESSION_START,
        session_elapsed_ms=ELAPSED_MS,
        session_ts=SESSION_END,
        activity_ts=SESSION_END,
        activity_local_ts=SESSION_END + 7200,
        file_id=(331, 3570, 3313379353),
    )
    a, _ = fix_fit_bytes(src, profile="zwift")
    b, _ = fix_fit_bytes(src, mimic_zwift=True)
    assert a == b


def test_unknown_profile_raises() -> None:
    with pytest.raises(FitError, match="unknown profile"):
        fix_fit_bytes(_broken_mywhoosh_bytes(), profile="bogus")


def test_profile_and_mimic_together_raises() -> None:
    with pytest.raises(FitError, match="cannot be combined"):
        fix_fit_bytes(_broken_mywhoosh_bytes(), profile="zwift", mimic_zwift=True)


def test_profile_rouvy_patches_file_id() -> None:
    from fix_fit import ROUVY_MANUFACTURER

    src = _make_fit(
        session_start=SESSION_START,
        session_elapsed_ms=ELAPSED_MS,
        session_ts=SESSION_END,
        activity_ts=SESSION_END,
        activity_local_ts=SESSION_END + 7200,
        file_id=(331, 3570, 3313379353),
    )
    fixed, _ = fix_fit_bytes(src, profile="rouvy")
    parsed = _parse_file_id(fixed)
    assert parsed == (ROUVY_MANUFACTURER, 0, 0)


def test_analyze_mywhoosh_detects_source_and_unix_shift() -> None:
    from fix_fit import analyze_fit

    r = analyze_fit(_broken_mywhoosh_bytes())
    assert r["sessions"]
    a = r["activity"]
    assert a["local_timestamp_unix_shifted"] is True
    assert any("Unix-shifted" in w for w in r["warnings"])


def test_analyze_correct_file_no_warnings() -> None:
    from fix_fit import analyze_fit

    r = analyze_fit(_correct_bytes())
    assert r["activity"]["local_timestamp_unix_shifted"] is False
    assert all("Unix-shifted" not in w for w in r["warnings"])


def test_analyze_reports_message_counts() -> None:
    from fix_fit import analyze_fit, MSG_SESSION, MSG_ACTIVITY

    r = analyze_fit(_broken_mywhoosh_bytes())
    counts = r["message_counts"]
    assert counts.get(MSG_SESSION) == 1
    assert counts.get(MSG_ACTIVITY) == 1


def test_compare_emits_markdown_table(tmp_path: Path) -> None:
    from fix_fit import compare_fits

    a = tmp_path / "a.fit"
    b = tmp_path / "b.fit"
    a.write_bytes(_broken_mywhoosh_bytes())
    b.write_bytes(_correct_bytes())
    md = compare_fits([a, b])
    assert "# FIT comparison" in md
    assert "| field |" in md
    assert "| source |" in md
    assert "Message types" in md


def test_matrix_creates_six_variants_and_doc(tmp_path: Path) -> None:
    from fix_fit import build_test_matrix

    src = tmp_path / "ride.fit"
    src.write_bytes(_broken_mywhoosh_bytes())
    out_dir = tmp_path / "variants"
    result = build_test_matrix(src, out_dir)
    assert len(result["results"]) == 6
    files = sorted(p.name for p in out_dir.iterdir())
    assert "test_matrix.md" in files
    expected_variants = [
        "01_timestamp_fixed_only.fit",
        "02_garmin_edge_indoor.fit",
        "03_garmin_forerunner_indoor.fit",
        "04_zwift_virtual.fit",
        "05_rouvy_virtual.fit",
        "06_tacx_indoor.fit",
    ]
    for name in expected_variants:
        assert (out_dir / name).is_file()


def test_matrix_md_has_test_procedure(tmp_path: Path) -> None:
    from fix_fit import build_test_matrix

    src = tmp_path / "ride.fit"
    src.write_bytes(_broken_mywhoosh_bytes())
    out_dir = tmp_path / "v"
    build_test_matrix(src, out_dir)
    md = (out_dir / "test_matrix.md").read_text(encoding="utf-8")
    assert "Manual Garmin Connect test procedure" in md
    assert "Acute Load" in md
    assert "Recovery Time" in md


def test_main_analyze_subcommand_json(tmp_path: Path) -> None:
    from fix_fit import main

    src = tmp_path / "ride.fit"
    src.write_bytes(_broken_mywhoosh_bytes())
    out_json = tmp_path / "out.json"
    rc = main(["analyze", str(src), "--json", str(out_json), "--no-gui"])
    assert rc == 0
    payload = out_json.read_text(encoding="utf-8")
    assert "source_heuristic" in payload
    assert "warnings" in payload


def test_main_compare_subcommand_md(tmp_path: Path) -> None:
    from fix_fit import main

    a = tmp_path / "a.fit"
    b = tmp_path / "b.fit"
    a.write_bytes(_broken_mywhoosh_bytes())
    b.write_bytes(_correct_bytes())
    out_md = tmp_path / "cmp.md"
    rc = main(["compare", str(a), str(b), "--md", str(out_md), "--no-gui"])
    assert rc == 0
    assert "# FIT comparison" in out_md.read_text(encoding="utf-8")


def test_main_matrix_subcommand(tmp_path: Path) -> None:
    from fix_fit import main

    src = tmp_path / "ride.fit"
    src.write_bytes(_broken_mywhoosh_bytes())
    out_dir = tmp_path / "v"
    rc = main(["matrix", str(src), "--out-dir", str(out_dir), "--no-gui"])
    assert rc == 0
    assert (out_dir / "test_matrix.md").is_file()


def test_main_legacy_invocation_defaults_to_patch(tmp_path: Path) -> None:
    from fix_fit import main

    src = tmp_path / "ride.fit"
    src.write_bytes(_broken_mywhoosh_bytes())
    rc = main([str(src), "--no-gui"])
    assert rc == 0
    assert (tmp_path / "ride_fixed.fit").is_file()


def test_inject_te_approx_requires_inject_metrics() -> None:
    from fix_fit import main

    rc = main(["patch", "--inject-te-approx", "--no-gui", "missing.fit"])
    assert rc == 2
