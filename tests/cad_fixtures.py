"""Small DXF drawings generated with ezdxf (no binary fixtures in the repo)."""

from __future__ import annotations

import io
from functools import cache

import pytest

ezdxf = pytest.importorskip("ezdxf")


def _bytes(doc) -> bytes:
    stream = io.StringIO()
    doc.write(stream)
    return stream.getvalue().encode("utf-8")


def _door_block(doc, name: str, width: float) -> None:
    block = doc.blocks.new(name=name)
    block.add_line((0, 0), (width, 0))
    block.add_arc((0, 0), width, 0, 90)


@cache
def korean_plan(*, insunits: int = 4, room_name: str = "거실") -> bytes:
    """6000 x 4000 mm room on Korean layers: 벽 walls, 문 door block, 창 window, 실 space."""
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = insunits
    for name in ("벽", "문", "창", "실", "실명", "치수"):
        doc.layers.add(name)
    _door_block(doc, "D900", 900)
    win = doc.blocks.new(name="W1500")
    win.add_lwpolyline([(0, 0), (1500, 0), (1500, 200), (0, 200)], close=True)
    msp = doc.modelspace()
    corners = [(0, 0), (6000, 0), (6000, 4000), (0, 4000)]
    for a, b in zip(corners, corners[1:] + corners[:1], strict=True):
        msp.add_line(a, b, dxfattribs={"layer": "벽"})
    msp.add_blockref("D900", (500, 0), dxfattribs={"layer": "문"})
    msp.add_blockref("W1500", (2000, 4000), dxfattribs={"layer": "창"})
    msp.add_lwpolyline(corners, close=True, dxfattribs={"layer": "실"})
    msp.add_text(room_name, dxfattribs={"layer": "실명", "insert": (3000, 2000)})
    msp.add_line((0, -500), (6000, -500), dxfattribs={"layer": "치수"})  # unmapped
    return _bytes(doc)


@cache
def aia_plan() -> bytes:
    """Metre drawing on AIA layers with a hatch space, a column and a stair."""
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 6
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (10, 0), (10, 0.2)], dxfattribs={"layer": "A-WALL"})
    hatch = msp.add_hatch(dxfattribs={"layer": "A-AREA"})
    hatch.paths.add_polyline_path([(0, 0), (5, 0), (5, 3), (0, 3)], is_closed=True)
    msp.add_lwpolyline(
        [(1, 1), (1.5, 1), (1.5, 1.5), (1, 1.5)], close=True, dxfattribs={"layer": "A-COLS"}
    )
    msp.add_lwpolyline(
        [(6, 0), (8, 0), (8, 4), (6, 4)], close=True, dxfattribs={"layer": "A-FLOR-STRS"}
    )
    msp.add_lwpolyline([(0, 5), (4, 5)], dxfattribs={"layer": "A-AREA"})  # open: not a space
    return _bytes(doc)


@cache
def real_law_plan() -> bytes:
    """Plan for the real-law E2E (mm): a 1.5 m and a 1.0 m corridor plus a 1.2 m wide stair."""
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 4
    for name in ("벽", "실", "실명", "계단"):
        doc.layers.add(name)
    msp = doc.modelspace()
    for y0, width, label in ((0, 1500, "복도-1"), (5000, 1000, "복도-2")):
        ring = [(0, y0), (12000, y0), (12000, y0 + width), (0, y0 + width)]
        msp.add_lwpolyline(ring, close=True, dxfattribs={"layer": "실"})
        msp.add_text(label, dxfattribs={"layer": "실명", "insert": (6000, y0 + width / 2)})
    stair = [(14000, 0), (15200, 0), (15200, 3000), (14000, 3000)]
    msp.add_lwpolyline(stair, close=True, dxfattribs={"layer": "계단"})
    return _bytes(doc)


@cache
def revision_plan(
    *,
    door_x: int = 500,
    corridor_width: int = 1100,
    fourth_wall: bool = True,
    extra_space: bool = False,
    shift_handles: bool = False,
    note: str = "",
    split_bottom_wall: bool = False,
) -> bytes:
    """Permit/construction pair builder (mm).

    Optional entities come last so the DXF handles of the always-present ones stay stable
    between variants; ``shift_handles`` deliberately breaks that.
    """
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 4
    for name in ("벽", "문", "실", "실명", "치수"):
        doc.layers.add(name)
    _door_block(doc, "D900", 900)
    msp = doc.modelspace()
    if shift_handles:
        msp.add_line((0, -900), (10, -900), dxfattribs={"layer": "치수"})
    corners = [(0, 0), (6000, 0), (6000, 4000), (0, 4000)]
    walls = list(zip(corners, corners[1:] + corners[:1], strict=True))
    for i, (a, b) in enumerate(walls[:3]):
        # split_bottom_wall: the bottom wall keeps its handle for its first half (BREAK)
        msp.add_line(
            a, (3000, 0) if i == 0 and split_bottom_wall else b, dxfattribs={"layer": "벽"}
        )
    msp.add_blockref("D900", (door_x, 0), dxfattribs={"layer": "문"})
    msp.add_lwpolyline(corners, close=True, dxfattribs={"layer": "실"})
    msp.add_text("거실", dxfattribs={"layer": "실명", "insert": (3000, 2000)})
    corridor = [(8000, 0), (16000, 0), (16000, corridor_width), (8000, corridor_width)]
    msp.add_lwpolyline(corridor, close=True, dxfattribs={"layer": "실"})
    msp.add_text("복도", dxfattribs={"layer": "실명", "insert": (12000, 300)})
    if fourth_wall:
        msp.add_line(*walls[3], dxfattribs={"layer": "벽"})
    if extra_space:
        box = [(20000, 0), (23000, 0), (23000, 2000), (20000, 2000)]
        msp.add_lwpolyline(box, close=True, dxfattribs={"layer": "실"})
        msp.add_text("창고", dxfattribs={"layer": "실명", "insert": (21500, 1000)})
    if split_bottom_wall:
        msp.add_line((3000, 0), (6000, 0), dxfattribs={"layer": "벽"})
    if note:
        msp.add_line((0, -500), (6000 + len(note), -500), dxfattribs={"layer": "치수"})
    return _bytes(doc)
