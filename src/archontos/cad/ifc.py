"""IFC -> model elements (MVP-4), via ifcopenshell (optional ``bim`` extra).

Reads building elements and spaces with their GlobalId, containing storey, attributes
(``OverallWidth``/``OverallHeight``), quantity sets (``Qto_*`` / IFC2x3 ``BaseQuantities``) and
property sets, normalises every quantity to m / m2 / m3 using the project's unit assignment,
and hands the elements to :func:`archontos.cad.model.build_model_drawing`.

The quantity selection (:func:`pick_quantities`) and unit arithmetic (:func:`unit_scale`) are
pure and tested without ifcopenshell; only :func:`parse_ifc` needs the library.
"""

from __future__ import annotations

import re
from typing import Any

from archontos.cad.dxf import CadDependencyError, CadParseError, ParsedDrawing
from archontos.cad.model import (
    DEFAULT_MODEL_RULE_SET,
    ModelElement,
    ModelMappingRuleSet,
    build_model_drawing,
)

IFC_PARSER_VERSION = "ifc-1.2"  # 1.1: door/window host_ref; 1.2: IfcOpeningElement

#: IFC SI prefixes -> factor.
SI_PREFIXES: dict[str | None, float] = {
    None: 1.0,
    "EXA": 1e18,
    "PETA": 1e15,
    "TERA": 1e12,
    "GIGA": 1e9,
    "MEGA": 1e6,
    "KILO": 1e3,
    "HECTO": 1e2,
    "DECA": 1e1,
    "DECI": 1e-1,
    "CENTI": 1e-2,
    "MILLI": 1e-3,
    "MICRO": 1e-6,
    "NANO": 1e-9,
    "PICO": 1e-12,
    "FEMTO": 1e-15,
    "ATTO": 1e-18,
}
_UNIT_DIMENSION = {"LENGTHUNIT": 1, "AREAUNIT": 2, "VOLUMEUNIT": 3}
_LENGTH_NAMES = {0.001: "mm", 0.01: "cm", 1.0: "m", 0.0254: "in", 0.3048: "ft"}

#: Quantity family per IFC class.
_FAMILIES: tuple[tuple[str, str], ...] = (
    (r"^IfcDoor", "door"),
    (r"^IfcWindow", "window"),
    (r"^IfcWall", "wall"),
    (r"^IfcSpace$", "space"),
    (r"^IfcStair", "stair"),
    (r"^IfcColumn", "column"),
    (r"^IfcSlab", "slab"),
    (r"^IfcOpening", "opening"),
)

#: (target property, dimension 0=count/1=length/2=area/3=volume, candidate IFC names).
QUANTITY_SPECS: dict[str, tuple[tuple[str, int, tuple[str, ...]], ...]] = {
    "door": (
        ("width", 1, ("OverallWidth", "Width")),
        ("height", 1, ("OverallHeight", "Height")),
        ("area", 2, ("Area",)),
    ),
    "window": (
        ("width", 1, ("OverallWidth", "Width")),
        ("height", 1, ("OverallHeight", "Height")),
        ("area", 2, ("Area",)),
    ),
    "wall": (
        ("length", 1, ("Length",)),
        ("height", 1, ("Height",)),
        ("thickness", 1, ("Width", "Thickness")),
        ("area", 2, ("NetSideArea", "GrossSideArea")),
        ("volume", 3, ("NetVolume", "GrossVolume")),
    ),
    "space": (
        ("area", 2, ("NetFloorArea", "GrossFloorArea")),
        ("gross_area", 2, ("GrossFloorArea",)),
        ("perimeter", 1, ("GrossPerimeter", "NetPerimeter")),
        ("height", 1, ("Height", "FinishCeilingHeight", "ClearHeight")),
        ("volume", 3, ("NetVolume", "GrossVolume")),
    ),
    "stair": (
        ("width", 1, ("Width", "ClearWidth", "NominalWidth", "RequiredWidth")),
        ("length", 1, ("Length",)),
        ("height", 1, ("Height",)),
        ("riser_height", 1, ("RiserHeight",)),
        ("tread_length", 1, ("TreadLength",)),
        ("riser_count", 0, ("NumberOfRiser", "NumberOfRisers")),
    ),
    "column": (
        ("height", 1, ("Length", "Height")),
        ("width", 1, ("Width",)),
        ("depth", 1, ("Depth",)),
        ("area", 2, ("CrossSectionArea",)),
        ("volume", 3, ("NetVolume", "GrossVolume")),
    ),
    "opening": (
        ("width", 1, ("Width",)),
        ("height", 1, ("Height",)),
        ("depth", 1, ("Depth",)),
    ),
    "slab": (
        ("area", 2, ("NetArea", "GrossArea")),
        ("thickness", 1, ("Width", "Depth", "Thickness")),
        ("perimeter", 1, ("Perimeter",)),
        ("volume", 3, ("NetVolume", "GrossVolume")),
    ),
}
_DIM_UNIT = {1: "m", 2: "m2", 3: "m3"}


