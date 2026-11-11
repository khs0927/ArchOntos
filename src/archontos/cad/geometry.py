"""Small, deterministic plan geometry helpers (pure Python, metres, no shapely).

Used by object pairing (``archontos.cad.pairing``), effective widths (``archontos.cad.checks``)
and wall split/merge detection (``archontos.cad.diff``). Every function is a pure function of
its inputs; results are rounded to 9 decimals so equal inputs give byte-equal JSON.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

Point = tuple[float, float]
Segment = tuple[Point, Point]

#: Two directions are parallel when the angle between them is at most this (degrees).
PARALLEL_DEG = 1.0
_EPS = 1e-9


def points_of(geometry: Any) -> list[Point]:
    """Coordinates of a ``point`` / ``linestring`` / ``polygon`` geometry JSON (metres)."""
    if not isinstance(geometry, dict):
        return []
    coords = geometry.get("coordinates")
    if not isinstance(coords, list) or not coords:
        return []
    try:
        if isinstance(coords[0], (int, float)):
            return [(float(coords[0]), float(coords[1]))]
        return [(float(p[0]), float(p[1])) for p in coords]
    except (TypeError, ValueError, IndexError):
        return []


def location(geometry: Any) -> Point | None:
    """The plan location of a point geometry (doors, windows, IFC elements)."""
    if isinstance(geometry, dict) and geometry.get("type") == "point":
        pts = points_of(geometry)
        return pts[0] if pts else None
    return None


def polygon(geometry: Any) -> list[Point] | None:
    """Ring of a polygon geometry without a repeated closing vertex (>= 3 vertices)."""
    if not isinstance(geometry, dict) or geometry.get("type") != "polygon":
        return None
    pts = points_of(geometry)
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    ring = [p for i, p in enumerate(pts) if i == 0 or math.dist(p, pts[i - 1]) > _EPS]
    if len(ring) > 1 and math.dist(ring[0], ring[-1]) <= _EPS:
        ring.pop()
    return ring if len(ring) >= 3 else None


def segments(geometry: Any) -> list[Segment]:
    """Straight segments of a linestring (wall centre/face lines) or of a polygon ring."""
    if not isinstance(geometry, dict):
        return []
    if geometry.get("type") == "polygon":
        ring = polygon(geometry)
        return list(zip(ring, ring[1:] + ring[:1], strict=True)) if ring else []
    if geometry.get("type") != "linestring":
        return []
    pts = points_of(geometry)
    if geometry.get("closed") and len(pts) > 2:
        pts = [*pts, pts[0]]
    return [(a, b) for a, b in zip(pts, pts[1:], strict=False) if math.dist(a, b) > _EPS]


def contains(ring: Sequence[Point], point: Point) -> bool:
    """Even-odd point-in-polygon test (boundary points may go either way)."""
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, [*ring[1:], ring[0]], strict=True):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def point_segment_distance(point: Point, seg: Segment) -> float:
    (ax, ay), (bx, by) = seg
    px, py = point
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 <= _EPS:
        return math.dist(point, seg[0])
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def distance_to_ring(ring: Sequence[Point], point: Point) -> float:
    return min(
        point_segment_distance(point, (a, b))
        for a, b in zip(ring, [*ring[1:], ring[0]], strict=True)
    )


def _unit(seg: Segment) -> tuple[float, float, float]:
    (ax, ay), (bx, by) = seg
    length = math.hypot(bx - ax, by - ay)
    return (bx - ax) / length, (by - ay) / length, length


def parallel(a: Segment, b: Segment, max_deg: float = PARALLEL_DEG) -> bool:
    ux, uy, _ = _unit(a)
    vx, vy, _ = _unit(b)
    return abs(ux * vy - uy * vx) <= math.sin(math.radians(max_deg))


def offset_and_overlap(a: Segment, b: Segment) -> tuple[float, float, float, float]:
    """For (near-)parallel segments: (perpendicular offset of ``b`` from ``a``'s line,
    overlap start, overlap end, overlap length) measured along ``a`` from its start."""
    ux, uy, length = _unit(a)
    (ax, ay), _ = a
    ts = [(px - ax) * ux + (py - ay) * uy for px, py in b]
    offs = [abs((px - ax) * uy - (py - ay) * ux) for px, py in b]
    lo, hi = max(0.0, min(ts)), min(length, max(ts))
    return sum(offs) / 2.0, lo, hi, max(0.0, hi - lo)


def clear_width(ring: Sequence[Point], *, min_overlap: float = 0.01) -> float | None:
    """Minimum clear width of a plan polygon from opposite parallel faces.

    For every pair of antiparallel edges whose projections overlap by more than
    ``min_overlap`` and that face each other across the inside of the polygon, the
    perpendicular distance is a clear width; the minimum is returned. A rotated rectangle gives
    its short side, an L-shaped corridor the narrower arm. ``None`` when no such pair exists
    (triangles, curved outlines approximated by few vertices), so callers fall back.
    """
    if len(ring) < 4:
        return None
    edges = list(zip(ring, [*ring[1:], ring[0]], strict=True))
    best: float | None = None
    for i, a in enumerate(edges):
        ux, uy, _ = _unit(a)
        for b in edges[i + 1 :]:
            vx, vy, _ = _unit(b)
            if ux * vx + uy * vy >= 0 or not parallel(a, b):
                continue
            dist, lo, hi, overlap = offset_and_overlap(a, b)
            if overlap <= min_overlap or dist <= _EPS:
                continue
            tm = (lo + hi) / 2
            px, py = a[0][0] + ux * tm, a[0][1] + uy * tm
            # unit normal of a pointing towards b
            nx, ny = -uy, ux
            bx, by = b[0]
            if (bx - px) * nx + (by - py) * ny < 0:
                nx, ny = -nx, -ny
            mid = (px + nx * dist / 2, py + ny * dist / 2)
            if not contains(ring, mid):
                continue
            if best is None or dist < best:
                best = dist
    return None if best is None else round(best, 9)


def straight_axis(geometry: Any, *, max_deg: float = PARALLEL_DEG) -> Segment | None:
    """The single straight axis of an open linestring whose vertices are collinear."""
    if not isinstance(geometry, dict) or geometry.get("type") != "linestring":
        return None
    if geometry.get("closed"):
        return None
    segs = segments(geometry)
    if not segs:
        return None
    first = segs[0]
    if any(not parallel(first, s, max_deg) for s in segs[1:]):
        return None
    pts = points_of(geometry)
    ux, uy, _ = _unit(first)
    ax, ay = first[0]
    if any(abs((px - ax) * uy - (py - ay) * ux) > 1e-3 for px, py in pts):
        return None
    ordered = sorted(pts, key=lambda p: (p[0] - ax) * ux + (p[1] - ay) * uy)
    return ordered[0], ordered[-1]
