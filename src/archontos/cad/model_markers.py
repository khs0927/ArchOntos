"""Issue markers for IFC and SketchUp revisions (P2).

The marker plan (:func:`archontos.cad.markers.build_marker_plan`) is built in **model
coordinates** (metres, the normalised x/y of ``arch_object.geometry``; ``units = "m"``). It is
exported, next to the BCF viewpoints, as

* :func:`write_ifc_markers`: a NEW, standalone IFC4 file (``archontos-ifc-markers/1``) with one
  ``IfcAnnotation`` per marker (circle and leader as ``IfcPolyline`` curves, z = 0, a
  ``Pset_ArchOntosIssue`` with issue id, outcome, label and the offending element's GlobalId)
  and one ``IfcGroup`` "ArchOntos issues" holding them. Load it as an overlay next to the
  original model (federation); the original IFC is never read or modified. Needs ifcopenshell
  (optional ``bim`` extra).
* :func:`skp_overlay`: a SketchUp JSON overlay (``archontos-skp-markers/1``) validated by
  :data:`SKP_MARKERS_SCHEMA`, in the same contract style as ``archontos-skp-export/1``; a
  SketchUp-side script draws it on the ``ARCHONTOS-ISSUE`` tag.

Both are deterministic: GlobalIds are uuid5 values of the plan hash and issue id, the IFC header
timestamp is fixed, the JSON is canonical.
"""

from __future__ import annotations

import json
import math
import uuid
from typing import Any

from archontos.cad.dxf import CAD_NAMESPACE, CadDependencyError
from archontos.cad.markers import MarkerError

IFC_MARKERS_SCHEMA_ID = "archontos-ifc-markers/1"
SKP_MARKERS_SCHEMA_ID = "archontos-skp-markers/1"
#: Circle approximation for IFC (IfcPolyline vertices).
_CIRCLE_SEGMENTS = 24
#: RGB per outcome for SketchUp materials (FAIL red, REVIEW yellow; like ACI 1 / 2).
OUTCOME_RGB: dict[str, list[int]] = {"FAIL": [255, 0, 0], "REVIEW": [255, 255, 0]}
_FIXED_TIMESTAMP = "2000-01-01T00:00:00"

_PT2 = {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}
_PT3 = {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3}

#: JSON Schema of the SketchUp overlay (documented in docs/MVP4-IFC-SKETCHUP.md).
SKP_MARKERS_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://archontos.dev/schemas/archontos-skp-markers-1.json",
    "title": SKP_MARKERS_SCHEMA_ID,
    "type": "object",
    "required": ["schema", "units", "tag", "plan_sha256", "markers"],
    "properties": {
        "schema": {"const": SKP_MARKERS_SCHEMA_ID},
        "units": {"const": "m"},
        "tag": {"type": "string", "minLength": 1, "maxLength": 300},
        "plan_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "source_sha256": {"type": ["string", "null"], "pattern": "^[0-9a-f]{64}$"},
        "markers": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "issue_id",
                    "outcome",
                    "color",
                    "center",
                    "radius",
                    "leader",
                    "label",
                    "text_height",
                ],
                "properties": {
                    "issue_id": {"type": "string", "minLength": 1},
                    "outcome": {"enum": ["FAIL", "REVIEW"]},
                    "persistent_id": {"type": ["string", "null"]},
                    "color": {
                        "type": "array",
                        "items": {"type": "integer", "minimum": 0, "maximum": 255},
                        "minItems": 3,
                        "maxItems": 3,
                    },
                    "center": _PT3,
                    "radius": {"type": "number", "exclusiveMinimum": 0},
                    "leader": {
                        "type": "object",
                        "required": ["start", "end"],
                        "properties": {"start": _PT3, "end": _PT3},
                        "additionalProperties": False,
                    },
                    "label": {
                        "type": "object",
                        "required": ["position", "text"],
                        "properties": {"position": _PT3, "text": {"type": "string"}},
                        "additionalProperties": False,
                    },
                    "text_height": {"type": "number", "exclusiveMinimum": 0},
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}


