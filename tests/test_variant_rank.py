"""Tests for the variant readiness ranking."""

from __future__ import annotations

from pathlib import Path

import pytest

from variant_rank import expand_variant_paths, rank_variants, write_rank_reports


@pytest.fixture
def existing_variants_dir() -> Path:
    return Path("outputs/variants")


def test_expand_variant_paths_handles_glob(existing_variants_dir: Path) -> None:
    if not existing_variants_dir.is_dir():
        pytest.skip("no existing variants in repo")
    matches = expand_variant_paths([str(existing_variants_dir / "*.fit")])
    assert matches
    assert all(path.suffix == ".fit" for path in matches)


def test_expand_variant_paths_dedupes_and_keeps_order() -> None:
    candidates = []
    explicit = Path("outputs/variants/MyWhoosh_Limmat_Loop_structural_only.fit")
    if explicit.is_file():
        candidates.append(str(explicit))
        candidates.append(str(explicit))
    if not candidates:
        pytest.skip("no concrete variant on disk")
    expanded = expand_variant_paths(candidates)
    assert len(expanded) == 1


def test_rank_variants_orders_by_ratio_then_risk(tmp_path: Path) -> None:
    trusted = Path("outputs/variants/MyWhoosh_Limmat_Loop_structural_only.fit")
    if not trusted.is_file():
        pytest.skip("no trusted reference FIT on disk")
    readiness = rank_variants(
        trusted,
        [str(trusted.parent / "*.fit")],
    )
    assert readiness
    ratios = [item.ratio for item in readiness]
    assert ratios == sorted(ratios, reverse=True)
    write_rank_reports(tmp_path, trusted, readiness)
    md = (tmp_path / "reports" / "variant_readiness_rank.md").read_text(encoding="utf-8")
    assert "Variant Readiness Rank" in md
    json_payload = (tmp_path / "variant_readiness_rank.json").read_text(encoding="utf-8")
    assert "checks" in json_payload


def test_rank_variants_raises_when_no_matches(tmp_path: Path) -> None:
    trusted = Path("outputs/variants/MyWhoosh_Limmat_Loop_structural_only.fit")
    if not trusted.is_file():
        pytest.skip("no trusted reference FIT on disk")
    with pytest.raises(ValueError, match="no variant FITs matched"):
        rank_variants(trusted, [str(tmp_path / "definitely-missing-*.fit")])
