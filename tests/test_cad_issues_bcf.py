"""MVP-5 issue derivation and BCF 3.0 zip/XML structure (pure, no database)."""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET  # noqa: S405 - parsing our own output in tests
import zipfile

from archontos.cad.bcf import export_bcf, topic_guid, viewpoint_guid
from archontos.cad.issues import derive_issues, derive_status, geometry_bbox, issue_id_for

RUN = {"id": "run-1", "revision_id": "rev-1", "reference_date": "2026-11-01"}
RV_FAIL = "11111111-1111-1111-1111-111111111111"
RV_REVIEW = "22222222-2222-2222-2222-222222222222"
OBJ_A = "aaaaaaaa-0000-0000-0000-000000000001"
OBJ_B = "aaaaaaaa-0000-0000-0000-000000000002"


def _result(rv: str, outcome: str, objects: list[str], handles: list[str]) -> dict:
    return {
        "id": f"res-{rv}",
        "rule_version_id": rv,
        "rule_title": "복도 폭",
        "outcome": outcome,
        "reason": "corridor.width < 1.0 m",
        "jurisdiction_code": "KR",
        "effective": {"valid_from": "2020-01-01", "valid_to": None},
        "binding": True,
        "evidence_locator": [{"locator": {"article": "제10조"}}],
        "fact_paths": ["corridor.width"],
        "arch_object_ids": objects,
        "entity_handles": handles,
    }


OBJECTS = {
    OBJ_A: {
        "id": OBJ_A,
        "kind": "space",
        "properties": {"name": "복도"},
        "geometry": {"type": "polygon", "coordinates": [[0, 0], [4, 0], [4, 1], [0, 1]]},
        "source_handles": ["2A"],
    },
    OBJ_B: {
        "id": OBJ_B,
        "kind": "door",
        "properties": {},
        "geometry": {"type": "point", "coordinates": [1.5, 0.5]},
        "source_handles": ["2B"],
    },
}
REFS = {OBJ_B: [("ifc_guid", "2O2Fr$t4X7Zf8NOew3FLOH")]}

RESULTS = [
    _result(RV_FAIL, "FAIL", [OBJ_A, OBJ_B], ["2A", "2B"]),
    _result(RV_REVIEW, "REVIEW", [], ["9Z"]),
    {**_result("33333333-3333-3333-3333-333333333333", "PASS", [OBJ_A], []), "outcome": "PASS"},
]


def test_one_issue_per_rule_and_object_deterministic():
    issues = derive_issues(RUN, RESULTS, OBJECTS, REFS)
    assert len(issues) == 3
    assert [i["id"] for i in issues] == sorted(i["id"] for i in issues)
    assert issues == derive_issues(RUN, list(reversed(RESULTS)), OBJECTS, REFS)
    by_obj = {i["arch_object_id"]: i for i in issues}
    a = by_obj[OBJ_A]
    assert a["id"] == issue_id_for("run-1", RV_FAIL, OBJ_A)
    assert a["entity_handles"] == ["2A"] and a["bbox"] == [0.0, 0.0, 4.0, 1.0]
    assert a["jurisdiction_code"] == "KR" and a["valid_from"] == "2020-01-01"
    assert a["evidence_locator"] and a["status"] == "open" and a["origin"] == "check"
    assert by_obj[OBJ_B]["source_refs"] == [{"kind": "ifc_guid", "value": "2O2Fr$t4X7Zf8NOew3FLOH"}]
    drawing = by_obj[None]
    assert drawing["label"] == "drawing" and drawing["entity_handles"] == ["9Z"]
    assert drawing["outcome"] == "REVIEW" and drawing["bbox"] is None


