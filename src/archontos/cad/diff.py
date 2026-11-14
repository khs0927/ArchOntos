"""MVP-3: deterministic diff between two CAD revisions (pure, no I/O).

``diff_objects`` pairs the ``arch_object`` rows of a base and a target revision and classifies
every object as ``added``, ``removed``, ``modified`` or ``unchanged``:

1. Handle matching: the same kind and the same DXF source handles. A handle match is refused
   when the objects are farther apart than ``handle_max_shift`` and do not overlap (handles
   reassigned by another tool).
2. Geometric matching for the rest, within one kind: centroid distance <= ``match_centroid``
   or bbox IoU >= ``match_min_iou``, assigned greedily, best score first, ties by id.

Before handle and geometric matching, straight walls are tested for a **split** (one base wall
replaced by N >= 2 collinear target walls that together cover it) and a **merge** (N >= 2
collinear base walls replaced by one target wall that covers them). Such groups become
``split`` / ``merged`` changes with lineage links instead of ``removed`` + ``added``.

Matched pairs get per-property deltas. Numeric deltas carry the unit (``fact_units``); a delta
within its tolerance is not a change. ``attach_impact`` then links changes to check results
and ``transitions`` compares check outcomes between the two revisions.
"""

from __future__ import annotations

import json
import math
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from archontos.cad import geometry as geo

DIFFER_VERSION = "cad-diff-1.1"  # 1.1: wall split / merge with lineage
CHANGE_TYPES = ("added", "removed", "modified", "unchanged", "split", "merged")
_ORDER = {
    t: i for i, t in enumerate(("removed", "split", "merged", "modified", "added", "unchanged"))
}
_RANK = {"PASS": 0, "REVIEW": 1, "FAIL": 2}
_BOX_PAD = 0.01  # metres: gives line-like objects (walls) a non-zero IoU footprint
_MAX_CELLS = 256


class DiffInputError(ValueError):
    """Tolerances or revisions cannot be diffed (HTTP 422)."""


