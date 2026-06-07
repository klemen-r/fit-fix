"""Tests for the Zwift/Garmin export comparison helpers."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from zwift_compare import compare_bytes, safe_extract_single_fit


def test_compare_bytes_reports_exact_match_and_differences() -> None:
    assert compare_bytes(b"same", b"same") == {
        "identical": True,
        "left_size": 4,
        "right_size": 4,
        "different_byte_count": 0,
        "first_differing_offsets": [],
    }
    result = compare_bytes(b"abc", b"axcd")
    assert result["identical"] is False
    assert result["different_byte_count"] == 2
    assert result["first_differing_offsets"] == [1]


def test_safe_extract_single_fit(tmp_path: Path) -> None:
    archive = tmp_path / "activity.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("activity.fit", b"FIT")

    extracted = safe_extract_single_fit(archive, tmp_path / "output")

    assert extracted.read_bytes() == b"FIT"
    assert extracted.parent == (tmp_path / "output").resolve()


def test_safe_extract_rejects_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("../activity.fit", b"FIT")

    with pytest.raises(ValueError, match="unsafe ZIP entry"):
        safe_extract_single_fit(archive, tmp_path / "output")
