"""Tests for the profile config loader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from profile_config import ProfileConfig, load_profile_config, parse_profile_config


def test_parse_profile_config_accepts_minimal_payload() -> None:
    config = parse_profile_config({"max_hr": 191, "ftp": 142})
    assert config == ProfileConfig(max_hr=191, ftp=142)
    assert not config.is_empty


def test_parse_profile_config_treats_all_null_as_empty() -> None:
    config = parse_profile_config(
        {
            "max_hr": None,
            "resting_hr": None,
            "ftp": None,
            "weight_kg": None,
            "hr_zones": None,
            "power_zones": None,
        }
    )
    assert config.is_empty


def test_parse_profile_config_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError, match="unknown profile config keys"):
        parse_profile_config({"max_heart_rate": 191})


def test_parse_profile_config_rejects_out_of_range_ftp() -> None:
    with pytest.raises(ValueError, match="ftp=5 is out of range"):
        parse_profile_config({"ftp": 5})


def test_parse_profile_config_rejects_non_ascending_zones() -> None:
    with pytest.raises(ValueError, match="hr_zones must be strictly ascending"):
        parse_profile_config({"hr_zones": [100, 100, 120]})


def test_parse_profile_config_accepts_zone_lists() -> None:
    config = parse_profile_config(
        {"hr_zones": [96, 115, 134, 153, 172, 191], "power_zones": [80, 110, 140, 170, 210, 260]}
    )
    assert config.hr_zones == (96, 115, 134, 153, 172, 191)
    assert config.power_zones == (80, 110, 140, 170, 210, 260)


def test_load_profile_config_round_trips(tmp_path: Path) -> None:
    payload = {"max_hr": 185, "ftp": 220, "weight_kg": 72.5}
    target = tmp_path / "profile.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    config = load_profile_config(target)
    assert config == ProfileConfig(max_hr=185, ftp=220, weight_kg=72.5)


def test_load_profile_config_none_returns_none() -> None:
    assert load_profile_config(None) is None


def test_as_dict_preserves_lists() -> None:
    config = ProfileConfig(hr_zones=(96, 115))
    assert config.as_dict()["hr_zones"] == [96, 115]


def test_parse_profile_config_accepts_sub_sport_override() -> None:
    config = parse_profile_config({"sub_sport": 7})
    assert config.sub_sport == 7
    assert not config.is_empty


def test_parse_profile_config_rejects_invalid_sub_sport() -> None:
    with pytest.raises(ValueError, match="sub_sport=999"):
        parse_profile_config({"sub_sport": 999})