def _tolerance(name: str, value: Any, upper: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise DiffInputError(f"tolerance {name} must be a finite number")
    if value < 0 or (upper is not None and value > upper):
        raise DiffInputError(f"tolerance {name} is out of range")
    return float(value)


_FLOAT_KEYS = (
    "numeric_abs",
    "numeric_rel",
    "position",
    "match_centroid",
    "match_min_iou",
    "handle_max_shift",
    "split_offset",
    "split_max_gap",
)


@dataclass(frozen=True, slots=True)
class DiffTolerances:
    numeric_abs: float = 0.001  # property's own unit (m, m2, deg)
    numeric_rel: float = 0.0
    position: float = 0.001  # metres: centroid shift / bbox size change
    match_centroid: float = 0.5  # metres
    match_min_iou: float = 0.5
    handle_max_shift: float = 10.0  # metres
    split_offset: float = 0.01  # metres: max distance of a piece from the whole wall's axis
    split_max_gap: float = 0.05  # metres: max uncovered gap at the ends / between pieces
    match_by_handle: bool = True
    detect_split: bool = True
    per_property: dict[str, float] = field(default_factory=dict)  # "door.width" or "width"

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> DiffTolerances:
        data = dict(data or {})
        unknown = set(data) - {*_FLOAT_KEYS, "match_by_handle", "detect_split", "per_property"}
        if unknown:
            raise DiffInputError(f"unknown tolerance(s): {sorted(unknown)}")
        kwargs: dict[str, Any] = {
            key: _tolerance(key, data[key], upper=1.0 if key == "match_min_iou" else None)
            for key in _FLOAT_KEYS
            if key in data
        }
        if "match_by_handle" in data:
            if not isinstance(data["match_by_handle"], bool):
                raise DiffInputError("match_by_handle must be a boolean")
            kwargs["match_by_handle"] = data["match_by_handle"]
        if "detect_split" in data:
            if not isinstance(data["detect_split"], bool):
                raise DiffInputError("detect_split must be a boolean")
            kwargs["detect_split"] = data["detect_split"]
        per = data.get("per_property") or {}
        if not isinstance(per, Mapping):
            raise DiffInputError("per_property must be an object")
        kwargs["per_property"] = {
            str(k): _tolerance(f"per_property.{k}", v) for k, v in sorted(per.items())
        }
        return cls(**kwargs)

    def as_dict(self) -> dict[str, Any]:
        return {
            **{k: getattr(self, k) for k in _FLOAT_KEYS},
            "match_by_handle": self.match_by_handle,
            "detect_split": self.detect_split,
            "per_property": dict(sorted(self.per_property.items())),
        }

    def numeric_tolerance(self, kind: str, name: str, a: float, b: float) -> float:
        specific = self.per_property.get(f"{kind}.{name}", self.per_property.get(name))
        base = self.numeric_abs if specific is None else specific
        return max(base, self.numeric_rel * max(abs(a), abs(b)))


# --- geometry ---------------------------------------------------------------------------

Box = tuple[float, float, float, float]


def _points(geometry: Any) -> list[tuple[float, float]]:
    if not isinstance(geometry, dict):
        return []
    coords = geometry.get("coordinates")
    if not isinstance(coords, list) or not coords:
        return []
    if isinstance(coords[0], (int, float)):
        return [(float(coords[0]), float(coords[1]))]
    return [(float(p[0]), float(p[1])) for p in coords]


@dataclass(frozen=True, slots=True)
class _Shape:
    box: Box | None

    @property
    def centroid(self) -> tuple[float, float] | None:
        if self.box is None:
            return None
        return ((self.box[0] + self.box[2]) / 2, (self.box[1] + self.box[3]) / 2)


def _shape(obj: Mapping[str, Any]) -> _Shape:
    pts = _points(obj.get("geometry"))
    if not pts:
        return _Shape(None)
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return _Shape((min(xs), min(ys), max(xs), max(ys)))


def _padded(box: Box, pad: float) -> Box:
    return (box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad)


def _iou(a: _Shape, b: _Shape) -> float:
    if a.box is None or b.box is None:
        return 0.0
    x0, y0, x1, y1 = _padded(a.box, _BOX_PAD)
    u0, v0, u1, v1 = _padded(b.box, _BOX_PAD)
    iw, ih = min(x1, u1) - max(x0, u0), min(y1, v1) - max(y0, v0)
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    union = (x1 - x0) * (y1 - y0) + (u1 - u0) * (v1 - v0) - inter
    return inter / union if union > 0 else 0.0


def _dist(a: _Shape, b: _Shape) -> float:
    ca, cb = a.centroid, b.centroid
    if ca is None or cb is None:
        return math.inf
    return math.hypot(ca[0] - cb[0], ca[1] - cb[1])


# --- matching ---------------------------------------------------------------------------

Pairs = dict[str, tuple[str, float]]


def _handle_key(obj: Mapping[str, Any]) -> tuple[str, tuple[str, ...]] | None:
    handles = tuple(obj.get("source_handles") or ())
    return (obj["kind"], handles) if handles else None


def _signature(obj: Mapping[str, Any]) -> str:
    """Content identity that ignores handles and ids (layer, properties, geometry)."""
    body = (obj.get("layer"), obj.get("properties"), obj.get("geometry"))
    return json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)


def _match_by_content(base: list[dict[str, Any]], target: list[dict[str, Any]]) -> Pairs:
    """Pair objects whose content is identical, preferring the same DXF handles.

    Runs before handle matching so that reassigned handles cannot pair an object with a
    different, merely look-alike one.
    """
    groups: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = defaultdict(
        lambda: ([], [])
    )
    for obj in base:
        groups[_signature(obj)][0].append(obj)
    for obj in target:
        groups[_signature(obj)][1].append(obj)
    pairs: Pairs = {}
    for bs, ts in groups.values():
        free = list(ts)
        rest = []
        for b in bs:  # same handles first
            hit = next(
                (t for t in free if t.get("source_handles") == b.get("source_handles")), None
            )
            if hit is None:
                rest.append(b)
            else:
                free.remove(hit)
                pairs[b["id"]] = (hit["id"], 1.0)
        for b, t in zip(rest, free, strict=False):
            pairs[b["id"]] = (t["id"], 1.0)
    return pairs


