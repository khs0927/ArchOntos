"""Source-agnostic model-object import (MVP-4): IFC and SketchUp JSON -> ``arch_object`` rows.

IFC (``archontos.cad.ifc``) and SketchUp JSON (``archontos.cad.skp``) readers both reduce a
model to a list of :class:`ModelElement` values whose quantities are already normalised to
metres (``m``, ``m2``, ``m3``, ``deg``). :func:`build_model_drawing` maps them with a versioned
:class:`ModelMappingRuleSet` and returns the same :class:`~archontos.cad.dxf.ParsedDrawing` the
DXF parser produces, so persistence (MVP-1), checks (MVP-2) and diffs (MVP-3) need no change.

Everything here is pure: no ifcopenshell, no database, no network.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from archontos.cad.dxf import (
    CAD_NAMESPACE,
    ArchObject,
    CadEntity,
    CadParseError,
    ParsedDrawing,
    canonical_json,
    sha256_hex,
)
from archontos.cad.rules import OBJECT_KINDS, MappingRuleError

SOURCE_FORMATS = ("dxf", "ifc", "skp_json")
#: Object kinds a model source can produce (DXF kinds plus ``slab``).
MODEL_OBJECT_KINDS = frozenset(OBJECT_KINDS | {"slab"})
#: Cross-source identity reference kinds (``arch_object_source_ref.ref_kind``).
REF_KINDS = ("dxf_handle", "ifc_guid", "skp_persistent_id")
#: Ref kinds that identify the *same physical element* across source formats.
SHARED_REF_KINDS = ("ifc_guid",)

# --- units --------------------------------------------------------------------------------

#: metres per unit, for lengths.
LENGTH_FACTORS: dict[str, float] = {
    "mm": 0.001,
    "cm": 0.01,
    "m": 1.0,
    "in": 0.0254,
    "inch": 0.0254,
    "ft": 0.3048,
    "feet": 0.3048,
}
_DIMENSION_UNIT = {1: "m", 2: "m2", 3: "m3"}


def unit_dimension(unit: str) -> tuple[float, int] | None:
    """``"mm2"`` -> (1e-6, 2): factor to the SI base unit and the dimension (1, 2 or 3)."""
    match = re.fullmatch(r"(mm|cm|m|in|inch|ft|feet)([23]?)", unit)
    if match is None:
        return None
    power = int(match.group(2) or 1)
    return LENGTH_FACTORS[match.group(1)] ** power, power


def normalize_quantity(value: Any, unit: str) -> dict[str, Any] | None:
    """A raw number in ``unit`` -> ``{"value", "unit"}`` in m / m2 / m3 / deg (None if not)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    if unit in ("deg", "rad"):
        deg = float(value) if unit == "deg" else float(value) * 57.29577951308232
        return {"value": _num(deg), "unit": "deg"}
    if unit == "count":
        return {"value": _num(value), "unit": "count"} if float(value).is_integer() else None
    dim = unit_dimension(unit)
    if dim is None:
        return None
    factor, power = dim
    return {"value": _num(float(value) * factor), "unit": _DIMENSION_UNIT[power]}


def _num(value: float) -> float:
    rounded = round(float(value), 6)
    return 0.0 if rounded == 0 else rounded


