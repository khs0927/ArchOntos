"""P2: building / storey facts from DXF title blocks (fail-closed)."""

from __future__ import annotations

import io

import pytest

from archontos.cad.building_info import (
    DEFAULT_BUILDING_RULES,
    BuildingRuleError,
    building_info_facts,
    building_rule_set_from_dict,
)
from archontos.cad.dxf import parse_dxf
from archontos.cad.model import building_facts
from tests.cad_fixtures import korean_plan

ezdxf = pytest.importorskip("ezdxf")


def _plan(*, attribs: dict[str, str] | None = None, texts: list[tuple[str, str]] = ()) -> bytes:
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 4
    doc.layers.add("도곽")
    block = doc.blocks.new(name="TITLE")
    block.add_lwpolyline([(0, 0), (1000, 0), (1000, 500), (0, 500)], close=True)
    for i, tag in enumerate(attribs or {}):
        block.add_attdef(tag, (10, 10 + i * 50))
    msp = doc.modelspace()
    msp.add_line((0, 0), (6000, 0), dxfattribs={"layer": "벽"})
    if attribs:
        ref = msp.add_blockref("TITLE", (10000, 0), dxfattribs={"layer": "도곽"})
        ref.add_auto_attribs(attribs)
    for layer, content in texts:
        msp.add_text(content, dxfattribs={"layer": layer, "insert": (9000, 100)})
    stream = io.StringIO()
    doc.write(stream)
    return stream.getvalue().encode("utf-8")


def test_attributes_give_use_group_floor_and_storey_label() -> None:
    drawing = parse_dxf(
        _plan(
            attribs={
                "주용도": "제2종 근린생활시설",
                "층수": "지상 5층",
                "지하층수": "1",
                "LEVEL": "1 층",
            }
        )
    )
    info = drawing.model_meta["building_info"]
    assert info["rule_set_version"] == DEFAULT_BUILDING_RULES.version
    assert info["values"] == {
        "use_group": "제2종 근린생활시설",
        "floor_count": 5,
        "basement_count": 1,
        "storey_label": "1층",
    }
    assert info["conflicts"] == []
    facts, units = building_facts(drawing.model_meta)
    assert facts["building"] == {
        "use_group": "제2종 근린생활시설",
        "floor_count": 5,
        "basement_count": 1,
    }
    assert facts["sheet"] == {"storey_label": "1층"}
    assert units == {"building.floor_count": "count", "building.basement_count": "count"}


def test_title_block_text_on_configured_layer() -> None:
    drawing = parse_dxf(
        _plan(
            texts=[
                ("도곽", "주용도: 업무시설"),
                ("도곽", "지하 2층 / 지상 12층"),
                ("도곽", "3층 평면도"),
            ]
        )
    )
    values = drawing.model_meta["building_info"]["values"]
    assert values == {
        "use_group": "업무시설",
        "floor_count": 12,
        "basement_count": 2,
        "storey_label": "3층",
    }


def test_text_on_other_layers_is_ignored_and_missing_means_no_fact() -> None:
    drawing = parse_dxf(_plan(texts=[("치수", "지상 5층")]))
    assert drawing.model_meta == {}
    assert building_facts(drawing.model_meta) == ({}, {})


def test_conflicting_sources_fail_closed() -> None:
    drawing = parse_dxf(
        _plan(attribs={"층수": "5"}, texts=[("도곽", "지상 7층"), ("도곽", "주용도: 판매시설")])
    )
    info = drawing.model_meta["building_info"]
    assert "floor_count" not in info["values"]
    assert info["conflicts"] == [{"field": "floor_count", "values": ["5", "7"]}]
    facts, units = building_info_facts(info)
    assert facts == {"building": {"use_group": "판매시설"}}
    assert units == {}


def test_plain_drawing_digest_is_unchanged_by_the_feature() -> None:
    a = parse_dxf(korean_plan())
    assert a.model_meta == {}
    b = parse_dxf(_plan(texts=[("도곽", "지상 3층")]))
    c = parse_dxf(_plan(texts=[("도곽", "지상 4층")]))
    assert b.content_digest != c.content_digest


def test_rule_set_round_trip_and_validation() -> None:
    again = building_rule_set_from_dict(DEFAULT_BUILDING_RULES.as_dict())
    assert again.fingerprint() == DEFAULT_BUILDING_RULES.fingerprint()
    with pytest.raises(BuildingRuleError):
        building_rule_set_from_dict(
            {"version": "x", "text_layer_pattern": "A", "text_patterns": {"use_group": "no-group"}}
        )
    with pytest.raises(BuildingRuleError):
        building_rule_set_from_dict(
            {"version": "x", "text_layer_pattern": "(", "attribute_tags": {}}
        )
    custom = building_rule_set_from_dict(
        {
            "version": "office-1",
            "text_layer_pattern": "^NOTE$",
            "text_patterns": {"floor_count": r"FLOORS=(?P<v>\d+)"},
        }
    )
    drawing = parse_dxf(_plan(texts=[("NOTE", "FLOORS=9")]), building_rules=custom)
    assert drawing.model_meta["building_info"]["values"] == {"floor_count": 9}
    assert drawing.model_meta["building_info"]["rule_set_version"] == "office-1"