def _match_by_handle(
    base: list[dict[str, Any]],
    target: list[dict[str, Any]],
    shapes: dict[str, _Shape],
    tol: DiffTolerances,
) -> Pairs:
    index: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for obj in target:
        key = _handle_key(obj)
        if key is not None:
            index[key].append(obj)
    base_count: dict[Any, int] = defaultdict(int)
    for obj in base:
        key = _handle_key(obj)
        if key is not None:
            base_count[key] += 1
    pairs: Pairs = {}
    for obj in base:
        key = _handle_key(obj)
        if key is None or base_count[key] != 1 or len(index.get(key, ())) != 1:
            continue
        other = index[key][0]
        a, b = shapes[obj["id"]], shapes[other["id"]]
        if _iou(a, b) == 0.0 and _dist(a, b) > tol.handle_max_shift:
            continue
        pairs[obj["id"]] = (other["id"], 1.0)
    return pairs


def _match_geometric(
    base: list[dict[str, Any]],
    target: list[dict[str, Any]],
    shapes: dict[str, _Shape],
    tol: DiffTolerances,
) -> Pairs:
    cell = max(1.0, 2 * tol.match_centroid)
    grid: dict[tuple[int, int], list[str]] = defaultdict(list)
    wide: list[str] = []
    located = [o["id"] for o in target if shapes[o["id"]].box is not None]

    def cells(shape: _Shape) -> list[tuple[int, int]] | None:
        assert shape.box is not None  # noqa: S101
        x0, y0, x1, y1 = _padded(shape.box, tol.match_centroid)
        cx = range(math.floor(x0 / cell), math.floor(x1 / cell) + 1)
        cy = range(math.floor(y0 / cell), math.floor(y1 / cell) + 1)
        if len(cx) * len(cy) > _MAX_CELLS:
            return None
        return [(i, j) for i in cx for j in cy]

    for tid in located:
        cs = cells(shapes[tid])
        if cs is None:
            wide.append(tid)
        else:
            for c in cs:
                grid[c].append(tid)
    candidates: list[tuple[float, float, str, str]] = []
    for obj in base:
        a = shapes[obj["id"]]
        if a.box is None:
            continue
        cs = cells(a)
        pool: set[str] = set(located) if cs is None else set(wide)
        for c in cs or ():
            pool.update(grid.get(c, ()))
        for tid in pool:
            b = shapes[tid]
            iou, d = _iou(a, b), _dist(a, b)
            if iou >= tol.match_min_iou or d <= tol.match_centroid:
                candidates.append((-iou, d, obj["id"], tid))
    candidates.sort()
    pairs: Pairs = {}
    used: set[str] = set()
    for neg_iou, _d, bid, tid in candidates:
        if bid in pairs or tid in used:
            continue
        pairs[bid] = (tid, round(-neg_iou, 6))
        used.add(tid)
    return pairs


# --- wall split / merge ---------------------------------------------------------------------


def _pieces_cover(
    whole: geo.Segment, pieces: list[tuple[str, geo.Segment]], tol: DiffTolerances
) -> list[tuple[str, float, float]] | None:
    """Pieces (id, start, end along ``whole``) when they are collinear with ``whole``, lie
    within it and cover it with gaps <= ``split_max_gap``; else None."""
    ux, uy = whole[1][0] - whole[0][0], whole[1][1] - whole[0][1]
    length = math.hypot(ux, uy)
    if length <= tol.split_max_gap:
        return None
    ux, uy = ux / length, uy / length
    ax, ay = whole[0]
    spans: list[tuple[str, float, float]] = []
    for pid, seg in pieces:
        if not geo.parallel(whole, seg):
            return None
        ts = []
        for px, py in seg:
            if abs((px - ax) * uy - (py - ay) * ux) > tol.split_offset:
                return None
            ts.append((px - ax) * ux + (py - ay) * uy)
        lo, hi = min(ts), max(ts)
        if lo < -tol.split_max_gap or hi > length + tol.split_max_gap:
            return None
        spans.append((pid, round(lo, 9), round(hi, 9)))
    spans.sort(key=lambda s: (s[1], s[2], s[0]))
    if spans[0][1] > tol.split_max_gap or spans[-1][2] < length - tol.split_max_gap:
        return None
    for (_a, _lo, a_hi), (_b, b_lo, _hi) in zip(spans, spans[1:], strict=False):
        if b_lo - a_hi > tol.split_max_gap or a_hi - b_lo > max(tol.split_offset, 0.01):
            return None  # an uncovered gap, or pieces overlapping each other
    return spans