# --- elements -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelElement:
    """One model element, already normalised to metres (quantities and coordinates)."""

    ref: str  # IFC GlobalId or SketchUp persistent_id: becomes the cad_entity handle
    element_type: str  # IFC class ("IfcDoor") or SketchUp entity type ("ComponentInstance")
    layer: str  # IFC storey name or SketchUp tag
    name: str | None = None
    definition: str | None = None  # SketchUp component definition / IFC ObjectType
    classification: str | None = None  # IFC class (IFC: own class; SketchUp: IFC type attr)
    storey: str | None = None
    quantities: dict[str, dict[str, Any]] = field(default_factory=dict)  # name -> {value,unit}
    location: tuple[float, float] | None = None  # m
    footprint: tuple[tuple[float, float], ...] | None = None  # m, closed polygon
    bbox: tuple[float, float, float, float] | None = None  # m, plan extent
    refs: tuple[tuple[str, str], ...] = ()
    attributes: dict[str, Any] = field(default_factory=dict)

    def geometry_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "model_element", "element_type": self.element_type}
        for key in ("name", "definition", "classification", "storey"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        if self.location is not None:
            out["location"] = [_num(self.location[0]), _num(self.location[1])]
        if self.footprint:
            out["footprint"] = [[_num(x), _num(y)] for x, y in self.footprint]
        if self.quantities:
            out["quantities"] = self.quantities
        if self.refs:
            out["refs"] = {kind: value for kind, value in self.refs}
        if self.attributes:
            out["attributes"] = self.attributes
        return out


# --- mapping rules ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelMappingRule:
    """Matches an element by source format and IFC class / SketchUp tag / definition regex.

    Every given pattern must match. ``class_pattern`` is tested against the element's
    classification (IFC class; for SketchUp the IFC type attribute) or, failing that, its
    element type; ``definition_pattern`` against the definition name, then the element name.
    """

    rule_id: str
    kind: str
    source_formats: frozenset[str]
    class_pattern: str | None = None
    tag_pattern: str | None = None
    definition_pattern: str | None = None
    _res: tuple[re.Pattern[str] | None, ...] = field(
        default=(), init=False, compare=False, repr=False
    )

    def __post_init__(self) -> None:
        if self.kind not in MODEL_OBJECT_KINDS:
            raise MappingRuleError(f"rule {self.rule_id!r}: unknown kind {self.kind!r}")
        if not self.source_formats or not self.source_formats <= {"ifc", "skp_json"}:
            raise MappingRuleError(f"rule {self.rule_id!r}: source_formats must be ifc/skp_json")
        patterns = (self.class_pattern, self.tag_pattern, self.definition_pattern)
        if not any(patterns):
            raise MappingRuleError(f"rule {self.rule_id!r}: needs at least one pattern")
        try:
            compiled = tuple(re.compile(p) if p else None for p in patterns)
        except re.error as exc:
            raise MappingRuleError(f"rule {self.rule_id!r}: invalid regex: {exc}") from exc
        object.__setattr__(self, "_res", compiled)

    def matches(self, source_format: str, element: ModelElement) -> bool:
        if source_format not in self.source_formats:
            return False
        class_re, tag_re, def_re = self._res
        if class_re is not None:
            subject = element.classification or element.element_type
            if not class_re.search(subject):
                return False
        if tag_re is not None and not tag_re.search(element.layer):
            return False
        if def_re is not None:
            subject = element.definition or element.name or ""
            if not def_re.search(subject):
                return False
        return True

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "kind": self.kind,
            "source_formats": sorted(self.source_formats),
            "class_pattern": self.class_pattern,
            "tag_pattern": self.tag_pattern,
            "definition_pattern": self.definition_pattern,
        }


@dataclass(frozen=True, slots=True)
class ModelMappingRuleSet:
    version: str
    rules: tuple[ModelMappingRule, ...]

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", self.version):
            raise MappingRuleError("rule-set version must match [A-Za-z0-9._-]{1,64}")
        ids = [rule.rule_id for rule in self.rules]
        if len(ids) != len(set(ids)):
            raise MappingRuleError("rule ids must be unique")

    def match(self, source_format: str, element: ModelElement) -> ModelMappingRule | None:
        for rule in self.rules:
            if rule.matches(source_format, element):
                return rule
        return None

    def as_dict(self) -> dict[str, Any]:
        return {"version": self.version, "rules": [rule.as_dict() for rule in self.rules]}

    def fingerprint(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, ensure_ascii=False)


def model_rule_set_from_dict(data: dict[str, Any]) -> ModelMappingRuleSet:
    """Build a model rule set from its JSON form (``as_dict``); raises MappingRuleError."""
    try:
        rules = tuple(
            ModelMappingRule(
                rule_id=str(item["rule_id"]),
                kind=str(item["kind"]),
                source_formats=frozenset(str(s) for s in item["source_formats"]),
                class_pattern=item.get("class_pattern"),
                tag_pattern=item.get("tag_pattern"),
                definition_pattern=item.get("definition_pattern"),
            )
            for item in data["rules"]
        )
        return ModelMappingRuleSet(version=str(data["version"]), rules=rules)
    except (KeyError, TypeError) as exc:
        raise MappingRuleError(f"malformed model rule set: {exc}") from exc


