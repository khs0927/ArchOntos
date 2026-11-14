"""MVP-3 golden tests: programmatic DXF pairs -> object diff, deltas, tolerances, impact."""

from __future__ import annotations

from typing import Any

import pytest

from archontos.cad.diff import (
    DiffInputError,
    DiffTolerances,
    attach_impact,
    change_id,
    diff_objects,
    summarize,
    transitions,
)
from archontos.cad.dxf import CAD_NAMESPACE, parse_dxf  # noqa: E402  (after importorskip)
from tests.cad_fixtures import revision_plan


def _objects(data: bytes) -> list[dict[str, Any]]:
    return [
        {
            "id": o.id,
            "kind": o.kind,
            "layer": o.layer,
            "properties": o.properties,
            "fact_units": o.fact_units,
            "geometry": o.geometry,
            "source_handles": list(o.source_handles),
            "object_hash": o.object_hash,
        }
        for o in parse_dxf(data).objects
    ]


def _diff(base: bytes, target: bytes, **tol: Any) -> list[dict[str, Any]]:
    return diff_objects(_objects(base), _objects(target), DiffTolerances.from_mapping(tol))


def _by_type(changes: list[dict[str, Any]], change_type: str, kind: str | None = None):
    return [
        c
        for c in changes
        if c["change_type"] == change_type and (kind is None or c["kind"] == kind)
    ]


def test_unchanged_drawing_with_unmapped_difference():
    changes = _diff(revision_plan(), revision_plan(note="x"))
    assert changes and {c["change_type"] for c in changes} == {"unchanged"}
    assert all(c["match_method"] == "handle" for c in changes)


def test_moved_door_by_handle_has_geometry_delta():
    changes = _diff(revision_plan(), revision_plan(door_x=1500))
    (door,) = _by_type(changes, "modified", "door")
    (delta,) = door["deltas"]
    assert door["match_method"] == "handle"
    assert delta["property"] == "geometry.centroid"
    assert delta["delta"] == pytest.approx(1.0)
    assert delta["dx"] == pytest.approx(1.0)
    assert delta["unit"] == "m"
    assert not _by_type(changes, "added") and not _by_type(changes, "removed")


def test_moved_door_when_handles_shift_uses_geometric_matching():
    changes = _diff(revision_plan(), revision_plan(door_x=600, shift_handles=True))
    (door,) = _by_type(changes, "modified", "door")
    assert door["match_method"] == "geometry"
    assert door["deltas"][0]["delta"] == pytest.approx(0.1)
    assert not _by_type(changes, "added") and not _by_type(changes, "removed")


def test_door_moved_beyond_match_distance_is_remove_plus_add():
    changes = _diff(
        revision_plan(), revision_plan(door_x=3000, shift_handles=True), match_by_handle=False
    )
    assert len(_by_type(changes, "removed", "door")) == 1
    assert len(_by_type(changes, "added", "door")) == 1


def test_widened_corridor_numeric_deltas_carry_units():
    changes = _diff(revision_plan(), revision_plan(corridor_width=1500))
    corridor = next(c for c in _by_type(changes, "modified", "space") if c["label"] == "복도")
    by_prop = {d["property"]: d for d in corridor["deltas"]}
    assert by_prop["area"]["delta"] == pytest.approx(3.2)
    assert by_prop["area"]["unit"] == "m2"
    assert by_prop["area"]["fact_path"] == "space.area"
    assert by_prop["perimeter"]["unit"] == "m"
    assert by_prop["geometry.bbox_size"]["dy"] == pytest.approx(0.4)
    assert next(c for c in changes if c["label"] == "거실")["change_type"] == "unchanged"


def test_tolerances_suppress_small_deltas():
    narrow, slightly = revision_plan(corridor_width=1100), revision_plan(corridor_width=1105)
    assert _by_type(_diff(narrow, slightly), "modified", "space")
    loose = _diff(narrow, slightly, per_property={"area": 0.1, "perimeter": 0.1}, position=0.01)
    assert not _by_type(loose, "modified")
    relative = _diff(narrow, slightly, numeric_rel=0.05, position=0.01)
    assert not _by_type(relative, "modified")


