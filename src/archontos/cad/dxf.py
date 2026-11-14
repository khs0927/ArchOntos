"""Deterministic DXF parser: bytes -> raw entities -> architectural objects.

Pure and file-based (no network, no database). The same bytes parsed with the same
``parser_revision`` (parser version + mapping rule-set version) always produce the same entity
and object ids and hashes, so persistence can be idempotent like evidence identity.

Requires the optional ``cad`` extra (``pip install -e '.[cad]'``, ezdxf).
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from archontos.cad.rules import DEFAULT_RULE_SET, MappingRule, MappingRuleSet

PARSER_VERSION = "dxf-1.1"  # 1.1: INSERT attributes, width_source on widths
#: Namespace for every CAD uuid5. Never change: ids are part of the persisted identity.
CAD_NAMESPACE = uuid.UUID("6f1d3c2a-7b4e-5a90-9c1d-2e3f4a5b6c7d")

#: $INSUNITS code -> (unit name, metres per drawing unit).
INSUNITS: dict[int, tuple[str, float]] = {
    1: ("in", 0.0254),
    2: ("ft", 0.3048),
    4: ("mm", 0.001),
    5: ("cm", 0.01),
    6: ("m", 1.0),
}
UNIT_FACTORS = {name: factor for name, factor in INSUNITS.values()}
_ENTITY_TYPES = (
    "LINE",
    "LWPOLYLINE",
    "POLYLINE",
    "CIRCLE",
    "ARC",
    "INSERT",
    "HATCH",
    "TEXT",
    "MTEXT",
)


#: INSERT attribute tags that carry an effective (clear / leaf / opening) width, best first.
WIDTH_ATTRIBUTE_TAGS = (
    "CLEAR_WIDTH",
    "유효폭",
    "OPENING_WIDTH",
    "LEAF_WIDTH",
    "DOOR_WIDTH",
    "WIDTH",
    "문폭",
    "폭",
    "W",
)
_WIDTH_TEXT = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(mm|cm|m)?\s*$", re.IGNORECASE)
_TEXT_UNITS = {"mm": 0.001, "cm": 0.01, "m": 1.0}


def attribute_width(attributes: Any, factor: float) -> tuple[float, str] | None:
    """(width in m, tag) from block attributes; a bare number is in drawing units."""
    if not isinstance(attributes, dict):
        return None
    for tag in WIDTH_ATTRIBUTE_TAGS:
        text = attributes.get(tag)
        if not isinstance(text, str):
            continue
        match = _WIDTH_TEXT.match(text)
        if match is None:
            continue
        value = float(match.group(1))
        unit = (match.group(2) or "").lower()
        metres = value * (_TEXT_UNITS[unit] if unit else factor)
        if metres > 0 and math.isfinite(metres):
            return metres, tag
    return None


class CadParseError(ValueError):
    """The bytes are not a readable DXF, or declare units the parser cannot interpret."""


class CadDependencyError(RuntimeError):
    """ezdxf (the ``cad`` extra) is not installed."""


def sha256_hex(data: bytes | str) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def parser_revision(rule_set: MappingRuleSet = DEFAULT_RULE_SET) -> str:
    return f"{PARSER_VERSION}+rules-{rule_set.version}"


def source_id_for(project_id: str, source_key: str) -> str:
    """64-hex stable drawing identity (``archontos-aec-subject-ref/1`` ``source_id``)."""
    return sha256_hex(f"cad-source\n{project_id}\n{source_key}")


def _num(value: float) -> float:
    rounded = round(float(value), 6)
    return 0.0 if rounded == 0 else rounded  # no "-0.0" in hashes


def _pt(point: Any) -> list[float]:
    return [_num(point[0]), _num(point[1])]


@dataclass(frozen=True, slots=True)
class CadEntity:
    id: str
    handle: str
    layer: str
    entity_type: str
    block_name: str | None
    geometry: dict[str, Any]
    bbox: tuple[float, float, float, float] | None
    entity_hash: str


@dataclass(frozen=True, slots=True)
class ArchObject:
    id: str
    kind: str
    rule_id: str
    layer: str
    properties: dict[str, Any]
    fact_units: dict[str, str]
    geometry: dict[str, Any]
    source_handles: tuple[str, ...]
    object_hash: str
    #: Cross-source identity refs ``(ref_kind, value)``, e.g. ``("ifc_guid", GlobalId)``.
    #: Empty for DXF objects (persistence records the geometry handle as ``dxf_handle``).
    source_refs: tuple[tuple[str, str], ...] = ()

    def facts(self) -> dict[str, Any]:
        """Numeric properties as rule-engine facts: ``{kind: {name: value}}``."""
        values = {
            name: prop["value"]
            for name, prop in self.properties.items()
            if isinstance(prop, dict) and "unit" in prop
        }
        return {self.kind: values}


@dataclass(frozen=True, slots=True)
class ParsedDrawing:
    sha256: str
    byte_size: int
    parser_revision: str
    rule_set: Any  # MappingRuleSet (DXF) or model.ModelMappingRuleSet (IFC / SketchUp JSON)
    units: str
    units_assumed: bool
    dxf_version: str
    entities: tuple[CadEntity, ...]
    objects: tuple[ArchObject, ...]
    unmapped_entity_count: int
    content_digest: str = field(default="")
    #: ``dxf`` | ``ifc`` | ``skp_json`` (migration 018 ``cad_revision.source_format``).
    source_format: str = "dxf"
    #: Model-level metadata (IFC schema, storeys, units); empty for DXF.
    model_meta: dict[str, Any] = field(default_factory=dict)


def _require_ezdxf() -> Any:
    try:
        import ezdxf
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise CadDependencyError(
            "DXF parsing needs the optional 'cad' extra: pip install -e '.[cad]'"
        ) from exc
    return ezdxf


def _read_document(data: bytes) -> Any:
    _require_ezdxf()
    from ezdxf import recover
    from ezdxf.lldxf.const import DXFStructureError

    if not data:
        raise CadParseError("empty DXF upload")
    try:
        doc, _auditor = recover.read(io.BytesIO(data))
    except (DXFStructureError, UnicodeDecodeError, ValueError, OSError) as exc:
        raise CadParseError(f"not a readable DXF: {exc}") from exc
    return doc


def _points_bbox(points: list[list[float]]) -> tuple[float, float, float, float] | None:
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (_num(min(xs)), _num(min(ys)), _num(max(xs)), _num(max(ys)))


def _hatch_outline(entity: Any) -> list[list[float]]:
    from ezdxf.entities.boundary_paths import EdgePath, PolylinePath

    for path in entity.paths:
        if isinstance(path, PolylinePath):
            return [_pt(v) for v in path.vertices]
        if isinstance(path, EdgePath):
            points = []
            for edge in path.edges:
                start = getattr(edge, "start", None)
                if start is not None:
                    points.append(_pt(start))
            if points:
                return points
    return []


def _geometry(entity: Any, doc: Any) -> tuple[dict[str, Any], Any]:
    """Return (geometry JSON, bbox) for one modelspace entity, in drawing units."""
    kind = entity.dxftype()
    dxf = entity.dxf
    if kind == "LINE":
        pts = [_pt(dxf.start), _pt(dxf.end)]
        return {"type": "line", "points": pts}, _points_bbox(pts)
    if kind == "LWPOLYLINE":
        pts = [_pt(p) for p in entity.get_points("xy")]
        bulged = any(abs(b) > 1e-12 for *_xy, b in entity.get_points("xyb"))
        geom: dict[str, Any] = {"type": "polyline", "points": pts, "closed": bool(entity.closed)}
        if bulged:
            geom["has_bulges"] = True
        return geom, _points_bbox(pts)
    if kind == "POLYLINE":
        pts = [_pt(v.dxf.location) for v in entity.vertices]
        return {"type": "polyline", "points": pts, "closed": bool(entity.is_closed)}, (
            _points_bbox(pts)
        )
    if kind in ("CIRCLE", "ARC"):
        c, r = _pt(dxf.center), _num(dxf.radius)
        geom = {"type": kind.lower(), "center": c, "radius": r}
        if kind == "ARC":
            geom["start_angle"] = _num(dxf.start_angle)
            geom["end_angle"] = _num(dxf.end_angle)
        return geom, (_num(c[0] - r), _num(c[1] - r), _num(c[0] + r), _num(c[1] + r))
    if kind == "INSERT":
        geom = {
            "type": "insert",
            "insert": _pt(dxf.insert),
            "rotation": _num(dxf.rotation),
            "scale": [_num(dxf.xscale), _num(dxf.yscale)],
        }
        attribs = {
            str(a.dxf.tag).strip().upper(): str(a.dxf.text)
            for a in getattr(entity, "attribs", ()) or ()
            if str(a.dxf.tag).strip()
        }
        if attribs:
            geom["attributes"] = dict(sorted(attribs.items()))
        block = doc.blocks.get(dxf.name)
        if block is not None:
            from ezdxf import bbox as ezbbox

            local = ezbbox.extents(block, fast=True)
            if local.has_data:
                geom["block_size"] = [_num(local.size.x), _num(local.size.y)]
        world = None
        try:
            from ezdxf import bbox as ezbbox

            ext = ezbbox.extents([entity], fast=True)
            if ext.has_data:
                world = (
                    _num(ext.extmin.x),
                    _num(ext.extmin.y),
                    _num(ext.extmax.x),
                    _num(ext.extmax.y),
                )
        except (ValueError, ArithmeticError):  # degenerate scale or empty block
            world = None
        return geom, world
    if kind == "HATCH":
        pts = _hatch_outline(entity)
        return {"type": "hatch", "points": pts, "closed": True}, _points_bbox(pts)
    if kind == "TEXT":
        p = _pt(dxf.insert)
        return {"type": "text", "insert": p, "text": dxf.text}, (p[0], p[1], p[0], p[1])
    if kind == "MTEXT":
        p = _pt(dxf.insert)
        return {"type": "text", "insert": p, "text": entity.plain_text()}, (p[0], p[1], p[0], p[1])
    return {"type": kind.lower()}, None


def _shoelace(points: list[list[float]]) -> float:
    total = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1], strict=True):
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def _path_length(points: list[list[float]], closed: bool) -> float:
    seq = points + points[:1] if closed and len(points) > 2 else points
    return sum(math.dist(a, b) for a, b in zip(seq, seq[1:], strict=False))


def _contains(polygon: list[list[float]], point: list[float]) -> bool:
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in zip(polygon, polygon[1:] + polygon[:1], strict=True):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _quantity(value: float, unit: str) -> dict[str, Any]:
    return {"value": _num(value), "unit": unit}


def _object_properties(
    rule: MappingRule, entity: CadEntity, factor: float
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """(properties, geometry in metres) for one mapped entity, or None if not representable."""
    geom = entity.geometry
    gtype = geom["type"]
    props: dict[str, Any] = {}
    if gtype == "insert":
        size = geom.get("block_size")
        sx, sy = (abs(v) for v in geom["scale"])
        props["rotation"] = _quantity(geom["rotation"], "deg")
        if size:
            props["width"] = _quantity(size[0] * sx * factor, "m")
            props["depth"] = _quantity(size[1] * sy * factor, "m")
            props["width_source"] = "block_bbox"
        attr = attribute_width(geom.get("attributes"), factor)
        if attr is not None:
            props["width"] = _quantity(attr[0], "m")
            props["width_source"] = f"attribute:{attr[1]}"
        out_geom = {
            "type": "point",
            "coordinates": [_num(geom["insert"][0] * factor), _num(geom["insert"][1] * factor)],
        }
        return props, out_geom
    if gtype in ("line", "polyline", "hatch"):
        pts = geom["points"]
        closed = bool(geom.get("closed"))
        scaled = [[_num(x * factor), _num(y * factor)] for x, y in pts]
        if rule.kind in ("space", "column", "stair") and closed and len(pts) >= 3:
            props["area"] = _quantity(_shoelace(pts) * factor * factor, "m2")
            props["perimeter"] = _quantity(_path_length(pts, True) * factor, "m")
            if entity.bbox and rule.kind != "space":
                props["width"] = _quantity((entity.bbox[2] - entity.bbox[0]) * factor, "m")
                props["depth"] = _quantity((entity.bbox[3] - entity.bbox[1]) * factor, "m")
                props["width_source"] = "bbox"
            if geom.get("has_bulges"):
                props["approximated"] = "bulges ignored (straight segments)"
            return props, {"type": "polygon", "coordinates": scaled}
        if rule.kind == "wall":
            props["length"] = _quantity(_path_length(pts, closed) * factor, "m")
            return props, {"type": "linestring", "coordinates": scaled, "closed": closed}
    return None


def _object_id(revision_key: str, kind: str, handles: tuple[str, ...]) -> str:
    return str(uuid.uuid5(CAD_NAMESPACE, f"obj\n{revision_key}\n{kind}\n{','.join(handles)}"))


def parse_dxf(
    data: bytes,
    *,
    rule_set: MappingRuleSet = DEFAULT_RULE_SET,
    assume_units: str = "mm",
) -> ParsedDrawing:
    """Parse DXF bytes into raw entities and mapped architectural objects.

    ``assume_units`` applies only when the drawing does not declare ``$INSUNITS`` (code 0);
    Korean architectural drawings are conventionally in millimetres.
    """
    if assume_units not in UNIT_FACTORS:
        raise CadParseError(f"assume_units must be one of {sorted(UNIT_FACTORS)}")
    digest = sha256_hex(data)
    doc = _read_document(data)
    code = int(doc.header.get("$INSUNITS", 0) or 0)
    if code == 0:
        units, factor, assumed = assume_units, UNIT_FACTORS[assume_units], True
    elif code in INSUNITS:
        units, factor = INSUNITS[code]
        assumed = False
    else:
        raise CadParseError(f"unsupported $INSUNITS code {code}")

    revision = parser_revision(rule_set)
    # Entity ids depend only on the bytes and the parser version (not the rules).
    entity_key = f"{digest}\n{PARSER_VERSION}"
    object_key = f"{digest}\n{revision}"

    raw = [e for e in doc.modelspace() if e.dxftype() in _ENTITY_TYPES]
    raw.sort(key=lambda e: int(e.dxf.handle, 16))
    entities: list[CadEntity] = []
    for item in raw:
        geometry, bbox = _geometry(item, doc)
        handle = item.dxf.handle.upper()
        block_name = item.dxf.name if item.dxftype() == "INSERT" else None
        body = {
            "handle": handle,
            "layer": item.dxf.layer,
            "type": item.dxftype(),
            "block": block_name,
            "geometry": geometry,
        }
        entities.append(
            CadEntity(
                id=str(uuid.uuid5(CAD_NAMESPACE, f"ent\n{entity_key}\n{handle}")),
                handle=handle,
                layer=item.dxf.layer,
                entity_type=item.dxftype(),
                block_name=block_name,
                geometry=geometry,
                bbox=bbox,
                entity_hash=sha256_hex(canonical_json(body)),
            )
        )

    objects: list[ArchObject] = []
    unmapped = 0
    space_polys: list[tuple[int, list[list[float]]]] = []
    pending: list[tuple[MappingRule, CadEntity, dict[str, Any], dict[str, Any]]] = []
    for ent in entities:
        if ent.entity_type in ("TEXT", "MTEXT"):
            continue
        rule = rule_set.match(
            entity_type=ent.entity_type,
            layer=ent.layer,
            block_name=ent.block_name,
            closed=bool(ent.geometry.get("closed")),
        )
        mapped = _object_properties(rule, ent, factor) if rule else None
        if rule is None or mapped is None:
            unmapped += 1
            continue
        props, geom = mapped
        if rule.kind == "space":
            space_polys.append((len(pending), ent.geometry["points"]))
        pending.append((rule, ent, props, geom))

    # Space names: TEXT/MTEXT on a label layer (or the space's own layer) inside the polygon.
    labels = [
        e
        for e in entities
        if e.entity_type in ("TEXT", "MTEXT") and str(e.geometry.get("text", "")).strip()
    ]
    for index, polygon in space_polys:
        rule, ent, props, geom = pending[index]
        names = [
            lab
            for lab in labels
            if (rule_set.is_space_label_layer(lab.layer) or lab.layer == ent.layer)
            and _contains(polygon, lab.geometry["insert"])
        ]
        handles: list[str] = [ent.handle]
        if names:
            props["name"] = str(names[0].geometry["text"]).strip()
            handles.append(names[0].handle)
        pending[index] = (rule, ent, props, geom)
        props["_handles"] = handles

    for rule, ent, props, geom in pending:
        obj_handles: tuple[str, ...] = tuple(props.pop("_handles", [ent.handle]))
        fact_units = {
            f"{rule.kind}.{name}": prop["unit"]
            for name, prop in sorted(props.items())
            if isinstance(prop, dict) and "unit" in prop
        }
        body = {
            "kind": rule.kind,
            "rule": rule.rule_id,
            "layer": ent.layer,
            "properties": props,
            "geometry": geom,
            "handles": list(obj_handles),
        }
        objects.append(
            ArchObject(
                id=_object_id(object_key, rule.kind, obj_handles),
                kind=rule.kind,
                rule_id=rule.rule_id,
                layer=ent.layer,
                properties=props,
                fact_units=fact_units,
                geometry=geom,
                source_handles=obj_handles,
                object_hash=sha256_hex(canonical_json(body)),
            )
        )

    content_digest = sha256_hex(
        canonical_json(
            {
                "sha256": digest,
                "parser_revision": revision,
                "units": units,
                "entities": [e.entity_hash for e in entities],
                "objects": [o.object_hash for o in objects],
            }
        )
    )
    return ParsedDrawing(
        sha256=digest,
        byte_size=len(data),
        parser_revision=revision,
        rule_set=rule_set,
        units=units,
        units_assumed=assumed,
        dxf_version=str(doc.dxfversion),
        entities=tuple(entities),
        objects=tuple(objects),
        unmapped_entity_count=unmapped,
        content_digest=content_digest,
    )


def subject_ref(
    obj: ArchObject, *, project_id: str, source_key: str, drawing: ParsedDrawing, revision_id: str
) -> dict[str, Any]:
    """``archontos-aec-subject-ref/1`` for one object (validated by AecSubjectRef)."""
    from archontos.domain.contracts import AecSubjectRef

    ref = AecSubjectRef(
        project_id=project_id,
        object_id=obj.id,
        source_id=source_id_for(project_id, source_key),
        source_byte_revision_id=drawing.sha256,
        parser_revision_id=sha256_hex(drawing.parser_revision),
        locator={
            "cad_revision_id": revision_id,
            "kind": obj.kind,
            "layer": obj.layer,
            "handles": list(obj.source_handles),
        },
    )
    return ref.model_dump(mode="json")