_BOTH = frozenset({"ifc", "skp_json"})
_SKP = frozenset({"skp_json"})

#: IFC classes first (they apply to IFC and to SketchUp entities classified with an IFC type),
#: then SketchUp tag / component-definition conventions (Korean office + AIA names, as MVP-1).
DEFAULT_MODEL_RULE_SET = ModelMappingRuleSet(
    version="model-2026.11",
    rules=(
        ModelMappingRule("ifc-door", "door", _BOTH, class_pattern=r"^IfcDoor(StandardCase)?$"),
        ModelMappingRule(
            "ifc-window", "window", _BOTH, class_pattern=r"^IfcWindow(StandardCase)?$"
        ),
        ModelMappingRule(
            "ifc-wall", "wall", _BOTH, class_pattern=r"^IfcWall(StandardCase|ElementedCase)?$"
        ),
        ModelMappingRule("ifc-space", "space", _BOTH, class_pattern=r"^IfcSpace$"),
        ModelMappingRule("ifc-stair", "stair", _BOTH, class_pattern=r"^IfcStair(Flight)?$"),
        ModelMappingRule(
            "ifc-column", "column", _BOTH, class_pattern=r"^IfcColumn(StandardCase)?$"
        ),
        ModelMappingRule("ifc-slab", "slab", _BOTH, class_pattern=r"^IfcSlab(StandardCase)?$"),
        ModelMappingRule("ifc-opening", "opening", _BOTH, class_pattern=r"^IfcOpeningElement$"),
        ModelMappingRule(
            "skp-door-def", "door", _SKP, definition_pattern=r"(?i)^(door|dr[-_]?\d|문|도어)"
        ),
        ModelMappingRule(
            "skp-window-def",
            "window",
            _SKP,
            definition_pattern=r"(?i)^(window|win[-_]?\d|창(?!고))",
        ),
        ModelMappingRule(
            "skp-door-tag", "door", _SKP, tag_pattern=r"(?i)^(A-DOOR|DOORS?|문(?!자)|도어)"
        ),
        ModelMappingRule(
            "skp-window-tag", "window", _SKP, tag_pattern=r"(?i)^(A-GLAZ|A-WIND|WINDOWS?|창)"
        ),
        ModelMappingRule(
            "skp-column-tag", "column", _SKP, tag_pattern=r"(?i)^(A-COLS|S-COLS|COLUMNS?|기둥)"
        ),
        ModelMappingRule(
            "skp-stair-tag", "stair", _SKP, tag_pattern=r"(?i)^(A-FLOR-STRS|A-STRS|STAIRS?|계단)"
        ),
        ModelMappingRule(
            "skp-slab-tag",
            "slab",
            _SKP,
            tag_pattern=r"(?i)^(A-FLOR(?!-STRS)|S-SLAB|SLABS?|슬래브|바닥)",
        ),
        ModelMappingRule(
            "skp-space-tag",
            "space",
            _SKP,
            tag_pattern=r"(?i)^(A-AREA|A-ROOM|A-SPACE|ROOMS?|SPACES?|실|공간|면적)",
        ),
        ModelMappingRule("skp-wall-tag", "wall", _SKP, tag_pattern=r"(?i)^(A-WALL|WALLS?|벽)"),
    ),
)

# --- element -> object --------------------------------------------------------------------

#: Which normalised quantities each kind keeps (others stay on the raw entity only).
KIND_QUANTITIES: dict[str, tuple[str, ...]] = {
    "door": ("width", "height", "area"),
    "window": ("width", "height", "area", "sill_height"),
    "wall": ("length", "height", "thickness", "area", "volume"),
    "space": ("area", "gross_area", "perimeter", "height", "volume"),
    "stair": ("width", "length", "height", "riser_height", "tread_length", "riser_count"),
    "column": ("width", "depth", "height", "area", "volume"),
    "slab": ("area", "thickness", "perimeter", "volume"),
    "opening": ("width", "height", "depth"),
}