def test_removed_wall_and_added_space():
    changes = _diff(revision_plan(), revision_plan(fourth_wall=False, extra_space=True))
    (wall,) = _by_type(changes, "removed", "wall")
    assert wall["target_object_id"] is None and wall["base_object_id"]
    (space,) = _by_type(changes, "added", "space")
    assert space["label"] == "창고" and space["base_object_id"] is None
    summary = summarize(changes, 7, 7)
    assert summary["removed"] == 1 and summary["added"] == 1
    assert summary["by_kind"]["wall"]["removed"] == 1


def test_diff_is_deterministic():
    a, b = revision_plan(), revision_plan(door_x=1500, extra_space=True)
    assert _diff(a, b) == _diff(a, b)


def test_tolerance_validation():
    for bad in ({"bogus": 1}, {"numeric_abs": -1}, {"match_min_iou": 2}, {"match_by_handle": "y"}):
        with pytest.raises(DiffInputError):
            DiffTolerances.from_mapping(bad)
    assert DiffTolerances.from_mapping(None).as_dict()["numeric_abs"] == 0.001


def _result(rid: str, rule: str, outcome: str, paths: list[str], objs: list[str]):
    return {
        "id": rid,
        "run_id": "r",
        "rule_version_id": rule,
        "rule_title": rule,
        "outcome": outcome,
        "fact_paths": paths,
        "arch_object_ids": objs,
    }


def test_impact_marks_base_results_stale_and_transitions_pass_to_fail():
    base, target = _objects(revision_plan()), _objects(revision_plan(corridor_width=900))
    changes = diff_objects(base, target)
    for c in changes:
        c["id"] = change_id("d", c, CAD_NAMESPACE)
    corridor = next(c for c in changes if c["label"] == "복도")
    door = next(c for c in changes if c["kind"] == "door")
    base_results = [
        _result("x1", "corridor-width", "PASS", ["corridor.width"], []),
        _result("x2", "door-width", "PASS", ["door.width"], [door["base_object_id"]]),
        _result("x3", "stairs", "REVIEW", ["stair.width"], []),
    ]
    attach_impact(changes, base_results, [])
    impacted = {r["rule_version_id"] for r in corridor["impact"]["base"]}
    assert impacted == {"corridor-width"}  # derived from the changed bbox
    assert all(r["stale"] for c in changes for r in c["impact"]["base"])
    assert door["change_type"] == "unchanged" and door["impact"]["base"] == []

    drawing_rule = [_result("x4", "area-total", "PASS", ["drawing.space_area_total"], [])]
    attach_impact(changes, drawing_rule, [])
    assert [r["rule_version_id"] for r in corridor["impact"]["base"]] == ["area-total"]

    new_results = [
        _result("y1", "corridor-width", "FAIL", ["corridor.width"], [corridor["target_object_id"]]),
        _result("y2", "door-width", "PASS", ["door.width"], []),
    ]
    attach_impact(changes, base_results, new_results)
    out = transitions(base_results, new_results, changes, base_run_id="b", target_run_id="t")
    by_rule = {t["rule_version_id"]: t for t in out}
    assert by_rule["corridor-width"]["transition"] == "PASS->FAIL"
    assert by_rule["corridor-width"]["regression"] and by_rule["corridor-width"]["changed"]
    assert by_rule["corridor-width"]["change_ids"] == [corridor["id"]]
    assert by_rule["door-width"]["transition"] == "PASS->PASS"
    assert not by_rule["door-width"]["changed"]
    assert by_rule["stairs"]["transition"] == "REVIEW->N/A"
    assert corridor["impact"]["target"][0]["via"] == "object"


# --- wall split / merge ----------------------------------------------------------------------


def _wall(oid: str, coords: list[list[float]], handle: str | None = None) -> dict[str, Any]:
    length = sum(
        ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5
        for a, b in zip(coords, coords[1:], strict=False)
    )
    return {
        "id": oid,
        "kind": "wall",
        "layer": "벽",
        "properties": {"length": {"value": round(length, 6), "unit": "m"}},
        "geometry": {"type": "linestring", "coordinates": coords, "closed": False},
        "source_handles": [handle or oid.upper()],
        "object_hash": oid,
    }