def _require_model_plan(plan: dict[str, Any]) -> None:
    if plan.get("units") != "m":
        raise MarkerError("model markers need a plan in model coordinates (units 'm')")


def _xyz(point: list[float]) -> list[float]:
    return [float(point[0]), float(point[1]), 0.0]


def skp_overlay(plan: dict[str, Any], *, source_sha256: str | None = None) -> dict[str, Any]:
    """SketchUp JSON overlay for a model-coordinate marker plan (validated)."""
    _require_model_plan(plan)
    doc = {
        "schema": SKP_MARKERS_SCHEMA_ID,
        "units": "m",
        "tag": str(plan["layer"]),
        "plan_sha256": str(plan["sha256"]),
        "source_sha256": source_sha256,
        "markers": [
            {
                "issue_id": m["issue_id"],
                "outcome": m["outcome"],
                "persistent_id": m.get("entity_handle"),
                "color": list(OUTCOME_RGB[m["outcome"]]),
                "center": _xyz(m["circle"]["center"]),
                "radius": m["circle"]["radius"],
                "leader": {"start": _xyz(m["leader"]["start"]), "end": _xyz(m["leader"]["end"])},
                "label": {"position": _xyz(m["mtext"]["insert"]), "text": m["mtext"]["text"]},
                "text_height": m["mtext"]["height"],
            }
            for m in plan["markers"]
        ],
    }
    validate_skp_overlay(doc)
    return doc


def validate_skp_overlay(document: Any) -> None:
    """Raise MarkerError with the first JSON Schema violation."""
    import jsonschema

    validator = jsonschema.Draft202012Validator(SKP_MARKERS_SCHEMA)
    errors = sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))
    if errors:
        path = "/".join(str(p) for p in errors[0].absolute_path) or "(root)"
        raise MarkerError(f"invalid {SKP_MARKERS_SCHEMA_ID} at {path}: {errors[0].message}")


def skp_overlay_bytes(plan: dict[str, Any], *, source_sha256: str | None = None) -> bytes:
    doc = skp_overlay(plan, source_sha256=source_sha256)
    return json.dumps(doc, sort_keys=True, ensure_ascii=False, indent=2).encode("utf-8")


# --- IFC ----------------------------------------------------------------------------------


def _require_ifcopenshell() -> Any:
    try:
        import ifcopenshell
        import ifcopenshell.guid
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise CadDependencyError(
            "IFC marker export needs the optional 'bim' extra: pip install -e '.[bim]'"
        ) from exc
    return ifcopenshell


def _gid(ifcopenshell: Any, plan_sha: str, *parts: str) -> str:
    value = uuid.uuid5(CAD_NAMESPACE, "ifc-marker\n" + plan_sha + "\n" + "\n".join(parts))
    return str(ifcopenshell.guid.compress(value.hex))


def _circle(center: list[float], radius: float) -> list[tuple[float, float]]:
    cx, cy = float(center[0]), float(center[1])
    pts = [
        (
            round(cx + radius * math.cos(2 * math.pi * i / _CIRCLE_SEGMENTS), 6),
            round(cy + radius * math.sin(2 * math.pi * i / _CIRCLE_SEGMENTS), 6),
        )
        for i in range(_CIRCLE_SEGMENTS)
    ]
    return [*pts, pts[0]]


