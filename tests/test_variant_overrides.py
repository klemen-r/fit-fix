"""Unit tests for the profile-zones override helpers."""

from __future__ import annotations

from fix_fit import (
    F_MESSAGE_INDEX,
    Message,
    TYPE_UINT8,
    TYPE_UINT16,
    _pack_field,
    _read_uint,
)
from garmin_pipeline import (
    MSG_HR_ZONE,
    MSG_POWER_ZONE,
    MSG_USER_PROFILE,
    MSG_ZONES_TARGET,
    _build_hr_zone_messages,
    _build_power_zone_messages,
    _override_user_profile,
    _override_zones_target,
)
from profile_config import ProfileConfig


def _zones_target_message() -> Message:
    return Message(
        MSG_ZONES_TARGET,
        "<",
        (
            _pack_field("<", 1, TYPE_UINT8, 180),
            _pack_field("<", 3, TYPE_UINT16, 200),
        ),
    )


def _user_profile_message() -> Message:
    return Message(
        MSG_USER_PROFILE,
        "<",
        (
            _pack_field("<", 4, TYPE_UINT16, 700),  # 70.0 kg
            _pack_field("<", 8, TYPE_UINT8, 60),
            _pack_field("<", 10, TYPE_UINT8, 180),
            _pack_field("<", 11, TYPE_UINT8, 180),
        ),
    )


def test_override_zones_target_replaces_max_hr_and_ftp() -> None:
    overridden = _override_zones_target(
        _zones_target_message(),
        ProfileConfig(max_hr=191, ftp=142),
    )
    assert _read_uint(overridden, 1) == 191
    assert _read_uint(overridden, 3) == 142


def test_override_zones_target_no_config_changes_keeps_values() -> None:
    overridden = _override_zones_target(_zones_target_message(), ProfileConfig())
    assert _read_uint(overridden, 1) == 180
    assert _read_uint(overridden, 3) == 200


def test_override_user_profile_overrides_supplied_fields_only() -> None:
    overridden = _override_user_profile(
        _user_profile_message(),
        ProfileConfig(max_hr=191, resting_hr=48, weight_kg=72.3),
    )
    assert _read_uint(overridden, 4) == 723
    assert _read_uint(overridden, 8) == 48
    assert _read_uint(overridden, 10) == 191
    assert _read_uint(overridden, 11) == 191


def test_build_hr_zone_messages_writes_message_index_and_bpm() -> None:
    messages = _build_hr_zone_messages([96, 115, 134])
    assert len(messages) == 3
    assert all(message.global_message == MSG_HR_ZONE for message in messages)
    for index, expected in enumerate([96, 115, 134]):
        assert _read_uint(messages[index], F_MESSAGE_INDEX) == index
        assert _read_uint(messages[index], 1) == expected


def test_build_power_zone_messages_writes_uint16_values() -> None:
    messages = _build_power_zone_messages([80, 250])
    assert len(messages) == 2
    assert all(message.global_message == MSG_POWER_ZONE for message in messages)
    assert _read_uint(messages[1], 1) == 250
    assert _read_uint(messages[1], F_MESSAGE_INDEX) == 1


def test_injected_variant_time_in_zone_uses_user_boundaries() -> None:
    import struct

    from fix_fit import MSG_SESSION
    from garmin_pipeline import MSG_TIME_IN_ZONE, _messages_of

    from pathlib import Path

    path = Path("outputs/variants/MyWhoosh_Limmat_Loop_07_profile_zones_injected.fit")
    if not path.is_file():
        import pytest

        pytest.skip("injected variant not built in this environment")
    from fix_fit import _parse_fit

    _, messages = _parse_fit(path.read_bytes())
    tiz = [
        msg
        for msg in _messages_of(messages, MSG_TIME_IN_ZONE)
        if _read_uint(msg, 0) == MSG_SESSION
    ]
    assert tiz, "session time_in_zone message missing"
    session_tiz = tiz[-1]

    hr_boundary_field = session_tiz.first(6)
    assert hr_boundary_field is not None
    hr_bounds = list(hr_boundary_field.data)
    assert hr_bounds == [120, 160, 179, 199, 205]

    power_boundary_field = session_tiz.first(9)
    assert power_boundary_field is not None
    power_count = power_boundary_field.size // 2
    power_bounds = list(struct.unpack(f"<{power_count}H", power_boundary_field.data))
    assert power_bounds == [88, 120, 144, 168, 192, 240, 999]

    hr_time_field = session_tiz.first(2)
    assert hr_time_field is not None
    hr_time_count = hr_time_field.size // 4
    hr_times = list(struct.unpack(f"<{hr_time_count}I", hr_time_field.data))
    assert len(hr_times) == len(hr_bounds) + 1
    # Total should approximately equal session timer time.
    total_ms = sum(hr_times)
    assert 4_000_000 < total_ms < 6_000_000

    power_time_field = session_tiz.first(5)
    assert power_time_field is not None
    power_time_count = power_time_field.size // 4
    power_times = list(struct.unpack(f"<{power_time_count}I", power_time_field.data))
    assert len(power_times) == len(power_bounds) + 1
    assert sum(power_times) > 0