def _collinear_candidates(
    whole: geo.Segment, axes: dict[str, geo.Segment], used: set[str], tol: DiffTolerances
) -> list[tuple[str, geo.Segment]]:
    """Free walls on the same line as ``whole`` that overlap its extent."""
    ux, uy = whole[1][0] - whole[0][0], whole[1][1] - whole[0][1]
    length = math.hypot(ux, uy)
    ux, uy = ux / length, uy / length
    ax, ay = whole[0]
    out = []
    for oid in sorted(axes):
        if oid in used:
            continue
        seg = axes[oid]
        if not geo.parallel(whole, seg):
            continue
        if any(abs((px - ax) * uy - (py - ay) * ux) > tol.split_offset for px, py in seg):
            continue
        ts = [(px - ax) * ux + (py - ay) * uy for px, py in seg]
        if max(ts) <= tol.split_max_gap or min(ts) >= length - tol.split_max_gap:
            continue  # merely touches an end
        out.append((oid, seg))
    return out


def match_split_merge(
    base: list[dict[str, Any]], target: list[dict[str, Any]], tol: DiffTolerances
) -> list[dict[str, Any]]:
    """Wall split (1 -> N) and merge (N -> 1) groups among unmatched straight walls.

    Deterministic: base walls are tried in id order for splits, then target walls in id order
    for merges; a wall joins at most one group. Each group is
    ``{"type", "base_ids", "target_ids", "spans", "length"}`` with spans along the whole wall.
    """
    axes_b = {
        str(o["id"]): a
        for o in base
        if o["kind"] == "wall" and (a := geo.straight_axis(o.get("geometry"))) is not None
    }
    axes_t = {
        str(o["id"]): a
        for o in target
        if o["kind"] == "wall" and (a := geo.straight_axis(o.get("geometry"))) is not None
    }
    used_b: set[str] = set()
    used_t: set[str] = set()
    groups: list[dict[str, Any]] = []
    for kind, wholes, parts, used_w, used_p in (
        ("split", axes_b, axes_t, used_b, used_t),
        ("merged", axes_t, axes_b, used_t, used_b),
    ):
        for wid in sorted(wholes):
            if wid in used_w:
                continue
            whole = wholes[wid]
            cands = _collinear_candidates(whole, parts, used_p, tol)
            if len(cands) < 2:
                continue
            spans = _pieces_cover(whole, cands, tol)
            if spans is None:
                continue
            used_w.add(wid)
            used_p.update(pid for pid, _lo, _hi in spans)
            ids = sorted(pid for pid, _lo, _hi in spans)  # pieces keep spatial order
            groups.append(
                {
                    "type": kind,
                    "base_ids": [wid] if kind == "split" else ids,
                    "target_ids": ids if kind == "split" else [wid],
                    "spans": spans,
                    "length": round(math.dist(*whole), 9),
                }
            )
    return groups


# --- deltas -----------------------------------------------------------------------------


def _quantity(value: Any) -> tuple[float, str] | None:
    if (
        isinstance(value, dict)
        and isinstance(value.get("value"), (int, float))
        and not isinstance(value.get("value"), bool)
        and isinstance(value.get("unit"), str)
    ):
        return float(value["value"]), value["unit"]
    return None


