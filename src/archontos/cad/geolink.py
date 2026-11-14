"""Geometry-based DXF <-> IFC object matching (P2, migration 022).

Pure: input is ``{"id", "kind", "geometry"}`` per object, where ``geometry`` is the stored
``arch_object.geometry_json``. Both parsers already normalise geometry to **metres** (DXF via
``$INSUNITS`` / ``assume_units``, IFC via the project length unit), so no unit conversion is
needed here; :func:`match_geometric` only refuses geometry it cannot read.

Score per candidate pair (same kind only):

* ``iou``: intersection-over-union of the two plan bboxes, each grown by ``tolerance_m / 2``
  so points (door/window inserts) and thin linework get an area;
* ``centroid``: ``max(0, 1 - d / tolerance_m)`` for the bbox-centre distance ``d``;
* ``confidence = 0.6 * iou + 0.4 * centroid``, rounded to 3 decimals.

A pair is a candidate when ``d <= tolerance_m`` or ``iou >= min_iou``, and its confidence is at
least ``min_confidence``. Matching is one-to-one and greedy by confidence (ties by ids), so the
result is deterministic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

#: Kinds that both DXF and IFC produce (``slab`` is IFC-only).
LINKABLE_KINDS = frozenset({"wall", "door", "window", "space", "stair", "column", "opening"})
DEFAULT_TOLERANCE_M = 0.3
DEFAULT_MIN_IOU = 0.5
DEFAULT_MIN_CONFIDENCE = 0.5


@dataclass(frozen=True, slots=True)
class GeometricMatch:
    a_id: str
    b_id: str
    kind: str
    confidence: float
    iou: float
    centroid_distance_m: float


def _points(geometry: Any) -> list[tuple[float, float]]:
    if not isinstance(geometry, dict):
        return []
    coords = geometry.get("coordinates")
    gtype = geometry.get("type")
    try:
        if gtype == "point" and coords is not None:
            return [(float(coords[0]), float(coords[1]))]
        if gtype in ("polygon", "linestring") and coords:
            return [(float(p[0]), float(p[1])) for p in coords]
    except (TypeError, ValueError, IndexError):
        return []
    return []


def plan_bbox(geometry: Any) -> tuple[float, float, float, float] | None:
    """(min_x, min_y, max_x, max_y) in metres, or None for unusable geometry."""
    pts = [p for p in _points(geometry) if math.isfinite(p[0]) and math.isfinite(p[1])]
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _grow(box: tuple[float, float, float, float], pad: float) -> tuple[float, ...]:
    return (box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad)


def _iou(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def score(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
    *,
    tolerance_m: float = DEFAULT_TOLERANCE_M,
) -> tuple[float, float, float]:
    """(confidence, iou, centroid distance in metres) of two plan bboxes."""
    pad = tolerance_m / 2.0
    iou = _iou(_grow(a, pad), _grow(b, pad))
    d = math.hypot((a[0] + a[2] - b[0] - b[2]) / 2.0, (a[1] + a[3] - b[1] - b[3]) / 2.0)
    centroid = max(0.0, 1.0 - d / tolerance_m) if tolerance_m > 0 else 0.0
    return round(0.6 * iou + 0.4 * centroid, 3), round(iou, 4), round(d, 4)


def match_geometric(
    side_a: list[dict[str, Any]],
    side_b: list[dict[str, Any]],
    *,
    tolerance_m: float = DEFAULT_TOLERANCE_M,
    min_iou: float = DEFAULT_MIN_IOU,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    exclude_ids: frozenset[str] | set[str] = frozenset(),
) -> list[GeometricMatch]:
    """One-to-one matches between two object lists (e.g. a DXF and an IFC revision)."""
    if not (tolerance_m > 0 and math.isfinite(tolerance_m)):
        raise ValueError("tolerance_m must be a positive number")

    def usable(
        objs: list[dict[str, Any]],
    ) -> list[tuple[str, str, tuple[float, float, float, float]]]:
        out = []
        for o in objs:
            box = plan_bbox(o.get("geometry"))
            if o["kind"] in LINKABLE_KINDS and box is not None and o["id"] not in exclude_ids:
                out.append((str(o["id"]), str(o["kind"]), box))
        return out

    a_objs, b_objs = usable(side_a), usable(side_b)
    candidates: list[GeometricMatch] = []
    for a_id, kind, a_box in a_objs:
        for b_id, b_kind, b_box in b_objs:
            if kind != b_kind:
                continue
            conf, iou, d = score(a_box, b_box, tolerance_m=tolerance_m)
            if (d <= tolerance_m or iou >= min_iou) and conf >= min_confidence:
                candidates.append(GeometricMatch(a_id, b_id, kind, conf, iou, d))
    candidates.sort(key=lambda m: (-m.confidence, m.centroid_distance_m, m.a_id, m.b_id))
    used_a: set[str] = set()
    used_b: set[str] = set()
    matches = []
    for m in candidates:
        if m.a_id in used_a or m.b_id in used_b:
            continue
        used_a.add(m.a_id)
        used_b.add(m.b_id)
        matches.append(m)
    return sorted(matches, key=lambda m: (m.a_id, m.b_id))
