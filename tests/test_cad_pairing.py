"""MVP-2 object-pair rules and effective widths (pure; DXF via ezdxf, IFC via ifcopenshell)."""

from __future__ import annotations

import io
import math
from dataclasses import replace
from datetime import date
from typing import Any

import pytest

from archontos.cad import geometry as geo
from archontos.cad.checks import extract_fact_space, run_checks
from archontos.cad.dxf import attribute_width, parse_dxf
from archontos.cad.pairing import pair_objects, wall_groups
from archontos.domain.enums import DecisionOutcome
from tests.cad_fixtures import aia_plan, ezdxf, korean_plan
from tests.test_cad_checks import _rule

REF = date(2026, 11, 1)


def _rows(drawing: Any) -> list[dict[str, Any]]:
    return [
        {
            "id": o.id,
            "kind": o.kind,
            "rule_id": o.rule_id,
            "layer": o.layer,
            "properties": o.properties,
            "fact_units": o.fact_units,
            "geometry": o.geometry,
            "source_handles": list(o.source_handles),
        }
        for o in drawing.objects
    ]


def _bytes(doc: Any) -> bytes:
    stream = io.StringIO()
    doc.write(stream)
    return stream.getvalue().encode("utf-8")


def _plan(*, door_at: tuple[float, float], double_wall: bool = False, attrib: str | None = None):
    """mm drawing: a 6000 x 4000 room (실), walls on 벽, one door block (with an attribute)."""
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 4
    block = doc.blocks.new(name="D900")
    block.add_line((0, 0), (900, 0))
    block.add_arc((0, 0), 900, 0, 90)
    if attrib is not None:
        block.add_attdef("WIDTH", (0, 0), dxfattribs={"height": 50})
    msp = doc.modelspace()
    corners = [(0, 0), (6000, 0), (6000, 4000), (0, 4000)]
    for a, b in zip(corners, corners[1:] + corners[:1], strict=True):
        msp.add_line(a, b, dxfattribs={"layer": "벽"})
    if double_wall:
        msp.add_line((0, -200), (6000, -200), dxfattribs={"layer": "벽"})
    ref = msp.add_blockref("D900", door_at, dxfattribs={"layer": "문"})
    if attrib is not None:
        ref.add_auto_attribs({"WIDTH": attrib})
    msp.add_lwpolyline(corners, close=True, dxfattribs={"layer": "실"})
    return _bytes(doc)


def _check(rules, rows, **kw):
    space = extract_fact_space(rows, model_meta=kw.pop("model_meta", None))
    return space, run_checks(rules, space, jurisdiction="KR", reference_date=REF, **kw)


def _door(space):
    (door,) = space.objects["door"]
    return door


# --- door <-> host wall -----------------------------------------------------------------------


def test_door_host_wall_geometric_pair():
    rows = _rows(parse_dxf(_plan(door_at=(500, 0))))
    space, run = _check(
        [
            _rule(
                "r",
                {"fact_path": "door.host_wall.length", "operator": ">=", "value": 5, "unit": "m"},
            )
        ],
        rows,
    )
    door = _door(space)
    pair = door.pairs["host_wall"]
    assert pair.status == "paired" and pair.method == "geometry"
    assert door.facts()["door"]["host_wall"]["length"] == 6.0
    assert door.fact_units()["door.host_wall.length"] == "m"
    (result,) = run.results
    assert result.outcome is DecisionOutcome.PASS
    assert result.details["objects"][0]["pairs"]["host_wall"]["status"] == "paired"


