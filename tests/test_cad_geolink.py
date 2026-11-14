"""P2: geometry-based DXF <-> IFC matching (pure part of migration 022 links)."""

from __future__ import annotations

import pytest

from archontos.cad.geolink import match_geometric, plan_bbox, score


def _obj(oid: str, kind: str, geometry: dict) -> dict:
    return {"id": oid, "kind": kind, "geometry": geometry}


def _square(x: float, y: float, s: float) -> dict:
    return {"type": "polygon", "coordinates": [[x, y], [x + s, y], [x + s, y + s], [x, y + s]]}


def test_plan_bbox_handles_points_polygons_and_junk() -> None:
    assert plan_bbox({"type": "point", "coordinates": [1, 2]}) == (1.0, 2.0, 1.0, 2.0)
    assert plan_bbox(_square(0, 0, 2)) == (0.0, 0.0, 2.0, 2.0)
    assert plan_bbox({"type": "none"}) is None
    assert plan_bbox({"type": "point", "coordinates": ["x"]}) is None


def test_score_identical_and_far() -> None:
    assert score((0, 0, 2, 2), (0, 0, 2, 2))[0] == 1.0
    conf, iou, d = score((0, 0, 1, 1), (10, 10, 11, 11))
    assert conf == 0.0 and iou == 0.0 and d > 10


def test_one_to_one_same_kind_best_first_and_excludes() -> None:
    dxf = [
        _obj("d-door", "door", {"type": "point", "coordinates": [0.5, 0.0]}),
        _obj("d-space", "space", _square(0, 0, 6)),
        _obj("d-wall", "wall", {"type": "linestring", "coordinates": [[0, 0], [6, 0]]}),
    ]
    ifc = [
        _obj("i-door-near", "door", {"type": "point", "coordinates": [0.55, 0.02]}),
        _obj("i-door-far", "door", {"type": "point", "coordinates": [3.0, 0.0]}),
        _obj("i-space", "space", _square(0.05, 0, 6)),
        _obj("i-slab", "slab", _square(0, 0, 6)),  # IFC-only kind: never linked
        _obj("i-window", "window", {"type": "point", "coordinates": [0.5, 0.0]}),  # other kind
    ]
    matches = match_geometric(dxf, ifc)
    assert [(m.a_id, m.b_id) for m in matches] == [
        ("d-door", "i-door-near"),
        ("d-space", "i-space"),
    ]
    door = matches[0]
    assert 0.5 <= door.confidence <= 1.0 and door.centroid_distance_m < 0.1
    assert matches[1].confidence > 0.9
    # objects that already have a manual / shared_ref link are never auto-linked
    assert [m.a_id for m in match_geometric(dxf, ifc, exclude_ids={"i-door-near"})] == ["d-space"]
    with pytest.raises(ValueError):
        match_geometric(dxf, ifc, tolerance_m=0)


def test_real_dxf_and_ifc_fixtures_link_door_by_geometry() -> None:
    pytest.importorskip("ifcopenshell")
    from archontos.cad.dxf import parse_dxf
    from archontos.cad.ifc import parse_ifc
    from tests.cad_fixtures import korean_plan
    from tests.model_fixtures import tiny_ifc

    def rows(drawing):
        return [{"id": o.id, "kind": o.kind, "geometry": o.geometry} for o in drawing.objects]

    dxf, ifc = parse_dxf(korean_plan()), parse_ifc(tiny_ifc())  # mm drawing vs mm IFC -> metres
    by_id = {o.id: o for o in (*dxf.objects, *ifc.objects)}
    pairs = {
        (by_id[m.a_id].kind, by_id[m.b_id].kind) for m in match_geometric(rows(dxf), rows(ifc))
    }
    assert ("door", "door") in pairs
    assert ("space", "space") in pairs