def quantity_family(ifc_class: str) -> str | None:
    for pattern, family in _FAMILIES:
        if re.search(pattern, ifc_class):
            return family
    return None


def si_prefix_factor(prefix: str | None) -> float:
    try:
        return SI_PREFIXES[prefix]
    except KeyError as exc:
        raise CadParseError(f"unknown IFC SI prefix {prefix!r}") from exc


def unit_scale(unit: Any, dimension: int) -> float:
    """SI factor of one IfcNamedUnit (IfcSIUnit or IfcConversionBasedUnit), duck-typed."""
    kind = unit.is_a()
    if kind == "IfcSIUnit":
        return si_prefix_factor(getattr(unit, "Prefix", None)) ** dimension
    if kind in ("IfcConversionBasedUnit", "IfcConversionBasedUnitWithOffset"):
        factor = unit.ConversionFactor
        value = factor.ValueComponent
        raw = getattr(value, "wrappedValue", value)
        return float(raw) * unit_scale(factor.UnitComponent, dimension)
    raise CadParseError(f"unsupported IFC unit {kind}")


def project_scales(units: list[Any]) -> dict[int, float]:
    """{1: length, 2: area, 3: volume} SI factors from IfcUnitAssignment.Units.

    A missing area/volume unit falls back to length^2 / length^3; a missing length unit to
    metres (IFC's default)."""
    found: dict[int, float] = {}
    for unit in units:
        unit_type = getattr(unit, "UnitType", None)
        if unit_type in _UNIT_DIMENSION:
            found[_UNIT_DIMENSION[unit_type]] = unit_scale(unit, _UNIT_DIMENSION[unit_type])
    length = found.get(1, 1.0)
    return {1: length, 2: found.get(2, length**2), 3: found.get(3, length**3)}


def length_unit_name(scale: float) -> str:
    for value, name in _LENGTH_NAMES.items():
        if abs(value - scale) < 1e-12:
            return name
    return f"{scale:g}m"


def _number(value: Any) -> float | None:
    raw = getattr(value, "wrappedValue", value)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    if raw != raw:
        return None
    return float(raw)


def pick_quantities(
    family: str | None,
    *,
    attributes: dict[str, Any],
    quantity_sets: dict[str, dict[str, Any]],
    property_sets: dict[str, dict[str, Any]],
    scales: dict[int, float],
) -> dict[str, dict[str, Any]]:
    """Normalised quantities for one element: attributes, then Qto sets, then Psets.

    The first candidate name found wins; Qto sets are searched in name order with ``Qto_*``
    before IFC2x3 ``BaseQuantities``. Values are scaled to m / m2 / m3 (counts unchanged).
    """
    if family is None:
        return {}
    qsets = sorted(quantity_sets.items(), key=lambda kv: (not kv[0].startswith("Qto_"), kv[0]))
    psets = sorted(property_sets.items())
    sources: list[dict[str, Any]] = [attributes, *(v for _, v in qsets), *(v for _, v in psets)]
    out: dict[str, dict[str, Any]] = {}
    for target, dimension, names in QUANTITY_SPECS.get(family, ()):
        for name in names:
            value = next((n for src in sources if (n := _number(src.get(name))) is not None), None)
            if value is None:
                continue
            if dimension == 0:
                if value >= 0 and value.is_integer():
                    out[target] = {"value": value, "unit": "count"}
                    break
                continue
            if value <= 0:
                continue
            out[target] = {
                "value": _round(value * scales[dimension]),
                "unit": _DIM_UNIT[dimension],
            }
            break
    return out


def _round(value: float) -> float:
    rounded = round(value, 6)
    return 0.0 if rounded == 0 else rounded


# --- ifcopenshell-dependent part ----------------------------------------------------------


def _require_ifcopenshell() -> Any:
    try:
        import ifcopenshell
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise CadDependencyError(
            "IFC parsing needs the optional 'bim' extra: pip install -e '.[bim]'"
        ) from exc
    return ifcopenshell


def _storey_of(element: Any) -> Any:
    import ifcopenshell.util.element as eu

    current = element
    for _ in range(16):
        parent = eu.get_container(current) or eu.get_aggregate(current)
        if parent is None:
            return None
        if parent.is_a("IfcBuildingStorey"):
            return parent
        current = parent
    return None


def _matrix(placement: Any) -> Any:
    import ifcopenshell.util.placement as up

    return up.get_local_placement(placement)


