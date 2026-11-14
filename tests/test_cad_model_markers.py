"""P2: issue markers for IFC (annotation file) and SketchUp (JSON overlay) revisions."""

from __future__ import annotations

import json

import pytest

from archontos.cad.markers import IssueMarker, MarkerError, build_marker_plan
from archontos.cad.model_markers import (
    SKP_MARKERS_SCHEMA_ID,
    skp_overlay,
    skp_overlay_bytes,
    validate_skp_overlay,
    write_ifc_markers,
)
from archontos.cad.outputs import MARKER_FORMATS

GUID = "2O2Fr$t4X7Zf8NOew3FLOH"


def _plan(units: str = "m") -> dict:
    return build_marker_plan(
        [
            IssueMarker(
                issue_id="b-issue",
                outcome="REVIEW",
                rule_label="복도 폭",
                text="폭 미확인",
                units=units,
                point=(3.0, 4.0),
            ),
            IssueMarker(
                issue_id="a-issue",
                outcome="FAIL",
                rule_label="출입문 유효폭",
                text="0.8 m < 0.9 m",
                units=units,
                entity_handle=GUID,
                bbox=(1.0, 0.0, 1.9, 0.2),
            ),
        ]
    )


def test_marker_formats_cover_all_source_formats() -> None:
    assert set(MARKER_FORMATS) == {"dxf", "ifc", "skp_json"}


def test_skp_overlay_shape_and_determinism() -> None:
    plan = _plan()
    doc = skp_overlay(plan, source_sha256="a" * 64)
    assert doc["schema"] == SKP_MARKERS_SCHEMA_ID
    assert doc["units"] == "m"
    assert doc["tag"] == "ARCHONTOS-ISSUE"
    assert [m["issue_id"] for m in doc["markers"]] == ["a-issue", "b-issue"]
    first = doc["markers"][0]
    assert first["persistent_id"] == GUID
    assert first["color"] == [255, 0, 0]
    assert first["center"][2] == 0.0
    assert first["label"]["text"].startswith("[FAIL] 출입문 유효폭")
    assert skp_overlay_bytes(plan) == skp_overlay_bytes(plan)
    json.loads(skp_overlay_bytes(plan))


def test_skp_overlay_refuses_drawing_unit_plans_and_bad_documents() -> None:
    with pytest.raises(MarkerError):
        skp_overlay(_plan("mm"))
    doc = skp_overlay(_plan())
    doc["markers"][0]["radius"] = -1
    with pytest.raises(MarkerError, match="markers/0/radius"):
        validate_skp_overlay(doc)


def test_ifc_marker_file_is_a_new_annotation_model() -> None:
    ifcopenshell = pytest.importorskip("ifcopenshell")
    plan = _plan()
    data = write_ifc_markers(plan, source_sha256="b" * 64)
    assert data == write_ifc_markers(plan, source_sha256="b" * 64)  # deterministic
    model = ifcopenshell.file.from_string(data.decode("utf-8"))
    assert model.schema == "IFC4"
    annotations = model.by_type("IfcAnnotation")
    assert len(annotations) == 2
    groups = model.by_type("IfcGroup")
    assert [g.Name for g in groups] == ["ArchOntos issues"]
    assigned = model.by_type("IfcRelAssignsToGroup")[0]
    assert {a.GlobalId for a in assigned.RelatedObjects} == {a.GlobalId for a in annotations}
    import ifcopenshell.util.element as util

    psets = {util.get_psets(a)["Pset_ArchOntosIssue"]["IssueId"]: a for a in annotations}
    fail = util.get_psets(psets["a-issue"])["Pset_ArchOntosIssue"]
    assert fail["Outcome"] == "FAIL"
    assert fail["ElementGlobalId"] == GUID
    assert "ElementGlobalId" not in util.get_psets(psets["b-issue"])["Pset_ArchOntosIssue"]
    curves = psets["a-issue"].Representation.Representations[0].Items[0].Elements
    assert len(curves) == 2  # circle + leader
    with pytest.raises(MarkerError):
        write_ifc_markers(_plan("mm"))