def _shoelace(points: tuple[tuple[float, float], ...]) -> float:
    total = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1], strict=True):
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def _perimeter(points: tuple[tuple[float, float], ...]) -> float:
    seq = points + points[:1]
    return sum(
        ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5 for a, b in zip(seq, seq[1:], strict=False)
    )


def object_properties(kind: str, element: ModelElement) -> dict[str, Any]:
    """Properties of the mapped object: whitelisted quantities + name (+ derived footprint)."""
    props: dict[str, Any] = {}
    for name in KIND_QUANTITIES.get(kind, ()):
        quantity = element.quantities.get(name)
        if quantity is not None:
            props[name] = dict(quantity)
    if element.footprint and len(element.footprint) >= 3 and kind in ("space", "slab"):
        props.setdefault("area", {"value": _num(_shoelace(element.footprint)), "unit": "m2"})
        props.setdefault("perimeter", {"value": _num(_perimeter(element.footprint)), "unit": "m"})
    plan_area = element.quantities.get("plan_area")
    if plan_area is not None and kind in ("space", "slab"):
        props.setdefault("area", dict(plan_area))
    label = element.name if kind == "space" else None
    if label and label.strip():
        props["name"] = label.strip()
    if element.storey:
        props["storey"] = element.storey
    return props


def object_geometry(element: ModelElement) -> dict[str, Any]:
    """Polygon (footprint) > point (location) > bbox polygon; always in metres."""
    if element.footprint and len(element.footprint) >= 3:
        return {
            "type": "polygon",
            "coordinates": [[_num(x), _num(y)] for x, y in element.footprint],
        }
    if element.location is not None:
        return {"type": "point", "coordinates": [_num(v) for v in element.location]}
    if element.bbox is not None:
        x0, y0, x1, y1 = element.bbox
        return {
            "type": "polygon",
            "coordinates": [
                [_num(x0), _num(y0)],
                [_num(x1), _num(y0)],
                [_num(x1), _num(y1)],
                [_num(x0), _num(y1)],
            ],
        }
    return {"type": "none"}


def _bbox(
    box: tuple[float, float, float, float] | None,
) -> tuple[float, float, float, float] | None:
    if box is None:
        return None
    return (_num(box[0]), _num(box[1]), _num(box[2]), _num(box[3]))


