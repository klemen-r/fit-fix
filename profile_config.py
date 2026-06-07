"""Optional user-supplied profile config for the build pipeline.

The config is a small JSON document. All keys are optional and may be ``null``.
Nothing is invented if a key is missing - the donor-derived value is kept.

Example::

    {
      "max_hr": 191,
      "resting_hr": null,
      "ftp": 142,
      "weight_kg": null,
      "hr_zones": null,
      "power_zones": null
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence


@dataclass(frozen=True)
class ProfileConfig:
    max_hr: Optional[int] = None
    resting_hr: Optional[int] = None
    ftp: Optional[int] = None
    weight_kg: Optional[float] = None
    hr_zones: Optional[tuple[int, ...]] = None
    power_zones: Optional[tuple[int, ...]] = None
    sub_sport: Optional[int] = None

    @property
    def is_empty(self) -> bool:
        return all(
            getattr(self, name) is None
            for name in (
                "max_hr",
                "resting_hr",
                "ftp",
                "weight_kg",
                "hr_zones",
                "power_zones",
                "sub_sport",
            )
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_hr": self.max_hr,
            "resting_hr": self.resting_hr,
            "ftp": self.ftp,
            "weight_kg": self.weight_kg,
            "hr_zones": list(self.hr_zones) if self.hr_zones is not None else None,
            "power_zones": list(self.power_zones) if self.power_zones is not None else None,
            "sub_sport": self.sub_sport,
        }


_KNOWN_KEYS = {
    "max_hr",
    "resting_hr",
    "ftp",
    "weight_kg",
    "hr_zones",
    "power_zones",
    "sub_sport",
}


def _coerce_int(value: Any, field: str, *, minimum: int = 1, maximum: int = 255) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer or null, got {type(value).__name__}")
    if not minimum <= value <= maximum:
        raise ValueError(f"{field}={value} is out of range [{minimum}, {maximum}]")
    return value


def _coerce_float(value: Any, field: str, *, minimum: float = 0.1, maximum: float = 1000.0) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number or null, got {type(value).__name__}")
    if not minimum <= value <= maximum:
        raise ValueError(f"{field}={value} is out of range [{minimum}, {maximum}]")
    return float(value)


def _coerce_zones(
    value: Any, field: str, *, minimum: int = 1, maximum: int = 2000
) -> Optional[tuple[int, ...]]:
    if value is None:
        return None
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must be a non-empty list of integers or null")
    boundaries: list[int] = []
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, int):
            raise ValueError(f"{field}[{index}] must be an integer, got {type(item).__name__}")
        if not minimum <= item <= maximum:
            raise ValueError(f"{field}[{index}]={item} is out of range [{minimum}, {maximum}]")
        boundaries.append(item)
    if any(b <= a for a, b in zip(boundaries, boundaries[1:])):
        raise ValueError(f"{field} must be strictly ascending, got {boundaries}")
    return tuple(boundaries)


def parse_profile_config(payload: dict[str, Any]) -> ProfileConfig:
    """Validate a parsed JSON document and return a ProfileConfig.

    Unknown keys are rejected so typos do not silently no-op.
    """

    if not isinstance(payload, dict):
        raise ValueError("profile config must be a JSON object")
    unknown = sorted(set(payload) - _KNOWN_KEYS)
    if unknown:
        raise ValueError(f"unknown profile config keys: {unknown}")
    return ProfileConfig(
        max_hr=_coerce_int(payload.get("max_hr"), "max_hr", minimum=80, maximum=240),
        resting_hr=_coerce_int(payload.get("resting_hr"), "resting_hr", minimum=20, maximum=120),
        ftp=_coerce_int(payload.get("ftp"), "ftp", minimum=40, maximum=600),
        weight_kg=_coerce_float(payload.get("weight_kg"), "weight_kg", minimum=20.0, maximum=250.0),
        hr_zones=_coerce_zones(payload.get("hr_zones"), "hr_zones", minimum=40, maximum=240),
        power_zones=_coerce_zones(payload.get("power_zones"), "power_zones", minimum=20, maximum=2000),
        sub_sport=_coerce_int(payload.get("sub_sport"), "sub_sport", minimum=0, maximum=100),
    )


def load_profile_config(path: Optional[Path]) -> Optional[ProfileConfig]:
    if path is None:
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return parse_profile_config(payload)


def merge_zones(
    donor_boundaries: Sequence[int], override: Optional[Sequence[int]]
) -> tuple[int, ...]:
    """Return the override if provided, else the donor boundaries."""

    if override is not None:
        return tuple(override)
    return tuple(donor_boundaries)
