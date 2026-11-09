import json

import pytest

ezdxf = pytest.importorskip("ezdxf")

from archontos.cad.dxf import DxfError, main, normalize_dxf  # noqa: E402


def _make(path, units=6):
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = units
    doc.layers.add("WALL")
    doc.blocks.new("DOOR")
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (4, 0), (4, 3), (0, 3)], close=True, dxfattribs={"layer": "WALL"})
    msp.add_lwpolyline([(0, 0), (9, 0), (9, 9)], close=False, dxfattribs={"layer": "WALL"})
    msp.add_blockref("DOOR", (1, 1))
    msp.add_blockref("DOOR", (2, 2))
    doc.saveas(path)
    return path


def test_values_and_open_polyline_excluded(tmp_path):
    out = normalize_dxf(_make(tmp_path / "a.dxf"))
    assert out["layers"].count("WALL") == 1
    assert out["block_counts"] == {"DOOR": 2}
    assert len(out["closed_polylines"]) == 1
    p = out["closed_polylines"][0]
    assert (p["area"], p["perimeter"], p["unit"], p["area_unit"]) == (12.0, 14.0, "m", "m^2")
    assert p["locator"]["layer"] == "WALL" and p["locator"]["handle"]
    json.dumps(out)


def test_deterministic(tmp_path):
    f = _make(tmp_path / "a.dxf")
    a, b = normalize_dxf(f), normalize_dxf(f)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert len(a["source_byte_revision_id"]) == 64 and len(a["parser_revision_id"]) == 64


def test_unknown_units_flagged(tmp_path):
    out = normalize_dxf(_make(tmp_path / "u.dxf", units=0))
    assert out["units"]["known"] is False
    p = out["closed_polylines"][0]
    assert p["unit"] == "unknown" and "unit_unknown" in p["flags"]
    assert p["area"] == 12.0  # raw drawing-unit value, never converted


def test_malformed_rejected(tmp_path):
    bad = tmp_path / "bad.dxf"
    bad.write_bytes(b"this is not a dxf\x00\xff")
    with pytest.raises(DxfError):
        normalize_dxf(bad)
    with pytest.raises(DxfError):
        normalize_dxf(tmp_path / "missing.dxf")
    assert main([str(bad)]) == 2


def test_input_unchanged(tmp_path):
    f = _make(tmp_path / "a.dxf")
    before = f.read_bytes()
    mtime = f.stat().st_mtime_ns
    normalize_dxf(f)
    assert main([str(f)]) == 0
    assert f.read_bytes() == before and f.stat().st_mtime_ns == mtime
