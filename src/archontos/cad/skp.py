"""SketchUp JSON export (``archontos-skp-export/1``) -> model elements (MVP-4).

``.skp`` binaries cannot be parsed in Python, so a SketchUp-side exporter (a Ruby extension,
or the SketchUp MCP servers on the workstation) writes this JSON contract instead. It is
validated with :data:`SKP_EXPORT_SCHEMA` (JSON Schema 2020-12) before anything is read.
Pure: no SketchUp, no network. See docs/MVP4-IFC-SKETCHUP.md for the field reference.
"""

from __future__ import annotations

import json
from typing import Any

from archontos.cad.dxf import CadParseError, ParsedDrawing
from archontos.cad.model import (
    DEFAULT_MODEL_RULE_SET,
    LENGTH_FACTORS,
    ModelElement,
    ModelMappingRuleSet,
    build_model_drawing,
    normalize_quantity,
)

SKP_SCHEMA_ID = "archontos-skp-export/1"
SKP_PARSER_VERSION = "skp-json-1.0"
SKP_UNITS = ("inch", "ft", "mm", "cm", "m")

_POINT3 = {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3}
_QUANTITY = {
    "type": "object",
    "required": ["value", "unit"],
    "properties": {
        "value": {"type": "number"},
        "unit": {
            "enum": [
                "mm",
                "cm",
                "m",
                "in",
                "inch",
                "ft",
                "mm2",
                "cm2",
                "m2",
                "in2",
                "ft2",
                "mm3",
                "cm3",
                "m3",
                "in3",
                "ft3",
                "deg",
                "rad",
                "count",
            ]
        },
    },
    "additionalProperties": False,
}

#: JSON Schema for the SketchUp export contract (documented in docs/MVP4-IFC-SKETCHUP.md).
SKP_EXPORT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://archontos.dev/schemas/archontos-skp-export-1.json",
    "title": SKP_SCHEMA_ID,
    "type": "object",
    "required": ["schema", "model", "entities"],
    "properties": {
        "schema": {"const": SKP_SCHEMA_ID},
        "model": {
            "type": "object",
            "required": ["units"],
            "properties": {
                "name": {"type": "string", "maxLength": 300},
                "guid": {"type": "string", "maxLength": 100},
                "units": {"enum": list(SKP_UNITS)},
                "exporter": {"type": "string", "maxLength": 200},
                "sketchup_version": {"type": "string", "maxLength": 50},
            },
            "additionalProperties": True,
        },
        "storeys": {
            "type": "array",
            "maxItems": 500,
            "items": {
                "type": "object",
                "required": ["name", "elevation"],
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 200},
                    "elevation": {"type": "number"},
                },
                "additionalProperties": False,
            },
        },
        "entities": {
            "type": "array",
            "maxItems": 200000,
            "items": {
                "type": "object",
                "required": ["persistent_id", "entity_type", "tag", "bbox"],
                "properties": {
                    "persistent_id": {
                        "oneOf": [
                            {"type": "integer", "minimum": 0},
                            {"type": "string", "pattern": "^[0-9A-Za-z_$-]{1,64}$"},
                        ]
                    },
                    "entity_type": {"enum": ["ComponentInstance", "Group"]},
                    "tag": {"type": "string", "minLength": 1, "maxLength": 300},
                    "name": {"type": "string", "maxLength": 300},
                    "definition": {"type": "string", "maxLength": 300},
                    "ifc_type": {"type": "string", "pattern": "^Ifc[A-Za-z]{2,60}$"},
                    "ifc_guid": {"type": "string", "pattern": "^[0-9A-Za-z_$]{22}$"},
                    "storey": {"type": "string", "maxLength": 200},
                    "bbox": {
                        "type": "object",
                        "required": ["min", "max"],
                        "properties": {"min": _POINT3, "max": _POINT3},
                        "additionalProperties": False,
                    },
                    "transform": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 16,
                        "maxItems": 16,
                    },
                    "footprint": {
                        "type": "array",
                        "minItems": 3,
                        "maxItems": 10000,
                        "items": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 2,
                            "maxItems": 3,
                        },
                    },
                    "quantities": {
                        "type": "object",
                        "propertyNames": {"pattern": "^[a-z][a-z0-9_]{0,40}$"},
                        "additionalProperties": _QUANTITY,
                    },
                    "attributes": {"type": "object"},
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}


def validate_export(document: Any) -> None:
    """Raise CadParseError with the first JSON Schema violation (path + message)."""
    import jsonschema

    validator = jsonschema.Draft202012Validator(SKP_EXPORT_SCHEMA)
    errors = sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        path = "/".join(str(p) for p in first.absolute_path) or "(root)"
        raise CadParseError(f"{SKP_SCHEMA_ID} invalid at {path}: {first.message}")