def test_status_from_newer_revisions_no_silent_carry_over():
    assert derive_status("run-1", RV_FAIL, []) == ("open", None)
    stale = [{"id": "d1", "transitions": []}]
    assert derive_status("run-1", RV_FAIL, stale) == ("stale", "d1")
    still_failing = [
        {
            "id": "d2",
            "transitions": [
                {"base_run_id": "run-1", "rule_version_id": RV_FAIL, "target_outcome": "FAIL"}
            ],
        }
    ]
    assert derive_status("run-1", RV_FAIL, still_failing) == ("stale", "d2")
    fixed = [
        *still_failing,
        {
            "id": "d3",
            "transitions": [
                {"base_run_id": "run-1", "rule_version_id": RV_FAIL, "target_outcome": "PASS"}
            ],
        },
    ]
    assert derive_status("run-1", RV_FAIL, fixed) == ("resolved", "d3")
    # a transition of another run does not resolve this one
    other = [{"id": "d4", "transitions": [{**fixed[1]["transitions"][0], "base_run_id": "x"}]}]
    assert derive_status("run-1", RV_FAIL, other) == ("stale", "d4")


def test_regression_origin_from_target_diff():
    target = [
        {
            "id": "diff-9",
            "transitions": [
                {
                    "target_run_id": "run-1",
                    "rule_version_id": RV_FAIL,
                    "regression": True,
                    "transition": "PASS->FAIL",
                }
            ],
        }
    ]
    issues = derive_issues(RUN, RESULTS, OBJECTS, REFS, target_diffs=target)
    fail = [i for i in issues if i["rule_version_id"] == RV_FAIL]
    assert all(i["origin"] == "regression" and i["diff_id"] == "diff-9" for i in fail)
    assert all(i["origin"] == "check" for i in issues if i["rule_version_id"] == RV_REVIEW)


def test_geometry_bbox_shapes():
    assert geometry_bbox({"type": "point", "coordinates": [2, 3]}) == [2.0, 3.0, 2.0, 3.0]
    assert geometry_bbox({"type": "none"}) is None
    assert geometry_bbox(None) is None


def _issues_for_export():
    out = []
    for i in derive_issues(RUN, RESULTS, OBJECTS, REFS):
        out.append({**i, "id": i["id"], "project_id": "P-5"})
    return out


def test_bcf_zip_structure_and_xml():
    issues = _issues_for_export()
    data = export_bcf(issues, project_id="P-5")
    assert data == export_bcf(list(reversed(issues)), project_id="P-5")  # deterministic
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = set(zf.namelist())
    assert {"bcf.version", "project.bcfp", "extensions.xml"} <= names
    version = ET.fromstring(zf.read("bcf.version"))  # noqa: S314
    assert version.tag == "Version" and version.get("VersionId") == "3.0"
    project = ET.fromstring(zf.read("project.bcfp"))  # noqa: S314
    assert project.find("Project").get("ProjectId") == "P-5"

    by_obj = {i["arch_object_id"]: i for i in issues}
    for issue in issues:
        folder = topic_guid(issue["id"])
        markup = ET.fromstring(zf.read(f"{folder}/markup.bcf"))  # noqa: S314
        topic = markup.find("Topic")
        assert topic.get("Guid") == folder and topic.get("TopicStatus") == "Open"
        tags = [c.tag for c in topic]
        assert tags.index("ReferenceLinks") < tags.index("Title") < tags.index("Labels")
        assert tags.index("Labels") < tags.index("CreationDate") < tags.index("Description")
        links = [e.text for e in topic.find("ReferenceLinks")]
        assert f"archontos://cad/issues/{issue['id']}" in links
        labels = [e.text for e in topic.find("Labels")]
        assert "jurisdiction:KR" in labels
        for h in issue["entity_handles"]:
            assert f"dxf_handle:{h}" in labels

    # IFC GlobalId -> component selection
    door = by_obj[OBJ_B]
    vp = ET.fromstring(  # noqa: S314
        zf.read(f"{topic_guid(door['id'])}/viewpoint.bcfv")
    )
    assert vp.get("Guid") == viewpoint_guid(door["id"])
    comp = vp.find("Components/Selection/Component")
    assert comp.get("IfcGuid") == "2O2Fr$t4X7Zf8NOew3FLOH"
    assert vp.find("Components/Visibility").get("DefaultVisibility") == "true"

    # no IFC guid -> bbox camera framing the object
    space = by_obj[OBJ_A]
    vp = ET.fromstring(  # noqa: S314
        zf.read(f"{topic_guid(space['id'])}/viewpoint.bcfv")
    )
    assert vp.find("Components") is None
    cam = vp.find("OrthogonalCamera")
    assert float(cam.find("CameraViewPoint/X").text) == 2.0
    assert float(cam.find("CameraViewPoint/Y").text) == 0.5
    assert float(cam.find("CameraDirection/Z").text) == -1.0
    assert float(cam.find("ViewToWorldScale").text) == 6.0

    # drawing-level issue: no geometry -> no viewpoint
    drawing = by_obj[None]
    assert f"{topic_guid(drawing['id'])}/viewpoint.bcfv" not in names
    markup = ET.fromstring(  # noqa: S314
        zf.read(f"{topic_guid(drawing['id'])}/markup.bcf")
    )
    assert markup.find("Topic/Viewpoints") is None
    assert markup.find("Topic").get("TopicType") == "Warning"


