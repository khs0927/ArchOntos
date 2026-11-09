"""MVP-4: SketchUp JSON / IFC model import, mapping rules, units, checks and diffs (pure)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from archontos.cad.checks import extract_fact_space
from archontos.cad.diff import diff_objects
from archontos.cad.dxf import CadParseError, ParsedDrawing
from archontos.cad.ifc import pick_quantities, project_scales, quantity_family, unit_scale
from archontos.cad.links import link_id, object_refs
from archontos.cad.model import (
    DEFAULT_MODEL_RULE_SET,
    ModelElement,
    UnsupportedModelFormat,
    building_facts,
    detect_source_format,
    model_rule_set_from_dict,
    normalize_quantity,
)
from archontos.cad.rules import MappingRuleError
from archontos.cad.skp import parse_skp_json
from tests.model_fixtures import DOOR_GUID, skp_bytes, skp_export


def _rows(drawing: ParsedDrawing) -> list[dict[str, Any]]:
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
        for o in drawing.objects
    ]


def _by_kind(drawing: ParsedDrawing) -> dict[str, Any]:
    return {o.kind: o for o in drawing.objects}


# --- units --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        (900, "mm", {"value": 0.9, "unit": "m"}),
        (36, "in", {"value": 0.9144, "unit": "m"}),
        (24_000_000, "mm2", {"value": 24.0, "unit": "m2"}),
        (10, "ft2", {"value": 0.92903, "unit": "m2"}),
        (1, "m3", {"value": 1.0, "unit": "m3"}),
        (3, "count", {"value": 3.0, "unit": "count"}),
    ],
)
def test_normalize_quantity(value, unit, expected):
    assert normalize_quantity(value, unit) == expected


def test_normalize_quantity_rejects_garbage():
    assert normalize_quantity(True, "m") is None
    assert normalize_quantity(1.5, "count") is None
    assert normalize_quantity(float("nan"), "m") is None
    assert normalize_quantity(1, "furlong") is None


class _Unit:
    def __init__(self, kind: str, **attrs: Any):
        self._kind = kind
        self.__dict__.update(attrs)

    def is_a(self) -> str:
        return self._kind


def test_ifc_unit_scales_si_and_conversion_based():
    mm = _Unit("IfcSIUnit", UnitType="LENGTHUNIT", Prefix="MILLI")
    m2 = _Unit("IfcSIUnit", UnitType="AREAUNIT", Prefix=None)
    assert project_scales([mm, m2]) == {1: 0.001, 2: 1.0, 3: pytest.approx(1e-9)}
    foot = _Unit(
        "IfcConversionBasedUnit",
        UnitType="LENGTHUNIT",
        ConversionFactor=_Unit(
            "IfcMeasureWithUnit",
            ValueComponent=0.3048,
            UnitComponent=_Unit("IfcSIUnit", Prefix=None),
        ),
    )
    assert unit_scale(foot, 1) == pytest.approx(0.3048)
    assert project_scales([foot])[2] == pytest.approx(0.3048**2)
    assert project_scales([]) == {1: 1.0, 2: 1.0, 3: 1.0}


def test_pick_quantities_prefers_attributes_then_qto_then_pset():
    scales = {1: 0.001, 2: 1.0, 3: 1.0}
    door = pick_quantities(
        "door",
        attributes={"OverallWidth": 900.0, "OverallHeight": None},
        quantity_sets={"Qto_DoorBaseQuantities": {"Width": 1000.0, "Height": 2100.0}},
        property_sets={},
        scales=scales,
    )
    assert door == {"width": {"value": 0.9, "unit": "m"}, "height": {"value": 2.1, "unit": "m"}}
    space = pick_quantities(
        "space",
        attributes={},
        quantity_sets={
            "BaseQuantities": {"NetFloorArea": 99.0},
            "Qto_SpaceBaseQuantities": {"NetFloorArea": 24.0, "GrossFloorArea": 26.0},
        },
        property_sets={},
        scales=scales,
    )
    assert space["area"] == {"value": 24.0, "unit": "m2"}
    assert space["gross_area"] == {"value": 26.0, "unit": "m2"}
    stair = pick_quantities(
        "stair",
        attributes={},
        quantity_sets={},
        property_sets={"Pset_StairFlightCommon": {"Width": 1200.0, "NumberOfRiser": 18}},
        scales=scales,
    )
    assert stair["width"] == {"value": 1.2, "unit": "m"}
    assert stair["riser_count"] == {"value": 18.0, "unit": "count"}
    assert (
        pick_quantities(None, attributes={}, quantity_sets={}, property_sets={}, scales=scales)
        == {}
    )


def test_quantity_family():
    assert quantity_family("IfcWallStandardCase") == "wall"
    assert quantity_family("IfcStairFlight") == "stair"
    assert quantity_family("IfcBeam") is None


# --- mapping rules ------------------------------------------------------------------------


def test_default_rules_map_ifc_classes_and_skp_tags():
    def kind(source: str, **kw: Any) -> str | None:
        element = ModelElement(ref="x", element_type=kw.pop("element_type", "Group"), **kw)
        rule = DEFAULT_MODEL_RULE_SET.match(source, element)
        return rule.kind if rule else None

    assert kind("ifc", element_type="IfcDoor", layer="1F", classification="IfcDoor") == "door"
    assert kind("ifc", element_type="IfcSlab", layer="1F", classification="IfcSlab") == "slab"
    assert kind("ifc", element_type="IfcBeam", layer="1F", classification="IfcBeam") is None
    assert kind("skp_json", layer="벽") == "wall"
    assert kind("skp_json", layer="Untagged", definition="Door 900") == "door"
    assert kind("skp_json", layer="창고") is None
    assert kind("skp_json", layer="Untagged", classification="IfcSpace") == "space"
    # SketchUp tag rules never apply to IFC elements.
    assert kind("ifc", element_type="IfcBuildingElementProxy", layer="벽") is None


def test_model_rule_set_round_trip_and_validation():
    again = model_rule_set_from_dict(DEFAULT_MODEL_RULE_SET.as_dict())
    assert again.fingerprint() == DEFAULT_MODEL_RULE_SET.fingerprint()
    with pytest.raises(MappingRuleError):
        model_rule_set_from_dict(
            {
                "version": "v1",
                "rules": [
                    {
                        "rule_id": "a",
                        "kind": "roof",
                        "source_formats": ["ifc"],
                        "class_pattern": "x",
                    }
                ],
            }
        )
    with pytest.raises(MappingRuleError):
        model_rule_set_from_dict(
            {
                "version": "v1",
                "rules": [
                    {
                        "rule_id": "a",
                        "kind": "wall",
                        "source_formats": ["dxf"],
                        "class_pattern": "x",
                    }
                ],
            }
        )
    with pytest.raises(MappingRuleError):
        model_rule_set_from_dict({"version": "v1"})


# --- SketchUp JSON ------------------------------------------------------------------------


def test_skp_export_maps_objects_with_units_and_refs():
    drawing = parse_skp_json(skp_bytes())
    assert drawing.source_format == "skp_json"
    assert drawing.parser_revision == "skp-json-1.0+rules-model-2026.11"
    assert drawing.dxf_version == "archontos-skp-export/1"
    assert len(drawing.entities) == 5 and len(drawing.objects) == 4
    assert drawing.unmapped_entity_count == 1
    objs = _by_kind(drawing)
    assert objs["door"].properties["width"] == {"value": 0.9, "unit": "m"}
    assert objs["door"].properties["height"] == {"value": 2.1, "unit": "m"}
    assert objs["door"].source_refs == (("ifc_guid", DOOR_GUID), ("skp_persistent_id", "101"))
    assert objs["wall"].properties["length"] == {"value": 6.0, "unit": "m"}
    assert objs["wall"].properties["thickness"] == {"value": 0.2, "unit": "m"}
    assert objs["space"].properties["area"] == {"value": 24.0, "unit": "m2"}
    assert objs["space"].properties["name"] == "거실"
    assert objs["space"].geometry["type"] == "polygon"
    assert objs["space"].fact_units["space.area"] == "m2"
    assert objs["stair"].properties["width"] == {"value": 1.2, "unit": "m"}
    assert objs["stair"].source_handles == ("s-105",)
    assert drawing.model_meta["storeys"] == [{"name": "1F", "elevation_m": 0.0}]
    assert object_refs(drawing)[objs["door"].id] == objs["door"].source_refs


def test_skp_export_is_deterministic_and_unit_independent():
    a, b = parse_skp_json(skp_bytes()), parse_skp_json(skp_bytes())
    assert [o.id for o in a.objects] == [o.id for o in b.objects]
    assert a.content_digest == b.content_digest
    inch = skp_export()
    inch["model"]["units"] = "inch"
    for ent in inch["entities"]:
        ent["bbox"] = {k: [v / 25.4 for v in p] for k, p in ent["bbox"].items()}
        if "footprint" in ent:
            ent["footprint"] = [[v / 25.4 for v in p] for p in ent["footprint"]]
    door = _by_kind(parse_skp_json(json.dumps(inch).encode()))["door"]
    assert door.properties["width"]["value"] == pytest.approx(0.9)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d.update(schema="other/1"), "schema"),
        (lambda d: d["entities"][0].pop("tag"), "tag"),
        (lambda d: d["model"].update(units="furlong"), "units"),
        (lambda d: d["entities"][0].update(ifc_guid="short"), "ifc_guid"),
        (lambda d: d["entities"][1].update(persistent_id=101), "unique"),
        (lambda d: d["entities"][0].update(surprise=1), "(root)|entities"),
    ],
)
def test_skp_export_schema_validation(mutate, message):
    doc = skp_export()
    mutate(doc)
    with pytest.raises(CadParseError, match=message):
        parse_skp_json(json.dumps(doc).encode())


def test_skp_export_rejects_non_json():
    with pytest.raises(CadParseError):
        parse_skp_json(b"{not json")
    with pytest.raises(CadParseError):
        parse_skp_json(b"")


def test_detect_source_format():
    assert detect_source_format(b"ISO-10303-21;\nHEADER;") == "ifc"
    assert detect_source_format(b'  {"schema": "x"}') == "skp_json"
    assert detect_source_format(b"0\nSECTION\n") == "dxf"
    assert detect_source_format(b"0\nSECTION\n", explicit="ifc") == "ifc"
    assert detect_source_format(b"xx", file_name="a.ifc") == "ifc"
    assert detect_source_format(b"xx", content_type="application/json; charset=utf-8") == (
        "skp_json"
    )
    with pytest.raises(UnsupportedModelFormat):
        detect_source_format(b"\xff\xfe\xff\x0eS\x00k\x00e\x00t\x00c\x00h\x00U\x00p\x00 M")
    with pytest.raises(UnsupportedModelFormat):
        detect_source_format(b"xx", file_name="house.skp")
    with pytest.raises(UnsupportedModelFormat):
        detect_source_format(b"xx", explicit="rvt")


def test_building_facts_from_storeys():
    meta = {"storeys": [{"elevation_m": -3.0}, {"elevation_m": 0.0}, {"elevation_m": 3.0}]}
    facts, units = building_facts(meta)
    assert facts == {"building": {"storey_count": 3, "floor_count": 2, "basement_count": 1}}
    assert units["building.floor_count"] == "count"
    assert building_facts({}) == ({}, {})
    assert building_facts(None) == ({}, {})


def test_link_id_is_order_independent():
    assert link_id("a", "b", "manual") == link_id("b", "a", "manual")
    assert link_id("a", "b", "manual") != link_id("a", "b", "shared_ref")


# --- MVP-2 checks / MVP-3 diffs on model revisions ----------------------------------------


def test_checks_fact_space_on_skp_revision():
    space = extract_fact_space(_rows(parse_skp_json(skp_bytes())))
    drawing = space.facts["drawing"]
    assert drawing["door_count"] == 1 and drawing["space_count"] == 1
    assert drawing["door_min_width"] == 0.9
    assert drawing["stair_min_width"] == 1.2
    assert drawing["space_area_total"] == 24.0
    assert space.fact_units["drawing.space_area_total"] == "m2"


def test_diff_on_skp_revisions_matches_by_persistent_id():
    base = _rows(parse_skp_json(skp_bytes()))
    target = _rows(parse_skp_json(skp_bytes(door_width_mm=1000)))
    changes = diff_objects(base, target)
    modified = [c for c in changes if c["change_type"] == "modified"]
    assert [c["kind"] for c in modified] == ["door"]
    (delta,) = [d for d in modified[0]["deltas"] if d.get("property") == "width"]
    assert delta["fact_path"] == "door.width"
    assert sum(c["change_type"] == "unchanged" for c in changes) == 3


# --- IFC (needs the optional bim extra) ---------------------------------------------------


def test_ifc_model_import():
    pytest.importorskip("ifcopenshell")
    from archontos.cad.ifc import parse_ifc
    from tests.model_fixtures import WALL_GUID, tiny_ifc

    drawing = parse_ifc(tiny_ifc())
    assert drawing.source_format == "ifc" and drawing.dxf_version == "IFC4"
    assert drawing.units == "mm"
    objs = _by_kind(drawing)
    assert sorted(objs) == ["door", "slab", "space", "stair", "wall"]
    assert drawing.unmapped_entity_count == 1  # the beam; the IfcStair container is skipped
    assert objs["door"].properties["width"] == {"value": 0.9, "unit": "m"}
    assert objs["door"].source_handles == (DOOR_GUID,)
    assert objs["door"].layer == "1F"
    assert objs["wall"].source_refs == (("ifc_guid", WALL_GUID),)
    assert objs["wall"].properties["length"] == {"value": 6.0, "unit": "m"}
    assert objs["space"].properties["area"] == {"value": 24.0, "unit": "m2"}
    assert objs["space"].properties["name"] == "거실"
    assert objs["space"].geometry["type"] == "polygon"
    xs = [p[0] for p in objs["space"].geometry["coordinates"]]
    assert min(xs) == 0.0 and max(xs) == 6.0
    assert objs["stair"].properties["width"] == {"value": 1.2, "unit": "m"}
    assert objs["slab"].properties["thickness"] == {"value": 0.2, "unit": "m"}
    facts, _ = building_facts(drawing.model_meta)
    assert facts["building"]["floor_count"] == 2 and facts["building"]["basement_count"] == 1
    space = extract_fact_space(_rows(drawing))
    assert space.facts["drawing"]["door_min_width"] == 0.9
    changed = diff_objects(_rows(drawing), _rows(parse_ifc(tiny_ifc(door_width_mm=1000))))
    assert [c["kind"] for c in changed if c["change_type"] == "modified"] == ["door"]