def test_double_line_wall_gives_thickness_and_one_host():
    rows = _rows(parse_dxf(_plan(door_at=(500, -100), double_wall=True)))
    walls = [r for r in rows if r["kind"] == "wall"]
    groups = wall_groups(walls)
    paired = {g.member_ids for g in groups.values() if len(g.member_ids) == 2}
    assert len(paired) == 1
    assert {g.thickness for g in groups.values() if len(g.member_ids) == 2} == {0.2}
    rule = _rule(
        "r", {"fact_path": "door.host_wall.thickness", "operator": ">=", "value": 0.15, "unit": "m"}
    )
    space, run = _check([rule], rows)
    assert _door(space).pairs["host_wall"].status == "paired"
    assert run.results[0].outcome is DecisionOutcome.PASS
    wall_facts = {w.object_id: w for w in space.objects["wall"]}
    assert sum(1 for w in wall_facts.values() if w.values.get("thickness") == 0.2) == 2
    assert all(
        w.meta["thickness"] == {"method": "parallel_faces", "approximate": False}
        for w in wall_facts.values()
        if "thickness" in w.values
    )


def test_door_at_corner_is_ambiguous_review():
    rows = _rows(parse_dxf(_plan(door_at=(0, 0))))
    rule = _rule(
        "r", {"fact_path": "door.host_wall.length", "operator": ">=", "value": 1, "unit": "m"}
    )
    space, run = _check([rule], rows)
    assert _door(space).pairs["host_wall"].status == "ambiguous"
    (result,) = run.results
    assert result.outcome is DecisionOutcome.REVIEW
    assert result.reason.startswith("Pairing ambiguous (host_wall)")
    assert result.object_ids == (_door(space).object_id,)


def test_door_far_from_walls_is_unpaired_review():
    rows = _rows(parse_dxf(_plan(door_at=(3000, 2000))))
    rule = _rule(
        "r", {"fact_path": "door.host_wall.length", "operator": ">=", "value": 1, "unit": "m"}
    )
    space, run = _check([rule], rows)
    assert _door(space).pairs["host_wall"].status == "unpaired"
    assert run.results[0].outcome is DecisionOutcome.REVIEW
    assert "unpaired" in run.results[0].reason


def test_rules_not_reading_pairs_ignore_pair_status():
    rows = _rows(parse_dxf(_plan(door_at=(3000, 2000))))
    rule = _rule("r", {"fact_path": "door.width", "operator": ">=", "value": 0.8, "unit": "m"})
    _space, run = _check([rule], rows)
    assert run.results[0].outcome is DecisionOutcome.PASS


def test_two_kinds_read_directly_stays_review_with_pair_hint():
    rule = _rule("r", {"fact_path": "door.width", "operator": ">=", "value": 0.8, "unit": "m"})
    rule = replace(rule, logic_expr={**rule.logic_expr, "x_second_kind": {"var": "wall.length"}})
    _space, run = _check([rule], _rows(parse_dxf(korean_plan())))
    (result,) = run.results
    assert result.outcome is DecisionOutcome.REVIEW
    assert "door.host_wall.thickness" in result.reason
    assert "door" in result.details["pair_paths"]


# --- space <-> doors ------------------------------------------------------------------------


def test_space_door_count():
    rows = _rows(parse_dxf(korean_plan()))
    space, run = _check(
        [
            _rule(
                "r1",
                {"fact_path": "space.door_count", "operator": ">=", "value": 1, "unit": "count"},
            ),
            _rule(
                "r2",
                {"fact_path": "space.door_count", "operator": ">=", "value": 2, "unit": "count"},
            ),
        ],
        rows,
    )
    (room,) = space.objects["space"]
    assert room.facts()["space"]["door_count"] == 1
    assert room.fact_units()["space.door_count"] == "count"
    assert [r.outcome for r in run.results] == [DecisionOutcome.PASS, DecisionOutcome.FAIL]


def test_space_door_count_ambiguous_when_a_door_has_no_location():
    rows = _rows(parse_dxf(korean_plan()))
    for r in rows:
        if r["kind"] == "door":
            r["geometry"] = {"type": "none"}
    rule = _rule(
        "r", {"fact_path": "space.door_count", "operator": ">=", "value": 1, "unit": "count"}
    )
    _space, run = _check([rule], rows)
    assert run.results[0].outcome is DecisionOutcome.REVIEW
    assert "no plan location" in run.results[0].reason