def write_ifc_markers(
    plan: dict[str, Any], *, source_sha256: str | None = None, project_name: str = "ArchOntos"
) -> bytes:
    """NEW standalone IFC4 file with the plan's markers as IfcAnnotation in one IfcGroup."""
    _require_model_plan(plan)
    ifcopenshell = _require_ifcopenshell()
    sha = str(plan["sha256"])
    f = ifcopenshell.file(schema="IFC4")
    f.header.file_name.name = "archontos-markers.ifc"
    f.header.file_name.time_stamp = _FIXED_TIMESTAMP
    f.header.file_description.description = (f"ViewDefinition [{IFC_MARKERS_SCHEMA_ID}]",)

    metre = f.create_entity("IfcSIUnit", UnitType="LENGTHUNIT", Name="METRE")
    units = f.create_entity("IfcUnitAssignment", Units=[metre])
    origin = f.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0))
    world = f.create_entity("IfcAxis2Placement3D", Location=origin)
    context = f.create_entity(
        "IfcGeometricRepresentationContext",
        ContextIdentifier="Annotation",
        ContextType="Plan",
        CoordinateSpaceDimension=3,
        Precision=1e-5,
        WorldCoordinateSystem=world,
    )
    project = f.create_entity(
        "IfcProject",
        GlobalId=_gid(ifcopenshell, sha, "project"),
        Name=project_name,
        Description=(
            f"{IFC_MARKERS_SCHEMA_ID}; plan {sha}"
            + (f"; source {source_sha256}" if source_sha256 else "")
        ),
        RepresentationContexts=[context],
        UnitsInContext=units,
    )
    group = f.create_entity(
        "IfcGroup",
        GlobalId=_gid(ifcopenshell, sha, "group"),
        Name="ArchOntos issues",
        Description=str(plan["layer"]),
    )
    f.create_entity(
        "IfcRelDeclares",
        GlobalId=_gid(ifcopenshell, sha, "declares"),
        RelatingContext=project,
        RelatedDefinitions=[group],
    )
    annotations = []
    for m in plan["markers"]:
        issue_id = str(m["issue_id"])

        def _poly(points: list[tuple[float, float]]) -> Any:
            return f.create_entity(
                "IfcPolyline",
                Points=[
                    f.create_entity("IfcCartesianPoint", Coordinates=(x, y, 0.0)) for x, y in points
                ],
            )

        circle = _poly(_circle(m["circle"]["center"], float(m["circle"]["radius"])))
        leader = _poly(
            [
                (float(m["leader"]["start"][0]), float(m["leader"]["start"][1])),
                (float(m["leader"]["end"][0]), float(m["leader"]["end"][1])),
            ]
        )
        shape = f.create_entity(
            "IfcShapeRepresentation",
            ContextOfItems=context,
            RepresentationIdentifier="Annotation",
            RepresentationType="GeometricCurveSet",
            Items=[f.create_entity("IfcGeometricCurveSet", Elements=[circle, leader])],
        )
        annotation = f.create_entity(
            "IfcAnnotation",
            GlobalId=_gid(ifcopenshell, sha, "annotation", issue_id),
            Name=f"[{m['outcome']}] {issue_id}",
            Description=str(m["mtext"]["text"]),
            ObjectType="ArchOntosIssueMarker",
            ObjectPlacement=f.create_entity("IfcLocalPlacement", RelativePlacement=world),
            Representation=f.create_entity("IfcProductDefinitionShape", Representations=[shape]),
        )
        props = [
            ("IssueId", "IfcIdentifier", issue_id),
            ("Outcome", "IfcLabel", str(m["outcome"])),
            ("Label", "IfcText", str(m["mtext"]["text"])),
        ]
        if m.get("entity_handle"):
            props.append(("ElementGlobalId", "IfcIdentifier", str(m["entity_handle"])))
        pset = f.create_entity(
            "IfcPropertySet",
            GlobalId=_gid(ifcopenshell, sha, "pset", issue_id),
            Name="Pset_ArchOntosIssue",
            HasProperties=[
                f.create_entity(
                    "IfcPropertySingleValue",
                    Name=name,
                    NominalValue=f.create_entity(kind, value),
                )
                for name, kind, value in props
            ],
        )
        f.create_entity(
            "IfcRelDefinesByProperties",
            GlobalId=_gid(ifcopenshell, sha, "defines", issue_id),
            RelatedObjects=[annotation],
            RelatingPropertyDefinition=pset,
        )
        annotations.append(annotation)
    if annotations:
        f.create_entity(
            "IfcRelAssignsToGroup",
            GlobalId=_gid(ifcopenshell, sha, "assigns"),
            RelatedObjects=annotations,
            RelatingGroup=group,
        )
    return str(f.to_string()).encode("utf-8")