def property_deltas(
    base: Mapping[str, Any], target: Mapping[str, Any], tol: DiffTolerances
) -> list[dict[str, Any]]:
    kind = base["kind"]
    out: list[dict[str, Any]] = []
    bp, tp = base.get("properties") or {}, target.get("properties") or {}
    for name in sorted(set(bp) | set(tp)):
        if name.startswith("_"):
            continue
        entry: dict[str, Any] = {"property": name, "fact_path": f"{kind}.{name}"}
        if name not in tp or name not in bp:
            q = _quantity(bp.get(name, tp.get(name)))
            out.append(
                {
                    **entry,
                    "change": "removed" if name not in tp else "added",
                    "base": bp.get(name),
                    "target": tp.get(name),
                    "unit": q[1] if q else None,
                }
            )
            continue
        a, b = _quantity(bp[name]), _quantity(tp[name])
        if a is not None and b is not None:
            if a[1] != b[1]:
                out.append(
                    {
                        **entry,
                        "change": "unit",
                        "base": bp[name],
                        "target": tp[name],
                        "delta": None,
                        "unit": None,
                    }
                )
                continue
            delta = round(b[0] - a[0], 9)
            if abs(delta) > tol.numeric_tolerance(kind, name, a[0], b[0]):
                out.append(
                    {
                        **entry,
                        "change": "numeric",
                        "base": a[0],
                        "target": b[0],
                        "delta": delta,
                        "unit": a[1],
                    }
                )
        elif bp[name] != tp[name]:
            out.append({**entry, "change": "value", "base": bp[name], "target": tp[name]})
    if base.get("layer") != target.get("layer"):
        out.append(
            {
                "property": "layer",
                "fact_path": None,
                "change": "value",
                "base": base.get("layer"),
                "target": target.get("layer"),
            }
        )
    return out


def geometry_deltas(a: _Shape, b: _Shape, tol: DiffTolerances) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    ca, cb = a.centroid, b.centroid
    if ca is None or cb is None or a.box is None or b.box is None:
        return out
    dx, dy = round(cb[0] - ca[0], 9), round(cb[1] - ca[1], 9)
    dist = round(math.hypot(dx, dy), 9)
    if dist > tol.position:
        out.append(
            {
                "property": "geometry.centroid",
                "fact_path": None,
                "change": "geometry",
                "base": [round(ca[0], 9), round(ca[1], 9)],
                "target": [round(cb[0], 9), round(cb[1], 9)],
                "delta": dist,
                "dx": dx,
                "dy": dy,
                "unit": "m",
            }
        )
    sa = (a.box[2] - a.box[0], a.box[3] - a.box[1])
    sb = (b.box[2] - b.box[0], b.box[3] - b.box[1])
    dw, dh = round(sb[0] - sa[0], 9), round(sb[1] - sa[1], 9)
    if max(abs(dw), abs(dh)) > tol.position:
        out.append(
            {
                "property": "geometry.bbox_size",
                "fact_path": None,
                "change": "geometry",
                "base": [round(sa[0], 9), round(sa[1], 9)],
                "target": [round(sb[0], 9), round(sb[1], 9)],
                "delta": round(math.hypot(dw, dh), 9),
                "dx": dw,
                "dy": dh,
                "unit": "m",
            }
        )
    return out


# --- diff -------------------------------------------------------------------------------


def _label(obj: Mapping[str, Any]) -> str:
    name = (obj.get("properties") or {}).get("name")
    return name if isinstance(name, str) and name else obj["kind"]


