"""Marker plan, power-cad payload and DXF writer (pure, no network)."""

from __future__ import annotations

import io
import math
from typing import Any

import pytest

from archontos.cad.markers import (
    MARKER_LAYER,
    IssueMarker,
    MarkerError,
    apply_markers_to_dxf,
    build_marker_plan,
    to_power_cad_payload,
)
from tests.cad_fixtures import korean_plan


def _issues() -> list[IssueMarker]:
    return [
        IssueMarker(
            issue_id="b-2",
            outcome="REVIEW",
            rule_label="KR-DOOR-WIDTH",
            text="door width unclear",
            point=(500.0, 0.0),
        ),
        IssueMarker(
            issue_id="a-1",
            outcome="FAIL",
            rule_label="KR-CORRIDOR",
            text="corridor 1.1 m < 1.2 m",
            entity_handle="2A",
            bbox=(0.0, 0.0, 6000.0, 4000.0),
        ),
    ]


def test_plan_is_deterministic_and_sorted() -> None:
    a = build_marker_plan(_issues())
    b = build_marker_plan(list(reversed(_issues())))
    assert a == b
    assert len(a["sha256"]) == 64
    assert [m["issue_id"] for m in a["markers"]] == ["a-1", "b-2"]
    changed = build_marker_plan(_issues(), text_height=300.0)
    assert changed["sha256"] != a["sha256"]
    assert a["layer"] == MARKER_LAYER
    assert a["units"] == "mm"
    assert a["text_height"] == 250.0


def test_colors_and_label() -> None:
    markers = {m["issue_id"]: m for m in build_marker_plan(_issues())["markers"]}
    assert markers["a-1"]["color"] == 1
    assert markers["b-2"]["color"] == 2
    assert markers["a-1"]["mtext"]["text"] == "[FAIL] KR-CORRIDOR: corridor 1.1 m < 1.2 m"
    assert markers["a-1"]["circle"]["color"] == 1
    assert markers["a-1"]["entity_handle"] == "2A"


def test_centroid_and_radius() -> None:
    markers = {m["issue_id"]: m for m in build_marker_plan(_issues())["markers"]}
    bbox_marker = markers["a-1"]
    assert bbox_marker["anchor"] == [3000.0, 2000.0]
    assert bbox_marker["circle"]["center"] == [3000.0, 2000.0]
    half_diag = math.hypot(6000.0, 4000.0) / 2.0
    assert bbox_marker["circle"]["radius"] == pytest.approx(half_diag * 1.1, abs=1e-6)
    point_marker = markers["b-2"]
    assert point_marker["anchor"] == [500.0, 0.0]
    assert point_marker["circle"]["radius"] == 500.0  # 2 text heights minimum
    leader = point_marker["leader"]
    assert leader["start"][0] > 500.0 and leader["end"][0] > leader["start"][0]


def test_input_validation() -> None:
    with pytest.raises(MarkerError):
        build_marker_plan([IssueMarker("x", "FAIL", "r", "t")])  # no geometry
    with pytest.raises(MarkerError):
        build_marker_plan([_issues()[0], _issues()[0]])  # duplicate id
    with pytest.raises(MarkerError):
        build_marker_plan(
            [
                IssueMarker("x", "FAIL", "r", "t", point=(0, 0), units="mm"),
                IssueMarker("y", "FAIL", "r", "t", point=(0, 0), units="m"),
            ]
        )
    with pytest.raises(MarkerError):
        build_marker_plan([IssueMarker("x", "PASS", "r", "t", point=(0, 0))])  # type: ignore[arg-type]
    assert build_marker_plan([])["markers"] == []


def test_power_cad_payload_shape() -> None:
    plan = build_marker_plan(_issues())
    payload = to_power_cad_payload(plan)
    assert payload["layers"] == [{"name": MARKER_LAYER, "color": 7}]
    assert payload["xdata_app"] == "ARCHONTOS"
    assert "items" not in payload
    items = payload["entities"]
    assert [i["type"] for i in items] == ["circle", "line", "mtext"] * 2
    assert all(i["layer"] == MARKER_LAYER for i in items)
    assert items[0]["color"] == 1 and items[3]["color"] == 2
    assert items[2]["text"] == "[FAIL] KR-CORRIDOR: corridor 1.1 m < 1.2 m"
    assert items[2]["justify"] == "BL" and "position" in items[2] and "insert" not in items[2]
    assert items[0]["hs"] == {"issue_id": "a-1", "outcome": "FAIL"}


