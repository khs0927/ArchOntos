"""site-pipeline parcel facts -> ``land_parcel`` adapter (recorded shapes, synthetic values)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from archontos.cad.checks import (
    CadCheckInputError,
    land_parcel_facts,
    land_parcel_from_site_pipeline,
)

FIXTURE: dict[str, Any] = json.loads(
    (Path(__file__).parent / "contracts" / "site_pipeline_parcel_facts.json").read_text(
        encoding="utf-8"
    )
)
PNU = "1111010100100010000"


def test_flat_facts_prefer_ledger_area_and_merge_districts_and_areas() -> None:
    parcel = land_parcel_from_site_pipeline(FIXTURE["flat"])
    assert parcel == {
        "pnu": PNU,
        "zone": "제2종일반주거지역",
        "districts": ["지구단위계획구역", "최고고도지구"],
        "site_area_m2": 500.0,  # 토지대장 wins over the 498.6 cadastral polygon area
    }
    # nothing else leaks in: road widths, district_plan, road_side are not parcel facts
    assert set(parcel) <= {"pnu", "zone", "districts", "site_area_m2"}


def test_cache_file_envelope_and_cadastral_area_fallback() -> None:
    parcel = land_parcel_from_site_pipeline(FIXTURE["cache_file"])
    assert parcel == {
        "pnu": PNU,
        "zone": "제2종일반주거지역",
        "districts": ["최고고도지구"],
        "site_area_m2": 498.6,
    }


def test_brief_facts_used_shape() -> None:
    parcel = land_parcel_from_site_pipeline({"facts_used": FIXTURE["brief_facts_used"]})
    assert parcel == {"pnu": PNU, "zone": "일반상업지역", "site_area_m2": 612.0}


def test_result_is_accepted_by_run_checks_normaliser() -> None:
    parcel = land_parcel_from_site_pipeline(FIXTURE["flat"])
    facts, units = land_parcel_facts(parcel)
    assert facts["land_parcel"] == {
        "pnu": PNU,
        "zone": "제2종일반주거지역",
        "districts": ["지구단위계획구역", "최고고도지구"],
        "site_area": 500.0,
    }
    assert units == {"land_parcel.site_area": "m2"}


def test_missing_zone_stays_missing_and_empty_input_gives_empty_parcel() -> None:
    two_zones = {k: v for k, v in FIXTURE["flat"].items() if k != "zone"}
    assert "zone" not in land_parcel_from_site_pipeline(two_zones)  # fail closed -> REVIEW
    assert land_parcel_from_site_pipeline({}) == {}
    assert land_parcel_facts(land_parcel_from_site_pipeline({})) == ({}, {})


@pytest.mark.parametrize(
    "bad",
    [
        {"pnu": "123"},
        {"pnu": 1111010100100010000},
        {"districts": "최고고도지구"},
        {"areas": [1]},
        {"land_ledger_area_m2": 0},
        {"land_ledger_area_m2": -5},
        {"land_ledger_area_m2": "500"},
        {"land_ledger_area_m2": True},
        {"land_ledger_area_m2": float("nan")},
        {"parcel_area_m2": float("inf")},
        {"facts_used": ["x"]},
    ],
)
def test_invalid_values_are_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises(CadCheckInputError):
        land_parcel_from_site_pipeline(bad)


def test_non_object_is_rejected() -> None:
    with pytest.raises(CadCheckInputError):
        land_parcel_from_site_pipeline([])  # type: ignore[arg-type]
