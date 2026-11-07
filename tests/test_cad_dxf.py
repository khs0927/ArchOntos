import pytest

from archontos.cad.dxf import CadParseError, parse_dxf, parser_revision, subject_ref
from archontos.cad.rules import (
    DEFAULT_RULE_SET,
    MappingRule,
    MappingRuleError,
    MappingRuleSet,
    rule_set_from_dict,
)
from archontos.domain.contracts import AecSubjectRef
from archontos.integration.ontology import load_facts_export
from tests.cad_fixtures import aia_plan, korean_plan


def _by_kind(drawing):
    out: dict[str, list] = {}
    for obj in drawing.objects:
        out.setdefault(obj.kind, []).append(obj)
    return out


def test_korean_plan_maps_walls_doors_windows_and_named_space():
    drawing = parse_dxf(korean_plan())
    kinds = _by_kind(drawing)
    assert len(kinds["wall"]) == 4
    assert sorted(w.properties["length"]["value"] for w in kinds["wall"]) == [4.0, 4.0, 6.0, 6.0]
    (space,) = kinds["space"]
    assert space.properties["area"] == {"value": 24.0, "unit": "m2"}
    assert space.properties["perimeter"] == {"value": 20.0, "unit": "m"}
    assert space.properties["name"] == "거실"
    assert space.fact_units == {"space.area": "m2", "space.perimeter": "m"}
    assert space.facts() == {"space": {"area": 24.0, "perimeter": 20.0}}
    assert len(space.source_handles) == 2  # polygon + label text
    (door,) = kinds["door"]
    assert door.properties["width"] == {"value": 0.9, "unit": "m"}
    assert door.rule_id == "door-layer"
    (window,) = kinds["window"]
    assert window.properties["width"]["value"] == 1.5
    assert drawing.units == "mm" and not drawing.units_assumed
    assert drawing.unmapped_entity_count == 1  # the 치수 line


def test_parse_is_deterministic():
    a, b = parse_dxf(korean_plan()), parse_dxf(korean_plan())
    assert a.sha256 == b.sha256
    assert a.content_digest == b.content_digest
    assert [o.id for o in a.objects] == [o.id for o in b.objects]
    assert [e.id for e in a.entities] == [e.id for e in b.entities]
    other = parse_dxf(korean_plan(room_name="침실"))
    assert other.content_digest != a.content_digest


def test_aia_layers_hatch_space_column_stair_and_open_polyline():
    drawing = parse_dxf(aia_plan())
    kinds = _by_kind(drawing)
    assert kinds["space"][0].properties["area"]["value"] == 15.0
    assert len(kinds["space"]) == 1  # the open A-AREA polyline is not a space
    assert kinds["column"][0].properties["width"]["value"] == 0.5
    assert kinds["stair"][0].properties["area"]["value"] == 8.0
    assert kinds["wall"][0].properties["length"]["value"] == pytest.approx(10.2)


def test_units_assumed_when_insunits_missing():
    drawing = parse_dxf(korean_plan(insunits=0), assume_units="mm")
    assert drawing.units_assumed
    assert _by_kind(drawing)["space"][0].properties["area"]["value"] == 24.0
    with pytest.raises(CadParseError):
        parse_dxf(korean_plan(), assume_units="furlong")


def test_garbage_is_rejected():
    with pytest.raises(CadParseError):
        parse_dxf(b"")
    with pytest.raises(CadParseError):
        parse_dxf(b"not a dxf at all")


def test_rule_set_version_changes_parser_revision_and_object_ids():
    custom = rule_set_from_dict({**DEFAULT_RULE_SET.as_dict(), "version": "custom-1"})
    assert parser_revision(custom) != parser_revision()
    a, b = parse_dxf(korean_plan()), parse_dxf(korean_plan(), rule_set=custom)
    assert {o.id for o in a.objects}.isdisjoint({o.id for o in b.objects})
    assert [e.id for e in a.entities] == [e.id for e in b.entities]


def test_invalid_rules_are_refused():
    with pytest.raises(MappingRuleError):
        MappingRule("x", "roof", frozenset({"LINE"}), layer_pattern="R")
    with pytest.raises(MappingRuleError):
        MappingRule("x", "wall", frozenset({"LINE"}))
    with pytest.raises(MappingRuleError):
        MappingRuleSet("bad version!", ())
    with pytest.raises(MappingRuleError):
        rule_set_from_dict({"version": "v"})


@pytest.mark.parametrize(
    ("layer", "kind"),
    [("벽", "wall"), ("벽체", "wall"), ("A-WALL", "wall"), ("A-WALL-PATT", None), ("문자", None)],
)
def test_layer_conventions(layer, kind):
    rule = DEFAULT_RULE_SET.match(entity_type="LINE", layer=layer, block_name=None, closed=False)
    assert (rule.kind if rule else None) == kind


def test_subject_ref_and_facts_contract():
    drawing = parse_dxf(korean_plan())
    obj = drawing.objects[0]
    ref = subject_ref(obj, project_id="P1", source_key="A-101", drawing=drawing, revision_id="r")
    parsed = AecSubjectRef.model_validate(ref)
    assert parsed.source_byte_revision_id == drawing.sha256
    assert parsed.parser_revision_id and parsed.source_id

    from archontos.cad.persistence import facts_export

    objects = [
        {"kind": o.kind, "properties": o.properties, "subject_ref": ref} for o in drawing.objects
    ]
    export = facts_export(
        {"id": "r", "project_id": "P1", "parser_revision": drawing.parser_revision}, objects
    )
    loaded = load_facts_export(export)
    assert loaded["facts"]["drawing"]["space_area_total"] == 24.0
    assert loaded["facts"]["fact_units"]["drawing.space_area_total"] == "m2"
