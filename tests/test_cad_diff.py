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