def _lineage_diff(did: str, change: dict | None, result: dict | None, rv: str = RV_FAIL) -> dict:
    return {
        "id": did,
        "transitions": [],
        "changes": {OBJ_A: change} if change else {},
        "target_results": {rv: result} if result else {},
    }


def test_resolved_without_rerun_by_lineage():
    moved = {"change_type": "modified", "target_object_ids": ["t-1"]}
    # the object was removed: resolved even though the target has no run for the rule
    removed = _lineage_diff("d1", {"change_type": "removed", "target_object_ids": []}, None)
    assert derive_status("run-1", RV_FAIL, [removed], OBJ_A) == ("resolved", "d1")
    # the rule now PASSes on the target revision run
    passes = _lineage_diff("d2", moved, {"outcome": "PASS", "arch_object_ids": []})
    assert derive_status("run-1", RV_FAIL, [passes], OBJ_A) == ("resolved", "d2")
    # still failing, but only on another object: this object passes now
    elsewhere = _lineage_diff("d3", moved, {"outcome": "FAIL", "arch_object_ids": ["t-9"]})
    assert derive_status("run-1", RV_FAIL, [elsewhere], OBJ_A) == ("resolved", "d3")
    # still failing on the successor of this object -> stale
    same = _lineage_diff("d4", moved, {"outcome": "FAIL", "arch_object_ids": ["t-1", "t-9"]})
    assert derive_status("run-1", RV_FAIL, [same], OBJ_A) == ("stale", "d4")
    # offenders unknown (drawing-level FAIL) -> cannot conclude -> stale
    unknown = _lineage_diff("d5", moved, {"outcome": "FAIL", "arch_object_ids": []})
    assert derive_status("run-1", RV_FAIL, [unknown], OBJ_A) == ("stale", "d5")
    # no run for the rule on the target revision -> stale; wrong rule does not count
    no_run = _lineage_diff("d6", moved, None)
    assert derive_status("run-1", RV_FAIL, [no_run], OBJ_A) == ("stale", "d6")
    other_rule = _lineage_diff("d7", moved, {"outcome": "PASS", "arch_object_ids": []}, RV_REVIEW)
    assert derive_status("run-1", RV_FAIL, [other_rule], OBJ_A) == ("stale", "d7")
    # a drawing-level issue (no object) resolves only when the rule PASSes
    assert derive_status("run-1", RV_FAIL, [elsewhere]) == ("stale", "d3")
    assert derive_status("run-1", RV_FAIL, [passes]) == ("resolved", "d2")
    # the first resolving diff (by id) is the basis; order of input does not matter
    assert derive_status("run-1", RV_FAIL, [passes, same, removed], OBJ_A) == ("resolved", "d1")


def test_derive_issues_uses_object_lineage_per_issue():
    diff = {
        "id": "dx",
        "transitions": [],
        "changes": {
            OBJ_A: {"change_type": "modified", "target_object_ids": ["t-a"]},
            OBJ_B: {"change_type": "modified", "target_object_ids": ["t-b"]},
        },
        "target_results": {RV_FAIL: {"outcome": "FAIL", "arch_object_ids": ["t-b"]}},
    }
    issues = derive_issues(RUN, RESULTS, OBJECTS, REFS, superseding_diffs=[diff])
    status = {(i["rule_version_id"], i["arch_object_id"]): i["status"] for i in issues}
    assert status[(RV_FAIL, OBJ_A)] == "resolved"
    assert status[(RV_FAIL, OBJ_B)] == "stale"
    assert status[(RV_REVIEW, None)] == "stale"
