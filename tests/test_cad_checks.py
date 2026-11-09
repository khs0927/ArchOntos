"""MVP-2 golden tests: programmatic DXF -> fact space -> in-memory rules -> PASS/FAIL/REVIEW."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from archontos.cad.checks import (
    CadCheckInputError,
    CheckRule,
    extract_fact_space,
    jurisdiction_covers,
    land_parcel_facts,
    normalize_quantity,
    run_checks,
    select_rules,
)
from archontos.cad.dxf import parse_dxf  # noqa: E402  (after importorskip in cad_fixtures)
from archontos.domain.enums import DecisionOutcome
from archontos.rules.compiler import compile_requirement
from tests.cad_fixtures import ezdxf, korean_plan

REF = date(2026, 11, 1)
LOCATOR = {"kind": "subparagraph", "article": "10", "paragraph": "1", "item": "1"}


def _objects(data: bytes) -> list[dict[str, Any]]:
    drawing = parse_dxf(data)
    return [
        {
            "id": o.id,
            "kind": o.kind,
            "properties": o.properties,
            "fact_units": o.fact_units,
            "geometry": o.geometry,
            "source_handles": list(o.source_handles),
        }
        for o in drawing.objects
    ]


def _rule(rid: str, payload: dict[str, Any], **kw: Any) -> CheckRule:
    compiled = compile_requirement(
        natural_language=payload.get("title", rid),
        structured_payload=payload,
        jurisdiction_code="KR",
    )
    return CheckRule(
        rule_version_id=rid,
        title=compiled.title,
        logic_expr=compiled.logic_expr,
        jurisdiction_code=kw.pop("jurisdiction_code", "KR"),
        valid_from=kw.pop("valid_from", date(2026, 1, 1)),
        valid_to=kw.pop("valid_to", None),
        evidence_locators=(LOCATOR,),
        **kw,
    )


def _corridor_plan() -> bytes:
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    msp.add_lwpolyline(
        [(0, 0), (8000, 0), (8000, 1100), (0, 1100)], close=True, dxfattribs={"layer": "실"}
    )
    msp.add_text("복도", dxfattribs={"layer": "실명", "insert": (4000, 500)})
    import io

    stream = io.StringIO()
    doc.write(stream)
    return stream.getvalue().encode()


def _run(rules: list[CheckRule], data: bytes | None = None, **kw: Any):
    space = extract_fact_space(_objects(data or korean_plan()))
    return space, run_checks(rules, space, jurisdiction="KR", reference_date=REF, **kw)


def test_fact_space_units_and_provenance():
    space = extract_fact_space(_objects(korean_plan()))
    drawing = space.facts["drawing"]
    assert drawing["door_count"] == 1 and drawing["space_count"] == 1
    assert drawing["door_min_width"] == pytest.approx(0.9)
    assert space.fact_units["drawing.door_min_width"] == "m"
    assert drawing["space_area_total"] == pytest.approx(24.0)
    assert space.fact_units["drawing.space_area_total"] == "m2"
    assert "drawing.stair_min_width" not in space.fact_units  # no stairs -> absent, not 0
    (door,) = space.objects["door"]
    assert space.sources["drawing.door_min_width"].object_ids == (door.object_id,)
    assert space.sources["drawing.door_min_width"].handles == door.handles
    (room,) = space.objects["space"]
    assert room.values["width"] == pytest.approx(4.0) and room.units["width"] == "m"


def test_unit_normalization():
    assert normalize_quantity({"value": 900, "unit": "mm"}) == (0.9, "m")
    assert normalize_quantity({"value": 25000, "unit": "cm2"}) == (2.5, "m2")
    assert normalize_quantity({"value": 90, "unit": "deg"}) == (90.0, "deg")
    for bad in (
        {"value": True, "unit": "m"},
        {"value": float("nan"), "unit": "m"},
        {"value": 1, "unit": "furlong"},
        {"value": "1", "unit": "m"},
        None,
    ):
        assert normalize_quantity(bad) is None


def test_golden_pass_door_width():
    _space, run = _run(
        [_rule("r-pass", {"fact_path": "door.width", "operator": ">=", "value": 0.85, "unit": "m"})]
    )
    (result,) = run.results
    assert result.outcome is DecisionOutcome.PASS
    assert result.object_ids == () and result.details["object_outcomes"]["PASS"] == 1
    assert result.rule.evidence_locators == (LOCATOR,)


def test_golden_fail_reports_offending_objects_and_handles():
    space, run = _run(
        [
            _rule(
                "r-fail",
                {
                    "fact_path": "door.width",
                    "operator": ">=",
                    "value": 1.2,
                    "unit": "m",
                    "failure_reason": "출입문 유효폭 부족",
                },
            )
        ]
    )
    (result,) = run.results
    (door,) = space.objects["door"]
    assert result.outcome is DecisionOutcome.FAIL
    assert result.reason == "출입문 유효폭 부족"
    assert result.object_ids == (door.object_id,)
    assert result.handles == door.handles and result.handles
    assert run.summary()["fail_count"] == 1


def test_golden_review_missing_fact_never_pass():
    rules = [
        _rule("r-stair", {"fact_path": "stair.width", "operator": ">=", "value": 1.2, "unit": "m"}),
        _rule(
            "r-agg",
            {"fact_path": "drawing.stair_min_width", "operator": ">=", "value": 1.2, "unit": "m"},
        ),
        _rule(
            "r-parcel",
            {"fact_path": "land_parcel.site_area", "operator": ">=", "value": 60, "unit": "m2"},
        ),
    ]
    _space, run = _run(rules)
    assert [r.outcome for r in run.results] == [DecisionOutcome.REVIEW] * 3
    assert run.summary()["pass_count"] == 0


def test_golden_unit_mismatch_is_review():
    # Rule authored in mm; drawing facts are normalised to m. Never silently converted.
    _space, run = _run(
        [_rule("r-mm", {"fact_path": "door.width", "operator": ">=", "value": 900, "unit": "mm"})]
    )
    (result,) = run.results
    assert result.outcome is DecisionOutcome.REVIEW
    assert "unit_problems" in result.details["objects"][0]["details"]


def test_drawing_level_fail_points_at_argmin_and_corridor():
    rule = _rule(
        "r-cor",
        {"fact_path": "drawing.corridor_min_width", "operator": ">=", "value": 1.2, "unit": "m"},
    )
    space, run = _run([rule], data=_corridor_plan())
    (result,) = run.results
    (corridor,) = space.objects["corridor"]
    assert space.facts["drawing"]["corridor_min_width"] == pytest.approx(1.1)
    assert result.outcome is DecisionOutcome.FAIL
    assert result.object_ids == (corridor.object_id,) and result.handles


def test_land_parcel_binding_and_site_area():
    rule = _rule(
        "r-site",
        {"fact_path": "land_parcel.site_area", "operator": ">=", "value": 60, "unit": "m2"},
    )
    zone = CheckRule(
        rule_version_id="r-zone",
        title="zone",
        logic_expr={
            "rule": {
                "if": {"in": [{"var": "land_parcel.zone"}, {"literal": ["제2종일반주거지역"]}]},
                "then": {"PASS": {}},
                "else": {"FAIL": {"reason": "zone"}},
            }
        },
        jurisdiction_code="KR",
        valid_from=date(2026, 1, 1),
        valid_to=None,
    )
    parcel = {
        "pnu": "1111010100100010000",
        "zone": "제2종일반주거지역",
        "districts": ["고도지구"],
        "site_area_m2": 250.5,
    }
    _space, run = _run([rule, zone], land_parcel=parcel)
    assert [r.outcome for r in run.results] == [DecisionOutcome.PASS, DecisionOutcome.PASS]
    assert run.facts["land_parcel"]["site_area"] == 250.5
    with pytest.raises(CadCheckInputError):
        land_parcel_facts({"site_area": {"value": 3, "unit": "m"}})
    with pytest.raises(CadCheckInputError):
        _run([rule], extra_facts={"drawing": {"door_count": 9}})


def test_binding_only_active_effective_jurisdiction_rules():
    payload = {"fact_path": "door.width", "operator": ">=", "value": 0.8, "unit": "m"}
    rules = [
        _rule("a", payload),
        _rule("b", payload, status="draft"),
        _rule("c", payload, basis_approved=False),
        _rule("d", payload, valid_to=date(2026, 6, 30)),
        _rule("e", payload, jurisdiction_code="JP"),
    ]
    bound, skipped = select_rules(rules, jurisdiction="KR-11", reference_date=REF)
    assert [r.rule_version_id for r in bound] == ["a"]
    assert [s["rule_version_id"] for s in skipped] == ["b", "c", "d", "e"]
    assert jurisdiction_covers("KR", "KR-11") and not jurisdiction_covers("KR-11", "KR")


def test_unexecutable_rule_is_review_not_error():
    bad = CheckRule(
        "x",
        "bad",
        {"rule": {"if": {"xor": [1, 2]}, "then": {"PASS": {}}}},
        "KR",
        date(2026, 1, 1),
        None,
    )
    _space, run = _run([bad])
    assert run.results[0].outcome is DecisionOutcome.REVIEW