def _transform(m: Any, x: float, y: float, z: float = 0.0) -> tuple[float, float]:
    return (
        float(m[0][0] * x + m[0][1] * y + m[0][2] * z + m[0][3]),
        float(m[1][0] * x + m[1][1] * y + m[1][2] * z + m[1][3]),
    )


def _profile_points(profile: Any) -> list[tuple[float, float]] | None:
    """2D outline of an extrusion profile in profile coordinates (project length units)."""
    if profile.is_a("IfcRectangleProfileDef"):
        hx, hy = float(profile.XDim) / 2, float(profile.YDim) / 2
        pts = [(-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy)]
        pos = getattr(profile, "Position", None)
        if pos is not None:
            ox, oy = (float(c) for c in pos.Location.Coordinates[:2])
            ref = getattr(pos, "RefDirection", None)
            cx, cy = (float(c) for c in ref.DirectionRatios[:2]) if ref else (1.0, 0.0)
            norm = (cx * cx + cy * cy) ** 0.5 or 1.0
            cx, cy = cx / norm, cy / norm
            pts = [(ox + x * cx - y * cy, oy + x * cy + y * cx) for x, y in pts]
        return pts
    if profile.is_a("IfcArbitraryClosedProfileDef"):
        curve = profile.OuterCurve
        if curve.is_a("IfcPolyline"):
            pts = [(float(p.Coordinates[0]), float(p.Coordinates[1])) for p in curve.Points]
        elif curve.is_a("IfcIndexedPolyCurve"):
            pts = [(float(c[0]), float(c[1])) for c in curve.Points.CoordList]
        else:
            return None
        if len(pts) > 1 and pts[0] == pts[-1]:
            pts = pts[:-1]
        return pts if len(pts) >= 3 else None
    return None


def _footprint(element: Any, scale: float) -> tuple[tuple[float, float], ...] | None:
    """Plan polygon (metres) of an extruded-area-solid body, if the element has one."""
    import ifcopenshell.util.placement as up

    rep = getattr(element, "Representation", None)
    placement = getattr(element, "ObjectPlacement", None)
    if rep is None or placement is None:
        return None
    obj = _matrix(placement)
    for representation in rep.Representations:
        for item in representation.Items:
            if not item.is_a("IfcExtrudedAreaSolid"):
                continue
            pts = _profile_points(item.SweptArea)
            if not pts:
                continue
            local = up.get_axis2placement(item.Position) if item.Position else None
            out = []
            for x, y in pts:
                if local is not None:
                    x, y = _transform(local, x, y)
                wx, wy = _transform(obj, x, y)
                out.append((wx * scale, wy * scale))
            return tuple(out)
    return None


def host_refs(item: Any) -> list[str]:
    """GlobalIds of the elements a door/window fills: IfcRelFillsElement -> opening ->
    IfcRelVoidsElement -> host (wall). Sorted, without duplicates."""
    hosts: set[str] = set()
    for fills in getattr(item, "FillsVoids", None) or ():
        opening = fills.RelatingOpeningElement
        for voids in getattr(opening, "VoidsElements", None) or ():
            host = voids.RelatingBuildingElement
            if host is not None and getattr(host, "GlobalId", None):
                hosts.add(str(host.GlobalId))
    return sorted(hosts)


def opening_refs(item: Any) -> list[str]:
    """GlobalIds of the IfcOpeningElements a door/window fills (IfcRelFillsElement)."""
    return sorted(
        {
            str(fills.RelatingOpeningElement.GlobalId)
            for fills in getattr(item, "FillsVoids", None) or ()
            if fills.RelatingOpeningElement is not None
        }
    )