def test_power_cad_payload_matches_create_many_contract() -> None:
    """Contract test against a vendored minimal snapshot of power-cad's ``cad_create_many``.

    Source: power-cad ``dotnet/PowerCad.Server/CadTools.cs`` (``CreateMany``) and
    ``dotnet/PowerCad.Core/Commands/CommandDispatcher.Bulk.cs`` / ``CreateSpec.cs`` at commit
    845f63a4114e6dc43a48cd8b9bdaf007d8f5fb63. The snapshot (``tests/contracts/``) is
    ``additionalProperties: false`` wherever power-cad uses ``AllowOnly``, so a renamed or
    stray key fails here instead of at AutoCAD.
    """
    import json
    from pathlib import Path

    import jsonschema

    path = Path(__file__).parent / "contracts" / "power_cad_create_many.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema)
    payload = to_power_cad_payload(build_marker_plan(_issues()))
    assert list(validator.iter_errors(payload)) == []
    # The earlier (unverified) shape must be rejected by the snapshot.
    old = {"layers": payload["layers"], "items": payload["entities"]}
    assert list(validator.iter_errors(old))
    bad = json.loads(json.dumps(payload))
    bad["entities"][2]["insert"] = bad["entities"][2].pop("position")
    assert list(validator.iter_errors(bad))
    # Every hs tag fits power-cad's 255-character XData string limit.
    for e in payload["entities"]:
        assert all(len(f"{k}={v}") <= 255 for k, v in e["hs"].items())


def test_apply_markers_to_dxf_readback() -> None:
    import ezdxf

    original = korean_plan()
    before = bytes(original)
    plan = build_marker_plan(_issues())
    out = apply_markers_to_dxf(original, plan)

    assert original == before  # input untouched
    assert out != original
    old_doc = ezdxf.read(io.StringIO(original.decode("utf-8")))
    new_doc = ezdxf.read(io.StringIO(out.decode("utf-8")))
    assert MARKER_LAYER not in old_doc.layers
    assert MARKER_LAYER in new_doc.layers

    on_layer = list(new_doc.modelspace().query(f'*[layer=="{MARKER_LAYER}"]'))
    assert sorted(e.dxftype() for e in on_layer) == ["CIRCLE", "CIRCLE", "LINE", "LINE"] + [
        "MTEXT",
        "MTEXT",
    ]
    assert len(new_doc.modelspace()) == len(old_doc.modelspace()) + 6
    others = [e for e in new_doc.modelspace() if e.dxf.layer != MARKER_LAYER]
    assert len(others) == len(old_doc.modelspace())
    texts = sorted(e.text for e in on_layer if e.dxftype() == "MTEXT")
    assert texts[0].startswith("[FAIL] KR-CORRIDOR")
    reds = {e.dxf.color for e in on_layer if e.dxftype() == "CIRCLE"}
    assert reds == {1, 2}


def test_apply_markers_is_byte_deterministic() -> None:
    plan = build_marker_plan(_issues())
    assert apply_markers_to_dxf(korean_plan(), plan) == apply_markers_to_dxf(korean_plan(), plan)


def test_apply_markers_reuses_existing_layer_and_escapes_text() -> None:
    import ezdxf

    plan = build_marker_plan(
        [IssueMarker("e", "FAIL", "R", r"{a}\b", point=(1.0, 1.0), units="mm")]
    )
    once = apply_markers_to_dxf(korean_plan(), plan)
    twice = apply_markers_to_dxf(once, plan)
    doc = ezdxf.read(io.StringIO(twice.decode("utf-8")))
    assert sum(1 for layer in doc.layers if layer.dxf.name == MARKER_LAYER) == 1
    mtext: Any = next(e for e in doc.modelspace() if e.dxftype() == "MTEXT")
    assert mtext.plain_text() == r"[FAIL] R: {a}\b"