def _change(
    change_type: str,
    base: dict[str, Any] | None,
    target: dict[str, Any] | None,
    *,
    method: str | None = None,
    score: float | None = None,
    deltas: list[dict[str, Any]] | None = None,
    lineage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ref = target or base
    assert ref is not None  # noqa: S101
    return {
        "lineage": lineage or {},
        "change_type": change_type,
        "kind": ref["kind"],
        "base_object_id": base["id"] if base else None,
        "target_object_id": target["id"] if target else None,
        "match_method": method,
        "match_score": score,
        "label": _label(ref),
        "base_handles": list(base.get("source_handles") or ()) if base else [],
        "target_handles": list(target.get("source_handles") or ()) if target else [],
        "deltas": deltas or [],
    }


def diff_objects(
    base_objects: Iterable[dict[str, Any]],
    target_objects: Iterable[dict[str, Any]],
    tolerances: DiffTolerances | None = None,
) -> list[dict[str, Any]]:
    tol = tolerances or DiffTolerances()
    base = sorted(base_objects, key=lambda o: o["id"])
    target = sorted(target_objects, key=lambda o: o["id"])
    shapes = {o["id"]: _shape(o) for o in (*base, *target)}
    by_b = {o["id"]: o for o in base}
    by_t = {o["id"]: o for o in target}
    changes: list[dict[str, Any]] = []
    for kind in sorted({o["kind"] for o in (*base, *target)}):
        kb = [o for o in base if o["kind"] == kind]
        kt = [o for o in target if o["kind"] == kind]
        by_id_kt = {o["id"]: o for o in kt}
        pairs: Pairs = _match_by_content(kb, kt)
        lineage_b: set[str] = set()
        lineage_t: set[str] = set()
        if tol.detect_split and kind == "wall":
            taken = {t for t, _ in pairs.values()}
            groups = match_split_merge(
                [o for o in kb if o["id"] not in pairs],
                [o for o in kt if o["id"] not in taken],
                tol,
            )
            for grp in groups:
                lineage_b.update(grp["base_ids"])
                lineage_t.update(grp["target_ids"])
                changes += _lineage_changes(grp, by_b, by_t)
        # identical content: "handle" when the DXF handles agree too, else "content"
        methods = {
            bid: "handle"
            if by_b[bid].get("source_handles") == by_id_kt[tid].get("source_handles")
            else "content"
            for bid, (tid, _s) in pairs.items()
        }
        taken = {t for t, _ in pairs.values()} | lineage_t
        if tol.match_by_handle:
            hp = _match_by_handle(
                [o for o in kb if o["id"] not in pairs and o["id"] not in lineage_b],
                [o for o in kt if o["id"] not in taken],
                shapes,
                tol,
            )
            pairs.update(hp)
            methods.update(dict.fromkeys(hp, "handle"))
            taken = {t for t, _ in pairs.values()} | lineage_t
        geom = _match_geometric(
            [o for o in kb if o["id"] not in pairs and o["id"] not in lineage_b],
            [o for o in kt if o["id"] not in taken],
            shapes,
            tol,
        )
        pairs.update(geom)
        methods.update(dict.fromkeys(geom, "geometry"))
        matched_t = {t for t, _ in pairs.values()}
        for bid, (tid, score) in pairs.items():
            b, t = by_b[bid], by_t[tid]
            if b.get("object_hash") and b.get("object_hash") == t.get("object_hash"):
                deltas: list[dict[str, Any]] = []
            else:
                deltas = property_deltas(b, t, tol) + geometry_deltas(shapes[bid], shapes[tid], tol)
            ctype = "modified" if deltas else "unchanged"
            changes.append(_change(ctype, b, t, method=methods[bid], score=score, deltas=deltas))
        changes += [
            _change("removed", o, None)
            for o in kb
            if o["id"] not in pairs and o["id"] not in lineage_b
        ]
        changes += [
            _change("added", None, o)
            for o in kt
            if o["id"] not in matched_t and o["id"] not in lineage_t
        ]
    changes.sort(
        key=lambda c: (
            c["kind"],
            _ORDER[c["change_type"]],
            c["base_object_id"] or "",
            c["target_object_id"] or "",
        )
    )
    return changes


def _lineage_changes(
    grp: dict[str, Any], by_b: dict[str, dict[str, Any]], by_t: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """One change row per (whole, piece) link of a split / merge group."""
    length = grp["length"]
    covered = round(sum(hi - lo for _pid, lo, hi in grp["spans"]), 9)
    lineage = {
        "group_type": grp["type"],
        "base_object_ids": list(grp["base_ids"]),
        "target_object_ids": list(grp["target_ids"]),
        "whole_length": length,
        "covered_length": covered,
        "pieces": [{"object_id": pid, "start": lo, "end": hi} for pid, lo, hi in grp["spans"]],
    }
    out = []
    for pid, lo, hi in grp["spans"]:
        if grp["type"] == "split":
            base, target = by_b[grp["base_ids"][0]], by_t[pid]
        else:
            base, target = by_b[pid], by_t[grp["target_ids"][0]]
        score = round((hi - lo) / length, 6) if length > 0 else None
        out.append(
            _change(grp["type"], base, target, method="collinear", score=score, lineage=lineage)
        )
    return out


def change_id(diff_id: str, change: Mapping[str, Any], namespace: uuid.UUID) -> str:
    return str(
        uuid.uuid5(
            namespace,
            f"change\n{diff_id}\n{change['change_type']}\n"
            f"{change['base_object_id'] or '-'}\n{change['target_object_id'] or '-'}",
        )
    )


def summarize(changes: list[dict[str, Any]], base_count: int, target_count: int) -> dict[str, Any]:
    by_kind: dict[str, dict[str, int]] = {}
    for c in changes:
        row = by_kind.setdefault(c["kind"], dict.fromkeys(CHANGE_TYPES, 0))
        row[c["change_type"]] += 1
    totals = {t: sum(1 for c in changes if c["change_type"] == t) for t in CHANGE_TYPES}
    return {
        **totals,
        "base_object_count": base_count,
        "target_object_count": target_count,
        "by_kind": by_kind,
    }


# --- impact on check results ------------------------------------------------------------


def _reads_kind(path: str, kind: str) -> bool:
    kinds = {kind, "corridor"} if kind == "space" else {kind}
    root, _, rest = path.partition(".")
    if root in kinds:
        return True
    return root == "drawing" and rest.split("_", 1)[0] in kinds


def _affects(change: dict[str, Any], result: Mapping[str, Any], side: str) -> str | None:
    oid = change[f"{side}_object_id"]
    if oid and oid in {str(x) for x in result.get("arch_object_ids") or ()}:
        return "object"
    paths = result.get("fact_paths") or ()
    if change["change_type"] == "modified":
        props = {d["fact_path"] for d in change["deltas"] if d.get("fact_path")}
        moved = any(d["change"] == "geometry" for d in change["deltas"])
        for p in paths:
            if p in props or (moved and _reads_kind(p, change["kind"])):
                return "fact"
            if p.startswith("drawing.") and props and _reads_kind(p, change["kind"]):
                return "fact"
    elif change["change_type"] in ("added", "removed", "split", "merged") and any(
        _reads_kind(p, change["kind"]) for p in paths
    ):
        return "fact"
    return None


def attach_impact(
    changes: list[dict[str, Any]],
    base_results: list[dict[str, Any]],
    target_results: list[dict[str, Any]],
) -> None:
    """Set ``impact = {"base": [...], "target": [...]}`` on every change, in place.

    Base results are bound to the old revision: each one listed is ``stale`` and is never
    carried over to the target; only a run on the target revision counts there.
    """
    for change in changes:
        impact: dict[str, list[dict[str, Any]]] = {"base": [], "target": []}
        if change["change_type"] != "unchanged":
            for side, results in (("base", base_results), ("target", target_results)):
                for r in results:
                    via = _affects(change, r, side)
                    if via:
                        ref = {
                            "result_id": r["id"],
                            "run_id": r["run_id"],
                            "rule_version_id": r["rule_version_id"],
                            "outcome": r["outcome"],
                            "via": via,
                        }
                        if side == "base":
                            ref["stale"] = True
                        impact[side].append(ref)
        change["impact"] = impact


def transitions(
    base_results: list[dict[str, Any]],
    target_results: list[dict[str, Any]],
    changes: list[dict[str, Any]],
    *,
    base_run_id: str,
    target_run_id: str,
) -> list[dict[str, Any]]:
    """Per-rule outcome transition between a base run and its re-run on the target."""
    b = {r["rule_version_id"]: r for r in base_results}
    t = {r["rule_version_id"]: r for r in target_results}
    linked: dict[str, set[str]] = defaultdict(set)
    for c in changes:
        for ref in c.get("impact", {}).get("base", []):
            linked[ref["rule_version_id"]].add(c["id"])
    out = []
    for rv in sorted(set(b) | set(t)):
        bo = b[rv]["outcome"] if rv in b else None
        to = t[rv]["outcome"] if rv in t else None
        both = bo is not None and to is not None
        out.append(
            {
                "base_run_id": base_run_id,
                "target_run_id": target_run_id,
                "rule_version_id": rv,
                "rule_title": (b.get(rv) or t[rv]).get("rule_title"),
                "base_outcome": bo,
                "target_outcome": to,
                "transition": f"{bo or 'N/A'}->{to or 'N/A'}",
                "changed": bo != to,
                "regression": both and _RANK[to] > _RANK[bo],  # type: ignore[index]
                "improvement": both and _RANK[to] < _RANK[bo],  # type: ignore[index]
                "change_ids": sorted(linked.get(rv, set())),
            }
        )
    return out