def _element(item: Any, scales: dict[int, float], storey_names: dict[int, str]) -> ModelElement:
    import ifcopenshell.util.element as eu

    ifc_class = item.is_a()
    family = quantity_family(ifc_class)
    attrs = {
        name: getattr(item, name, None)
        for name in ("OverallWidth", "OverallHeight")
        if hasattr(item, name)
    }
    qsets = {
        k: {n: v for n, v in props.items() if n != "id"}
        for k, props in eu.get_psets(item, qtos_only=True).items()
    }
    psets = {
        k: {n: v for n, v in props.items() if n != "id"}
        for k, props in eu.get_psets(item, psets_only=True).items()
    }
    quantities = pick_quantities(
        family, attributes=attrs, quantity_sets=qsets, property_sets=psets, scales=scales
    )
    storey = _storey_of(item)
    storey_name = storey_names.get(storey.id()) if storey is not None else None
    location = None
    if getattr(item, "ObjectPlacement", None) is not None:
        m = _matrix(item.ObjectPlacement)
        location = (float(m[0][3]) * scales[1], float(m[1][3]) * scales[1])
    footprint = _footprint(item, scales[1]) if family in ("space", "slab") else None
    name = (getattr(item, "LongName", None) or item.Name) if family == "space" else item.Name
    attributes: dict[str, Any] = {}
    if family in ("door", "window"):
        hosts = host_refs(item)
        if len(hosts) == 1:
            attributes["host_ref"] = hosts[0]
        elif hosts:
            attributes["host_ref"] = hosts
        openings = opening_refs(item)
        if openings:
            attributes["opening_ref"] = openings[0] if len(openings) == 1 else openings
    elif family == "opening":
        # IfcRelVoidsElement -> host (wall/slab); IfcRelFillsElement -> filler (door/window).
        voided = sorted(
            str(rel.RelatingBuildingElement.GlobalId)
            for rel in getattr(item, "VoidsElements", None) or ()
            if rel.RelatingBuildingElement is not None
        )
        if voided:
            attributes["host_ref"] = voided[0] if len(voided) == 1 else voided
        fillers = sorted(
            str(rel.RelatedBuildingElement.GlobalId)
            for rel in getattr(item, "HasFillings", None) or ()
            if rel.RelatedBuildingElement is not None
        )
        if fillers:
            attributes["filler_refs"] = fillers
    xs = [p[0] for p in footprint] if footprint else []
    ys = [p[1] for p in footprint] if footprint else []
    return ModelElement(
        ref=str(item.GlobalId),
        element_type=ifc_class,
        layer=storey_name or "(no storey)",
        name=name,
        definition=getattr(item, "ObjectType", None),
        classification=ifc_class,
        storey=storey_name,
        quantities=quantities,
        location=location,
        footprint=footprint,
        bbox=(min(xs), min(ys), max(xs), max(ys)) if footprint else None,
        refs=(("ifc_guid", str(item.GlobalId)),),
        attributes=attributes,
    )


def _open(data: bytes) -> Any:
    ifcopenshell = _require_ifcopenshell()
    if not data:
        raise CadParseError("empty IFC upload")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    if not text.lstrip("﻿ \t\r\n").startswith("ISO-10303-21"):
        raise CadParseError("not an IFC STEP file (ISO-10303-21 header missing)")
    try:
        return ifcopenshell.file.from_string(text)
    except Exception as exc:  # noqa: BLE001 - ifcopenshell raises plain RuntimeError/Error
        raise CadParseError(f"not a readable IFC file: {exc}") from exc


def parse_ifc(
    data: bytes, *, rule_set: ModelMappingRuleSet = DEFAULT_MODEL_RULE_SET
) -> ParsedDrawing:
    """Parse IFC (STEP physical file) bytes into a ParsedDrawing (source_format ``ifc``)."""
    model = _open(data)
    projects = model.by_type("IfcProject")
    units = []
    if projects and projects[0].UnitsInContext is not None:
        units = list(projects[0].UnitsInContext.Units)
    scales = project_scales(units)

    storeys = sorted(
        model.by_type("IfcBuildingStorey"),
        key=lambda s: (_number(s.Elevation) or 0.0, s.Name or "", s.GlobalId),
    )
    storey_names = {s.id(): (s.Name or s.GlobalId) for s in storeys}
    storey_meta = [
        {
            "guid": s.GlobalId,
            "name": s.Name or s.GlobalId,
            "elevation_m": _round((_number(s.Elevation) or 0.0) * scales[1]),
        }
        for s in storeys
    ]

    building_class = (
        "IfcBuiltElement" if model.schema.upper().startswith("IFC4X3") else ("IfcBuildingElement")
    )
    try:
        items = (
            list(model.by_type(building_class))
            + list(model.by_type("IfcSpace"))
            + list(model.by_type("IfcOpeningElement"))
        )
    except RuntimeError as exc:
        raise CadParseError(f"unsupported IFC schema {model.schema}: {exc}") from exc
    elements = []
    for item in items:
        if item.is_a("IfcStair") and any(
            part.is_a("IfcStairFlight")
            for rel in getattr(item, "IsDecomposedBy", ()) or ()
            for part in rel.RelatedObjects
        ):
            continue  # flights carry the stair facts; the container would double-count
        elements.append(_element(item, scales, storey_names))

    meta: dict[str, Any] = {
        "schema": model.schema,
        "length_unit": length_unit_name(scales[1]),
        "storeys": storey_meta,
    }
    if projects:
        meta["project_guid"] = projects[0].GlobalId
    return build_model_drawing(
        elements,
        data=data,
        source_format="ifc",
        parser_version=IFC_PARSER_VERSION,
        format_version=str(model.schema),
        units=length_unit_name(scales[1]),
        model_meta=meta,
        rule_set=rule_set,
    )