def build_model_drawing(
    elements: list[ModelElement],
    *,
    data: bytes,
    source_format: str,
    parser_version: str,
    format_version: str,
    units: str,
    model_meta: dict[str, Any],
    rule_set: ModelMappingRuleSet = DEFAULT_MODEL_RULE_SET,
) -> ParsedDrawing:
    """Map normalised elements into a ParsedDrawing (deterministic ids and hashes)."""
    if source_format not in ("ifc", "skp_json"):
        raise CadParseError(f"unsupported model source format {source_format!r}")
    refs_seen: set[str] = set()
    for element in elements:
        if not element.ref or element.ref in refs_seen:
            raise CadParseError(f"duplicate or empty element id {element.ref!r}")
        refs_seen.add(element.ref)
    digest = sha256_hex(data)
    revision = f"{parser_version}+rules-{rule_set.version}"
    entity_key = f"{digest}\n{parser_version}"
    object_key = f"{digest}\n{revision}"

    entities: list[CadEntity] = []
    objects: list[ArchObject] = []
    unmapped = 0
    for element in sorted(elements, key=lambda e: e.ref):
        geometry = element.geometry_json()
        body = {
            "handle": element.ref,
            "layer": element.layer,
            "type": element.element_type,
            "block": element.definition,
            "geometry": geometry,
        }
        entities.append(
            CadEntity(
                id=str(uuid.uuid5(CAD_NAMESPACE, f"ent\n{entity_key}\n{element.ref}")),
                handle=element.ref,
                layer=element.layer,
                entity_type=element.element_type,
                block_name=element.definition,
                geometry=geometry,
                bbox=_bbox(element.bbox),
                entity_hash=sha256_hex(canonical_json(body)),
            )
        )
        rule = rule_set.match(source_format, element)
        if rule is None:
            unmapped += 1
            continue
        props = object_properties(rule.kind, element)
        geom = object_geometry(element)
        fact_units = {
            f"{rule.kind}.{name}": prop["unit"]
            for name, prop in sorted(props.items())
            if isinstance(prop, dict) and "unit" in prop
        }
        handles = (element.ref,)
        obj_body = {
            "kind": rule.kind,
            "rule": rule.rule_id,
            "layer": element.layer,
            "properties": props,
            "geometry": geom,
            "handles": list(handles),
            "refs": sorted([list(r) for r in element.refs]),
        }
        objects.append(
            ArchObject(
                id=str(uuid.uuid5(CAD_NAMESPACE, f"obj\n{object_key}\n{rule.kind}\n{element.ref}")),
                kind=rule.kind,
                rule_id=rule.rule_id,
                layer=element.layer,
                properties=props,
                fact_units=fact_units,
                geometry=geom,
                source_handles=handles,
                object_hash=sha256_hex(canonical_json(obj_body)),
                source_refs=tuple(sorted(element.refs)),
            )
        )

    content_digest = sha256_hex(
        canonical_json(
            {
                "sha256": digest,
                "parser_revision": revision,
                "units": units,
                "model_meta": model_meta,
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
        units_assumed=False,
        dxf_version=format_version,
        entities=tuple(entities),
        objects=tuple(objects),
        unmapped_entity_count=unmapped,
        content_digest=content_digest,
        source_format=source_format,
        model_meta=model_meta,
    )


# --- storeys -> building facts ------------------------------------------------------------


def building_facts(model_meta: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, str]]:
    """``building.floor_count`` / ``basement_count`` / ``storey_count`` from model storeys.

    A storey whose elevation is below -0.5 m counts as a basement (지하층); the rest are above
    ground (지상층). Only IFC (or SketchUp exports that declare storeys) provide this.
    """
    storeys = (model_meta or {}).get("storeys")
    if not isinstance(storeys, list) or not storeys:
        return {}, {}
    basements = sum(
        1
        for s in storeys
        if isinstance(s, dict)
        and isinstance(s.get("elevation_m"), (int, float))
        and s["elevation_m"] < -0.5
    )
    building = {
        "storey_count": len(storeys),
        "floor_count": len(storeys) - basements,
        "basement_count": basements,
    }
    return {"building": building}, {f"building.{k}": "count" for k in building}


# --- format detection ---------------------------------------------------------------------

SKP_BINARY_MAGIC = b"\xff\xfe\xff\x0eS\x00k\x00e\x00t\x00c\x00h\x00U\x00p\x00"


class UnsupportedModelFormat(ValueError):
    """The upload is a format the service cannot parse (e.g. a binary .skp)."""


def detect_source_format(
    data: bytes,
    *,
    explicit: str | None = None,
    content_type: str | None = None,
    file_name: str | None = None,
) -> str:
    """dxf | ifc | skp_json: explicit choice, then content sniffing, then name/content type."""
    if explicit and explicit != "auto":
        if explicit not in SOURCE_FORMATS:
            raise UnsupportedModelFormat(f"source_format must be one of {SOURCE_FORMATS}")
        return explicit
    head = data[:64].lstrip(b"\xef\xbb\xbf \t\r\n")
    if data.startswith(SKP_BINARY_MAGIC) or (file_name or "").lower().endswith(".skp"):
        raise UnsupportedModelFormat(
            "binary .skp files cannot be parsed; export archontos-skp-export/1 JSON instead"
        )
    if head.startswith(b"ISO-10303-21"):
        return "ifc"
    if head.startswith(b"{"):
        return "skp_json"
    name = (file_name or "").lower()
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    if name.endswith(".ifc") or ctype in ("application/x-step", "model/ifc", "application/ifc"):
        return "ifc"
    if name.endswith(".json") or ctype == "application/json":
        return "skp_json"
    return "dxf"
