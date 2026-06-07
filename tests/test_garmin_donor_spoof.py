"""Focused tests for Garmin donor spoof variant helpers."""

from pathlib import Path

import pytest

from fix_fit import MSG_DEVICE_INFO, _parse_fit, _read_uint
from garmin_donor_spoof import (
    VARIANT_NAMES,
    _device_payload,
    _load_input,
    _replace_uint_preserving,
    build_spoof_variants,
)
from garmin_pipeline import _messages_of


MYWHOOSH = Path.home() / "Downloads" / "MyWhoosh_Limmat_Loop.fit"
DONOR = Path.home() / "Downloads" / "23128003580.zip"


@pytest.mark.skipif(
    not MYWHOOSH.is_file() or not DONOR.is_file(),
    reason="local MyWhoosh/donor fixtures are unavailable",
)
def test_build_spoof_variants_preserves_source_and_device_payloads(tmp_path: Path) -> None:
    paths, report, validation = build_spoof_variants(MYWHOOSH, DONOR, tmp_path)
    assert tuple(paths) == VARIANT_NAMES
    assert report.is_file()
    for name, path in paths.items():
        assert path.is_file()
        result = validation[name]
        assert result["internal_decode"] == "pass"
        assert result["crc"] == "pass"
        assert result["garmin_fit_sdk"] == "pass"
        assert result["record_stream_matches_mywhoosh"]
        assert result["summary_totals_match_mywhoosh"]
        assert result["all_donor_device_info_payloads_copied"]
        assert result["file_identity_payload_matches_donor"]
        assert result["file_time_created_matches_mywhoosh"]
        assert result["donor_timer_event_payloads_copied"]
        assert result["copied_profile_payloads_exact"]
        assert result["no_donor_field_253_timestamps"]
        assert result["target_sport_is_cycling_indoor_cycling"]
        assert result["proprietary_session_values_not_copied"]
        assert result["donor_gps_and_run_sample_message_types_absent"]


@pytest.mark.skipif(not DONOR.is_file(), reason="local donor fixture is unavailable")
def test_device_payload_ignores_only_timestamp() -> None:
    donor = _load_input(DONOR)
    _header, messages = _parse_fit(donor.data)
    devices = _messages_of(messages, MSG_DEVICE_INFO)
    assert len(devices) == 32
    first = devices[0]
    timestamp = _read_uint(first, 253)
    assert timestamp is not None
    shifted = _replace_uint_preserving(first, 253, timestamp + 1)
    assert _device_payload(first) == _device_payload(shifted)