# --- stair <-> storey -----------------------------------------------------------------------


def test_dxf_stair_has_no_storey_review():
    rule = _rule(
        "r",
        {"fact_path": "stair.storey.floor_number", "operator": "<=", "value": 5, "unit": "count"},
    )
    space, run = _check([rule], _rows(parse_dxf(aia_plan())))
    (stair,) = space.objects["stair"]
    assert stair.pairs["storey"].status == "unpaired"
    assert run.results[0].outcome is DecisionOutcome.REVIEW


def test_storey_pairing_from_model_meta():
    stairs = [
        {"id": "s1", "kind": "stair", "properties": {"storey": "1F"}, "geometry": {"type": "none"}},
        {"id": "s2", "kind": "stair", "properties": {"storey": "B1"}, "geometry": {"type": "none"}},
        {"id": "s3", "kind": "stair", "properties": {"storey": "9F"}, "geometry": {"type": "none"}},
    ]
    meta = {
        "storeys": [
            {"guid": "g2", "name": "2F", "elevation_m": 3.0},
            {"guid": "g1", "name": "1F", "elevation_m": 0.0},
            {"guid": "gb", "name": "B1", "elevation_m": -3.0},
        ]
    }
    pairs, _ = pair_objects(stairs, model_meta=meta)
    first = pairs["s1"]["storey"]
    assert first.status == "paired" and first.related_ids == ("g1",)
    assert first.values == {
        "elevation": 0.0,
        "floor_number": 1.0,
        "basement_level": 0.0,
        "stair_count": 1.0,
    }
    assert pairs["s2"]["storey"].values["basement_level"] == 1.0
    assert pairs["s3"]["storey"].status == "unpaired"
    dup = {"storeys": [*meta["storeys"], {"guid": "gx", "name": "1F", "elevation_m": 0.1}]}
    assert pair_objects(stairs, model_meta=dup)[0]["s1"]["storey"].status == "ambiguous"


def test_ifc_relations_pair_door_and_stair():
    pytest.importorskip("ifcopenshell")
    from archontos.cad.ifc import parse_ifc
    from tests.model_fixtures import WALL_GUID, tiny_ifc

    drawing = parse_ifc(tiny_ifc())
    door = next(o for o in drawing.objects if o.kind == "door")
    assert door.properties["host_ref"] == WALL_GUID
    rules = [
        _rule(
            "r1",
            {"fact_path": "door.host_wall.thickness", "operator": ">=", "value": 0.15, "unit": "m"},
        ),
        _rule(
            "r2",
            {
                "fact_path": "stair.storey.floor_number",
                "operator": "==",
                "value": 1,
                "unit": "count",
            },
        ),
    ]
    space, run = _check(rules, _rows(drawing), model_meta=drawing.model_meta)
    host = _door(space).pairs["host_wall"]
    assert host.method == "ifc:IfcRelFillsElement" and host.status == "paired"
    assert _door(space).meta["width"] == {"method": "ifc_quantity", "approximate": False}
    assert [r.outcome for r in run.results] == [DecisionOutcome.PASS, DecisionOutcome.PASS]


# --- effective widths -------------------------------------------------------------------------


def _rotate(points, deg):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return [(x * c - y * s, x * s + y * c) for x, y in points]


def test_clear_width_rectangle_rotated_and_l_shape():
    rect = [(0, 0), (8, 0), (8, 1.1), (0, 1.1)]
    assert geo.clear_width(rect) == pytest.approx(1.1)
    assert geo.clear_width(_rotate(rect, 30)) == pytest.approx(1.1)
    ell = [(0, 0), (6, 0), (6, 1.2), (1.5, 1.2), (1.5, 5), (0, 5)]
    assert geo.clear_width(ell) == pytest.approx(1.2)
    assert geo.clear_width([(0, 0), (4, 0), (0, 3)]) is None


