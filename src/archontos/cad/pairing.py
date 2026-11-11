"""MVP-2 object pairing: explicit, deterministic links between two object kinds (pure).

Rules that need two kinds at once (a door and its host wall, a stair and its storey, a space
and the doors that open into it) read *pair facts* that this module derives before any rule
runs. Every pair has a status:

``paired``     exactly one partner (or, for counts, a well-defined set) was found;
``unpaired``   no partner, or the inputs needed to look for one are missing;
``ambiguous``  several partners fit equally well.

Only ``paired`` pairs produce facts. A rule that reads a pair path of an object whose pair is
``unpaired`` or ``ambiguous`` gives ``REVIEW`` with the pairing reason (fail closed), never
``PASS`` or ``FAIL``.

Pair fact paths (all lengths in m):

* ``door.host_wall.<prop>`` / ``window.host_wall.<prop>`` (``thickness``, ``length``,
  ``height``, ...). IFC: ``IfcRelFillsElement`` -> opening -> ``IfcRelVoidsElement`` -> wall
  (``host_ref`` property). Otherwise geometric: walls within ``host_max_distance`` of the
  door's insert point; two parallel wall lines that are faces of one wall count as one host.
* ``stair.storey.elevation`` (m), ``stair.storey.floor_number`` (count; 1, 2, ... above
  ground, 0 for basements), ``stair.storey.basement_level`` (count; 1, 2, ... below ground,
  0 above), ``stair.storey.stair_count`` (stairs on that storey). IFC:
  ``IfcRelContainedInSpatialStructure`` storey of the stair, matched by name to the model
  storeys. DXF has no storeys, so these pairs are ``unpaired``.
* ``space.door_count`` / ``corridor.door_count`` (count): doors whose insert point lies in the
  space polygon or within ``door_space_distance`` of its boundary. Ambiguous when any door has
  no plan location.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from archontos.cad import geometry as g

PAIR_STATUSES = ("paired", "unpaired", "ambiguous")


@dataclass(frozen=True, slots=True)
class PairTolerances:
    host_max_distance: float = 0.35  # m: door insert point to a host wall line
    host_ambiguity: float = 0.05  # m: a second host this much farther is still a tie
    wall_max_thickness: float = 0.6  # m: two parallel wall lines closer are faces of one wall
    wall_min_overlap: float = 0.1  # m: faces must overlap along the wall by at least this
    door_space_distance: float = 0.35  # m: door point to space boundary

    def as_dict(self) -> dict[str, float]:
        return {k: getattr(self, k) for k in self.__slots__}


DEFAULT_PAIR_TOLERANCES = PairTolerances()


@dataclass(frozen=True, slots=True)
class Pair:
    """One pairing outcome of an object (e.g. its ``host_wall``)."""

    name: str
    status: str
    method: str
    reason: str | None = None
    related_ids: tuple[str, ...] = ()
    values: dict[str, float] = field(default_factory=dict)
    units: dict[str, str] = field(default_factory=dict)
    #: True: facts live under ``<kind>.<name>.<value>``; False: directly ``<kind>.<value>``.
    nested: bool = True
    #: Fact names this pair provides when it is not nested (e.g. ``door_count``).
    provides: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "method": self.method,
            "reason": self.reason,
            "related_object_ids": list(self.related_ids),
        }


#: Which pair names exist per kind (for documentation and the rule-path check).
PAIR_NAMES: dict[str, tuple[str, ...]] = {
    "door": ("host_wall",),
    "window": ("host_wall",),
    "stair": ("storey",),
    "space": ("door_count",),
    "corridor": ("door_count",),
}


def _quantity(prop: Any) -> tuple[float, str] | None:
    if (
        isinstance(prop, dict)
        and isinstance(prop.get("value"), (int, float))
        and not isinstance(prop.get("value"), bool)
        and isinstance(prop.get("unit"), str)
        and math.isfinite(prop["value"])
    ):
        return float(prop["value"]), prop["unit"]
    return None


_LENGTH_TO_M = {"mm": 0.001, "cm": 0.01, "m": 1.0}


def _numeric_props(obj: dict[str, Any]) -> tuple[dict[str, float], dict[str, str]]:
    values: dict[str, float] = {}
    units: dict[str, str] = {}
    for name, prop in sorted((obj.get("properties") or {}).items()):
        q = _quantity(prop)
        if q is None:
            continue
        value, unit = q
        if unit in _LENGTH_TO_M:
            value, unit = round(value * _LENGTH_TO_M[unit], 9), "m"
        values[name], units[name] = value, unit
    return values, units


# --- walls: faces of one wall --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WallGroup:
    """One physical wall: a single line, or two parallel face lines (``thickness`` known)."""

    member_ids: tuple[str, ...]
    thickness: float | None


def wall_groups(
    walls: list[dict[str, Any]], tol: PairTolerances = DEFAULT_PAIR_TOLERANCES
) -> dict[str, WallGroup]:
    """Group wall lines that are the two faces of one wall (mutual nearest parallel line).

    Two straight wall lines are faces of one wall when they are parallel, overlap along their
    length by at least ``wall_min_overlap`` and are at most ``wall_max_thickness`` apart; each
    line is paired with its nearest such line only if that choice is mutual (ties broken by
    id). The face distance becomes the wall ``thickness`` (method ``parallel_faces``).
    """
    axes = {
        str(w["id"]): axis
        for w in sorted(walls, key=lambda o: str(o["id"]))
        if (axis := g.straight_axis(w.get("geometry"))) is not None
    }
    nearest: dict[str, tuple[float, str]] = {}
    ids = sorted(axes)
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            sa, sb = axes[a], axes[b]
            if not g.parallel(sa, sb):
                continue
            dist, _lo, _hi, overlap = g.offset_and_overlap(sa, sb)
            if overlap < tol.wall_min_overlap or not 0.001 < dist <= tol.wall_max_thickness:
                continue
            for x, y in ((a, b), (b, a)):
                if x not in nearest or (dist, y) < nearest[x]:
                    nearest[x] = (dist, y)
    groups: dict[str, WallGroup] = {}
    for w in sorted(walls, key=lambda o: str(o["id"])):
        wid = str(w["id"])
        if wid in groups:
            continue
        hit = nearest.get(wid)
        if hit is not None and nearest.get(hit[1], (0.0, ""))[1] == wid:
            group = WallGroup(tuple(sorted((wid, hit[1]))), round(hit[0], 9))
            groups[wid] = groups[hit[1]] = group
        else:
            groups[wid] = WallGroup((wid,), None)
    return groups


def _host_pair(
    name: str,
    values: dict[str, float],
    units: dict[str, str],
    *,
    method: str,
    related: tuple[str, ...],
) -> Pair:
    return Pair(name, "paired", method, None, related, values, units)


def _wall_values(
    wall: dict[str, Any], group: WallGroup | None
) -> tuple[dict[str, float], dict[str, str]]:
    values, units = _numeric_props(wall)
    if "thickness" not in values and group is not None and group.thickness is not None:
        values["thickness"], units["thickness"] = group.thickness, "m"
    return values, units


def _door_host(
    door: dict[str, Any],
    walls: list[dict[str, Any]],
    groups: dict[str, WallGroup],
    by_ref: dict[str, list[dict[str, Any]]],
    tol: PairTolerances,
) -> Pair:
    props = door.get("properties") or {}
    host_ref = props.get("host_ref")
    if isinstance(host_ref, list):
        return Pair(
            "host_wall",
            "ambiguous",
            "ifc:IfcRelFillsElement",
            f"the opening is hosted by {len(host_ref)} elements",
        )
    if isinstance(host_ref, str) and host_ref:
        hits = by_ref.get(host_ref, [])
        if len(hits) == 1:
            wall = hits[0]
            values, units = _wall_values(wall, groups.get(str(wall["id"])))
            return _host_pair(
                "host_wall",
                values,
                units,
                method="ifc:IfcRelFillsElement",
                related=(str(wall["id"]),),
            )
        if not hits:
            return Pair(
                "host_wall",
                "unpaired",
                "ifc:IfcRelFillsElement",
                f"host element {host_ref} is not mapped as a wall",
            )
        return Pair(
            "host_wall",
            "ambiguous",
            "ifc:IfcRelFillsElement",
            f"{len(hits)} walls carry host id {host_ref}",
        )
    point = g.location(door.get("geometry"))
    if point is None:
        return Pair("host_wall", "unpaired", "geometry", "the opening has no plan location")
    best: dict[tuple[str, ...], tuple[float, str]] = {}
    for wall in walls:
        segs = g.segments(wall.get("geometry"))
        if not segs:
            continue
        dist = min(g.point_segment_distance(point, s) for s in segs)
        if dist > tol.host_max_distance:
            continue
        wid = str(wall["id"])
        key = groups[wid].member_ids if wid in groups else (wid,)
        if key not in best or (dist, wid) < best[key]:
            best[key] = (dist, wid)
    if not best:
        return Pair(
            "host_wall",
            "unpaired",
            "geometry",
            f"no wall line within {tol.host_max_distance} m of the insert point",
        )
    ranked = sorted(best.items(), key=lambda kv: (kv[1][0], kv[1][1]))
    if len(ranked) > 1 and ranked[1][1][0] - ranked[0][1][0] <= tol.host_ambiguity:
        return Pair(
            "host_wall",
            "ambiguous",
            "geometry",
            f"{len(ranked)} walls lie within {tol.host_max_distance} m of the insert point",
            tuple(sorted(i for key, _ in ranked for i in key)),
        )
    members, (_dist, wid) = ranked[0]
    wall = next(w for w in walls if str(w["id"]) == wid)
    values, units = _wall_values(wall, groups.get(wid))
    return _host_pair("host_wall", values, units, method="geometry", related=members)


# --- stairs: storey -------------------------------------------------------------------------


def _storey_index(model_meta: dict[str, Any] | None) -> list[dict[str, Any]]:
    storeys = (model_meta or {}).get("storeys")
    if not isinstance(storeys, list):
        return []
    rows = [
        s
        for s in storeys
        if isinstance(s, dict)
        and isinstance(s.get("name"), str)
        and isinstance(s.get("elevation_m"), (int, float))
    ]
    rows.sort(key=lambda s: (float(s["elevation_m"]), s["name"], str(s.get("guid", ""))))
    above = [s for s in rows if s["elevation_m"] >= -0.5]
    below = [s for s in rows if s["elevation_m"] < -0.5]
    out = []
    for i, s in enumerate(above):
        out.append({**s, "floor_number": i + 1, "basement_level": 0})
    for i, s in enumerate(reversed(below)):
        out.append({**s, "floor_number": 0, "basement_level": i + 1})
    return out


def _stair_storey(
    stair: dict[str, Any], storeys: list[dict[str, Any]], stair_counts: dict[str, int]
) -> Pair:
    method = "ifc:IfcRelContainedInSpatialStructure"
    name = (stair.get("properties") or {}).get("storey")
    if not isinstance(name, str) or not name:
        return Pair("storey", "unpaired", method, "the stair is not contained in a storey")
    hits = [s for s in storeys if s["name"] == name]
    if not hits:
        return Pair("storey", "unpaired", method, f"storey {name!r} is not in the model storeys")
    if len(hits) > 1:
        return Pair("storey", "ambiguous", method, f"{len(hits)} model storeys are named {name!r}")
    s = hits[0]
    values = {
        "elevation": round(float(s["elevation_m"]), 9),
        "floor_number": float(s["floor_number"]),
        "basement_level": float(s["basement_level"]),
        "stair_count": float(stair_counts[name]),
    }
    units = {
        "elevation": "m",
        "floor_number": "count",
        "basement_level": "count",
        "stair_count": "count",
    }
    related = (str(s["guid"]),) if s.get("guid") else ()
    return Pair("storey", "paired", method, None, related, values, units)


# --- spaces: doors --------------------------------------------------------------------------


def _space_doors(
    space: dict[str, Any],
    located: list[tuple[str, g.Point]],
    unlocated: int,
    tol: PairTolerances,
) -> Pair:
    method = "geometry"
    ring = g.polygon(space.get("geometry"))
    if ring is None:
        return Pair(
            "door_count",
            "unpaired",
            method,
            "the space has no plan polygon",
            nested=False,
            provides=("door_count",),
        )
    if unlocated:
        return Pair(
            "door_count",
            "ambiguous",
            method,
            f"{unlocated} door(s) have no plan location",
            nested=False,
            provides=("door_count",),
        )
    hits = tuple(
        did
        for did, p in located
        if g.contains(ring, p) or g.distance_to_ring(ring, p) <= tol.door_space_distance
    )
    return Pair(
        "door_count",
        "paired",
        method,
        None,
        hits,
        {"door_count": float(len(hits))},
        {"door_count": "count"},
        nested=False,
        provides=("door_count",),
    )


# --- entry point ----------------------------------------------------------------------------


def pair_objects(
    objects: list[dict[str, Any]],
    *,
    model_meta: dict[str, Any] | None = None,
    tolerances: PairTolerances = DEFAULT_PAIR_TOLERANCES,
) -> tuple[dict[str, dict[str, Pair]], dict[str, WallGroup]]:
    """Pairs per object id (``{object_id: {pair_name: Pair}}``) and the wall face groups."""
    ordered = sorted(objects, key=lambda o: str(o["id"]))
    walls = [o for o in ordered if o.get("kind") == "wall"]
    groups = wall_groups(walls, tolerances)
    by_ref: dict[str, list[dict[str, Any]]] = {}
    for wall in walls:
        refs = set(wall.get("source_handles") or ())
        for ref in sorted(refs):
            by_ref.setdefault(str(ref), []).append(wall)
    storeys = _storey_index(model_meta)
    stair_counts: dict[str, int] = {}
    for o in ordered:
        name = (o.get("properties") or {}).get("storey")
        if o.get("kind") == "stair" and isinstance(name, str):
            stair_counts[name] = stair_counts.get(name, 0) + 1
    doors = [o for o in ordered if o.get("kind") == "door"]
    located = [(str(d["id"]), p) for d in doors if (p := g.location(d.get("geometry"))) is not None]
    unlocated = len(doors) - len(located)

    out: dict[str, dict[str, Pair]] = {}
    for o in ordered:
        oid, kind = str(o["id"]), o.get("kind")
        if kind in ("door", "window"):
            out[oid] = {"host_wall": _door_host(o, walls, groups, by_ref, tolerances)}
        elif kind == "stair":
            out[oid] = {"storey": _stair_storey(o, storeys, stair_counts)}
        elif kind == "space":
            out[oid] = {"door_count": _space_doors(o, located, unlocated, tolerances)}
    return out, groups