def _derived_quantities(dx: float, dy: float, dz: float) -> dict[str, Any]:
    """Quantities from the world bbox (metres) when the exporter gave none explicitly."""
    long_side, short_side = max(dx, dy), min(dx, dy)
    out: dict[str, Any] = {}
    if dz > 0:
        out["height"] = dz
    if long_side > 0:
        out["width"] = long_side
        out["length"] = long_side
    if short_side > 0:
        out["depth"] = short_side
        out["thickness"] = short_side
    if dx > 0 and dy > 0:
        out["area"] = dx * dy
    return out


def element_from_entity(entity: dict[str, Any], factor: float) -> ModelElement:
    """One validated export entity -> ModelElement (lengths scaled by ``factor`` to metres)."""
    pid = str(entity["persistent_id"])
    lo, hi = entity["bbox"]["min"], entity["bbox"]["max"]
    x0, y0, z0 = (min(a, b) * factor for a, b in zip(lo, hi, strict=True))
    x1, y1, z1 = (max(a, b) * factor for a, b in zip(lo, hi, strict=True))
    dx, dy, dz = x1 - x0, y1 - y0, z1 - z0

    quantities: dict[str, dict[str, Any]] = {}
    for name, value in _derived_quantities(dx, dy, dz).items():
        unit = "m2" if name == "area" else "m"
        normalized = normalize_quantity(value, unit)
        if normalized is not None:
            quantities[name] = normalized
    footprint = None
    if entity.get("footprint"):
        footprint = tuple((float(p[0]) * factor, float(p[1]) * factor) for p in entity["footprint"])
        quantities.pop("area", None)  # the polygon area wins over the bbox rectangle
    for name, quantity in (entity.get("quantities") or {}).items():
        unit = quantity["unit"]
        normalized = normalize_quantity(quantity["value"], unit)
        if normalized is None:
            raise CadParseError(f"entity {pid}: quantity {name} has an unusable unit {unit!r}")
        quantities[name] = normalized

    refs: list[tuple[str, str]] = [("skp_persistent_id", pid)]
    if entity.get("ifc_guid"):
        refs.append(("ifc_guid", entity["ifc_guid"]))
    attributes = {}
    if entity.get("transform"):
        attributes["transform"] = entity["transform"]
    if entity.get("attributes"):
        attributes["attributes"] = entity["attributes"]
    return ModelElement(
        ref=pid,
        element_type=entity["entity_type"],
        layer=entity["tag"],
        name=entity.get("name"),
        definition=entity.get("definition"),
        classification=entity.get("ifc_type"),
        storey=entity.get("storey"),
        quantities=quantities,
        location=None if footprint else ((x0 + x1) / 2, (y0 + y1) / 2),
        footprint=footprint,
        bbox=(x0, y0, x1, y1),
        refs=tuple(refs),
        attributes=attributes,
    )


def load_export(data: bytes) -> dict[str, Any]:
    if not data:
        raise CadParseError("empty SketchUp JSON upload")
    try:
        document = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CadParseError(f"not valid UTF-8 JSON: {exc}") from exc
    validate_export(document)
    ids = [str(e["persistent_id"]) for e in document["entities"]]
    if len(ids) != len(set(ids)):
        raise CadParseError(f"{SKP_SCHEMA_ID}: persistent_id values must be unique")
    return document


def parse_skp_json(
    data: bytes, *, rule_set: ModelMappingRuleSet = DEFAULT_MODEL_RULE_SET
) -> ParsedDrawing:
    """Parse ``archontos-skp-export/1`` bytes into a ParsedDrawing (source_format skp_json)."""
    document = load_export(data)
    units = document["model"]["units"]
    factor = LENGTH_FACTORS[units]
    elements = [element_from_entity(e, factor) for e in document["entities"]]
    storeys = [
        {"name": s["name"], "elevation_m": round(float(s["elevation"]) * factor, 6)}
        for s in sorted(document.get("storeys") or [], key=lambda s: (s["elevation"], s["name"]))
    ]
    meta: dict[str, Any] = {"schema": SKP_SCHEMA_ID, "length_unit": units}
    for key in ("name", "guid", "exporter", "sketchup_version"):
        if document["model"].get(key):
            meta[f"model_{key}"] = document["model"][key]
    if storeys:
        meta["storeys"] = storeys
    return build_model_drawing(
        elements,
        data=data,
        source_format="skp_json",
        parser_version=SKP_PARSER_VERSION,
        format_version=SKP_SCHEMA_ID,
        units=units,
        model_meta=meta,
        rule_set=rule_set,
    )