def test_space_width_uses_clear_width_and_flags_bbox_fallback():
    rotated = {
        "id": "a",
        "kind": "space",
        "properties": {"name": "복도"},
        "geometry": {
            "type": "polygon",
            "coordinates": [list(p) for p in _rotate([(0, 0), (8, 0), (8, 1.1), (0, 1.1)], 45)],
        },
    }
    triangle = {
        "id": "b",
        "kind": "space",
        "properties": {},
        "geometry": {"type": "polygon", "coordinates": [[0, 0], [4, 0], [0, 3]]},
    }
    space = extract_fact_space([rotated, triangle])
    by_id = {o.object_id: o for o in space.objects["space"]}
    assert by_id["a"].values["width"] == pytest.approx(1.1)
    assert by_id["a"].meta["width"] == {"method": "opposite_faces", "approximate": False}
    assert by_id["b"].meta["width"] == {"method": "bbox", "approximate": True}
    assert space.facts["drawing"]["corridor_min_width"] == pytest.approx(1.1)
    assert space.as_dict()["fact_meta"]["drawing.corridor_min_width"]["approximate"] is False


def test_door_width_from_block_attribute_else_bbox_flagged():
    with_attr = parse_dxf(_plan(door_at=(500, 0), attrib="850"))
    (door,) = [o for o in with_attr.objects if o.kind == "door"]
    assert door.properties["width"] == {"value": 0.85, "unit": "m"}
    assert door.properties["width_source"] == "attribute:WIDTH"
    facts = _door(extract_fact_space(_rows(with_attr)))
    assert facts.meta["width"] == {"method": "attribute:WIDTH", "approximate": False}

    plain = _door(extract_fact_space(_rows(parse_dxf(_plan(door_at=(500, 0))))))
    assert plain.values["width"] == 0.9
    assert plain.meta["width"] == {"method": "block_bbox", "approximate": True}
    rule = _rule("r", {"fact_path": "door.width", "operator": ">=", "value": 0.8, "unit": "m"})
    _space, run = _check([rule], _rows(parse_dxf(_plan(door_at=(500, 0)))))
    assert run.results[0].details["objects"][0]["fact_meta"]["width"]["approximate"] is True


@pytest.mark.parametrize(
    ("attrs", "factor", "expected"),
    [
        ({"WIDTH": "900"}, 0.001, (0.9, "WIDTH")),
        ({"W": "0.8m", "CLEAR_WIDTH": "85cm"}, 0.001, (0.85, "CLEAR_WIDTH")),
        ({"유효폭": "1200"}, 0.001, (1.2, "유효폭")),
        ({"WIDTH": "abc"}, 0.001, None),
        ({"WIDTH": "0"}, 0.001, None),
        (None, 0.001, None),
    ],
)
def test_attribute_width(attrs, factor, expected):
    got = attribute_width(attrs, factor)
    if expected is None:
        assert got is None
    else:
        assert got is not None and got[1] == expected[1]
        assert got[0] == pytest.approx(expected[0])


def test_dxf_stair_width_is_clear_width():
    space = extract_fact_space(_rows(parse_dxf(aia_plan())))
    (stair,) = space.objects["stair"]
    assert stair.values["width"] == 2.0  # 2 x 4 m polygon: short side, not the x extent
    assert stair.meta["width"]["method"] == "opposite_faces"


def test_pairing_is_deterministic():
    rows = _rows(parse_dxf(_plan(door_at=(500, -100), double_wall=True)))
    a = extract_fact_space(rows)
    b = extract_fact_space(list(reversed(rows)))
    assert a.as_dict() == b.as_dict()
    da = {o.object_id: (o.facts(), o.fact_units()) for o in a.objects["door"]}
    db = {o.object_id: (o.facts(), o.fact_units()) for o in b.objects["door"]}
    assert da == db