def test_wall_split_one_to_three_with_lineage():
    base = [_wall("b1", [[0, 0], [6, 0]], handle="A1")]
    target = [
        _wall("t1", [[0, 0], [2, 0]], handle="A1"),  # keeps the original handle
        _wall("t2", [[2, 0], [4.5, 0]]),
        _wall("t3", [[4.5, 0], [6, 0]]),
    ]
    changes = diff_objects(base, target)
    assert {c["change_type"] for c in changes} == {"split"}
    assert [c["target_object_id"] for c in changes] == ["t1", "t2", "t3"]
    assert all(c["base_object_id"] == "b1" and c["match_method"] == "collinear" for c in changes)
    lineage = changes[0]["lineage"]
    assert lineage["group_type"] == "split"
    assert lineage["target_object_ids"] == ["t1", "t2", "t3"]
    assert lineage["whole_length"] == 6.0 and lineage["covered_length"] == 6.0
    scores = [c["match_score"] for c in changes]
    assert scores == pytest.approx([2 / 6, 2.5 / 6, 1.5 / 6], abs=1e-6)
    summary = summarize(changes, 1, 3)
    assert summary["split"] == 3 and summary["removed"] == 0 and summary["added"] == 0


def test_dxf_wall_break_is_split_not_removed_plus_added():
    changes = _diff(revision_plan(), revision_plan(split_bottom_wall=True))
    split = _by_type(changes, "split", "wall")
    assert len(split) == 2 and len({c["base_object_id"] for c in split}) == 1
    assert not _by_type(changes, "removed") and not _by_type(changes, "added")
    merged = _by_type(_diff(revision_plan(split_bottom_wall=True), revision_plan()), "merged")
    assert len(merged) == 2


def test_wall_merge_n_to_one():
    base = [_wall("b1", [[0, 0], [0, 3]]), _wall("b2", [[0, 3], [0, 5]])]
    target = [_wall("t1", [[0, 5], [0, 0]])]  # reversed direction still merges
    changes = diff_objects(base, target)
    assert [(c["change_type"], c["base_object_id"]) for c in changes] == [
        ("merged", "b1"),
        ("merged", "b2"),
    ]
    assert changes[0]["lineage"]["base_object_ids"] == ["b1", "b2"]


def test_split_with_gap_needs_tolerance():
    base = [_wall("b1", [[0, 0], [6, 0]])]
    target = [_wall("t1", [[0, 0], [2, 0]]), _wall("t2", [[2.9, 0], [6, 0]])]  # 0.9 m door gap
    default = diff_objects(base, target)
    assert "split" not in {c["change_type"] for c in default}
    wide = diff_objects(base, target, DiffTolerances.from_mapping({"split_max_gap": 1.0}))
    assert {c["change_type"] for c in wide} == {"split"}
    off = diff_objects(
        base, target, DiffTolerances.from_mapping({"split_max_gap": 1.0, "detect_split": False})
    )
    assert "split" not in {c["change_type"] for c in off}
    with pytest.raises(DiffInputError):
        DiffTolerances.from_mapping({"detect_split": "yes"})


def test_non_collinear_or_partial_pieces_are_not_split():
    base = [_wall("b1", [[0, 0], [6, 0]])]
    offset = [_wall("t1", [[0, 0], [3, 0]]), _wall("t2", [[3, 0.2], [6, 0.2]])]
    assert "split" not in {c["change_type"] for c in diff_objects(base, offset)}
    short = [_wall("t1", [[0, 0], [2, 0]]), _wall("t2", [[2, 0], [4, 0]])]  # 4 of 6 m
    assert "split" not in {c["change_type"] for c in diff_objects(base, short)}
    single = [_wall("t1", [[0, 0], [5.99, 0]])]
    assert "split" not in {c["change_type"] for c in diff_objects(base, single)}


def test_split_impact_and_ids_are_deterministic():
    base = [_wall("b1", [[0, 0], [6, 0]])]
    target = [_wall("t2", [[3, 0], [6, 0]]), _wall("t1", [[0, 0], [3, 0]])]
    a = diff_objects(base, target)
    b = diff_objects(list(reversed(base)), list(reversed(target)))
    assert a == b
    assert len({change_id("d", c, CAD_NAMESPACE) for c in a}) == 2
    attach_impact(a, [_result("r1", "rv1", "FAIL", ["drawing.wall_length_total"], [])], [])
    assert all(c["impact"]["base"] for c in a)
