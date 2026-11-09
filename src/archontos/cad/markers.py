"""Issue markers for CAD drawings: marker plan, DXF writer and power-cad payload.

Pure functions, no network, no database. Three steps:

1. :func:`build_marker_plan` turns :class:`IssueMarker` inputs (FAIL / REVIEW issues with a bbox or
   point in drawing units) into a JSON-able plan. Same inputs give the same plan and the same
   ``sha256`` (canonical JSON, coordinates rounded to 6 decimals, markers sorted by ``issue_id``).
2. :func:`apply_markers_to_dxf` reads the original DXF bytes with ezdxf and returns NEW bytes: the
   original modelspace entities are kept, the layer ``ARCHONTOS-ISSUE`` is added if absent, and
   per marker a CIRCLE (the "cloud"), a leader LINE and an MTEXT label are drawn on that layer.
3. :func:`to_power_cad_payload` maps the plan to the shape ``power-cad`` ``cad_create_many`` takes.

Plan -> power-cad ``cad_create_many`` mapping
---------------------------------------------
``to_power_cad_payload(plan)`` returns ``{"layers": [...], "items": [...]}``:

* ``layers``: one entry ``{"name": "ARCHONTOS-ISSUE", "color": 7}`` (create the layer if absent).
* ``items``: three entities per marker, in plan order, all with ``"layer": plan["layer"]``:
  ``{"type": "circle", "center": [x, y], "radius": r, "color": 1|2}``,
  ``{"type": "line", "start": [x, y], "end": [x, y], "color": 1|2}`` (the leader) and
  ``{"type": "mtext", "insert": [x, y], "text": "[FAIL] rule: text", "height": h, "color": 1|2}``.
  Each item also carries ``"xdata": {"app": "ARCHONTOS", "issue_id": ..., "outcome": ...}`` so the
  marker can be found and removed again. Colors are AutoCAD Color Index values: FAIL red = 1,
  REVIEW yellow = 2. Entity types are the ``cad_create`` types (line, circle, mtext). The caller
  adds the document binding and ``dry_run`` flags; the lead should adapt key names if the live
  ``cad_create_many`` schema differs. Nothing here talks to AutoCAD.

Determinism
-----------
The plan is fully deterministic. ``apply_markers_to_dxf`` is deterministic for identical
``(bytes, plan)`` on the same ezdxf version: ezdxf normally stamps ``$TDUPDATE`` (current time)
and a random ``$VERSIONGUID`` on every write, so the writer temporarily turns on
``ezdxf.options.write_fixed_meta_data_for_testing`` (fixed 2000-01-01 dates and a constant GUID)
under a lock and restores the previous value. Remaining non-determinism: the output embeds the
ezdxf version string (``$ACADVER`` family and the ezdxf metadata block), so bytes may differ
across ezdxf releases; entity handles of the new entities are allocated from the original
``$HANDSEED`` and are stable for identical input. Original header timestamps are replaced by the
fixed values above (the new document is not a byte-for-byte copy of the original).
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import threading
from dataclasses import dataclass
from typing import Any, Literal

MARKER_LAYER = "ARCHONTOS-ISSUE"
PLAN_VERSION = "archontos-cad-markers/1"
#: AutoCAD Color Index per outcome: FAIL red, REVIEW yellow.
OUTCOME_COLORS: dict[str, int] = {"FAIL": 1, "REVIEW": 2}
#: Default label text height per drawing unit (about 2.5 mm paper text at 1:100).
DEFAULT_TEXT_HEIGHT: dict[str, float] = {"mm": 250.0, "cm": 25.0, "m": 0.25, "in": 10.0, "ft": 1.0}
_VALID_UNITS = tuple(DEFAULT_TEXT_HEIGHT)
#: The circle is this much larger than the half diagonal of the bbox so it encloses the object.
_BBOX_PADDING = 1.1
#: Minimum circle radius, in text heights, so point markers stay visible.
_MIN_RADIUS_TEXT_HEIGHTS = 2.0

Outcome = Literal["FAIL", "REVIEW"]

_META_LOCK = threading.Lock()


class MarkerError(ValueError):
    """Invalid marker input or plan."""


@dataclass(frozen=True, slots=True)
class IssueMarker:
    """One issue to mark on a drawing.

    ``bbox`` is ``(min_x, min_y, max_x, max_y)`` in drawing units; ``point`` is ``(x, y)``.
    At least one of them is required; the bbox wins when both are given. ``units`` is the unit of
    the drawing coordinates (``mm``, ``cm``, ``m``, ``in`` or ``ft``) and must be the same for
    every issue of one plan. ``entity_handle`` is the DXF handle of the offending entity, if any.
    """

    issue_id: str
    outcome: Outcome
    rule_label: str
    text: str
    units: str = "mm"
    entity_handle: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    point: tuple[float, float] | None = None


def _num(value: float) -> float:
    rounded = round(float(value), 6)
    return 0.0 if rounded == 0 else rounded


def _pt(x: float, y: float) -> list[float]:
    return [_num(x), _num(y)]


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _label(issue: IssueMarker) -> str:
    rule = " ".join(issue.rule_label.split())
    text = " ".join(issue.text.split())
    return f"[{issue.outcome}] {rule}: {text}"


def _validate(issue: IssueMarker) -> None:
    if not issue.issue_id:
        raise MarkerError("issue_id must not be empty")
    if issue.outcome not in OUTCOME_COLORS:
        raise MarkerError(
            f"{issue.issue_id}: outcome must be FAIL or REVIEW, got {issue.outcome!r}"
        )
    if issue.units not in _VALID_UNITS:
        raise MarkerError(f"{issue.issue_id}: units must be one of {list(_VALID_UNITS)}")
    if issue.bbox is None and issue.point is None:
        raise MarkerError(f"{issue.issue_id}: bbox or point is required")
    values = list(issue.bbox or ()) + list(issue.point or ())
    if not all(math.isfinite(v) for v in values):
        raise MarkerError(f"{issue.issue_id}: coordinates must be finite")


def _marker(issue: IssueMarker, text_height: float) -> dict[str, Any]:
    if issue.bbox is not None:
        x0, y0, x1, y1 = issue.bbox
        min_x, max_x = min(x0, x1), max(x0, x1)
        min_y, max_y = min(y0, y1), max(y0, y1)
        cx, cy = (min_x + max_x) / 2.0, (min_y + max_y) / 2.0
        half_diag = math.hypot(max_x - min_x, max_y - min_y) / 2.0
    else:
        cx, cy = issue.point or (0.0, 0.0)  # _validate guarantees bbox or point
        half_diag = 0.0
    radius = max(half_diag * _BBOX_PADDING, text_height * _MIN_RADIUS_TEXT_HEIGHTS)
    # Leader runs from the circle edge at 45 degrees up-right to the label corner.
    edge = radius / math.sqrt(2.0)
    start = (cx + edge, cy + edge)
    end = (cx + edge + text_height, cy + edge + text_height)
    label = _label(issue)
    color = OUTCOME_COLORS[issue.outcome]
    return {
        "issue_id": issue.issue_id,
        "outcome": issue.outcome,
        "entity_handle": issue.entity_handle,
        "anchor": _pt(cx, cy),
        "color": color,
        "circle": {"center": _pt(cx, cy), "radius": _num(radius), "color": color},
        "leader": {"start": _pt(*start), "end": _pt(*end), "color": color},
        "mtext": {
            "insert": _pt(end[0] + text_height * 0.25, end[1]),
            "text": label,
            "height": _num(text_height),
            "color": color,
        },
    }


def build_marker_plan(
    issues: list[IssueMarker] | tuple[IssueMarker, ...],
    layer: str = MARKER_LAYER,
    text_height: float | None = None,
) -> dict[str, Any]:
    """Build a deterministic, JSON-able marker plan.

    ``text_height`` is in drawing units; by default it follows the units (250 for mm, 0.25 for m).
    Markers are sorted by ``issue_id``; duplicate ids, mixed units and invalid input raise
    :class:`MarkerError`. The plan carries ``sha256`` over its canonical JSON (without the hash).
    """
    if not layer or not layer.strip():
        raise MarkerError("layer must not be empty")
    for issue in issues:
        _validate(issue)
    ids = [i.issue_id for i in issues]
    if len(set(ids)) != len(ids):
        raise MarkerError("duplicate issue_id")
    units_set = {i.units for i in issues}
    if len(units_set) > 1:
        raise MarkerError(f"mixed drawing units: {sorted(units_set)}")
    units = next(iter(units_set), "mm")
    height = DEFAULT_TEXT_HEIGHT[units] if text_height is None else float(text_height)
    if not math.isfinite(height) or height <= 0:
        raise MarkerError("text_height must be a positive number")
    body: dict[str, Any] = {
        "version": PLAN_VERSION,
        "layer": layer,
        "units": units,
        "text_height": _num(height),
        "markers": [_marker(i, height) for i in sorted(issues, key=lambda i: i.issue_id)],
    }
    plan = dict(body)
    plan["sha256"] = hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()
    return plan


def to_power_cad_payload(plan: dict[str, Any]) -> dict[str, Any]:
    """Map a marker plan to a power-cad ``cad_create_many`` payload (see the module docstring)."""
    layer = str(plan["layer"])
    items: list[dict[str, Any]] = []
    for m in plan["markers"]:
        tag = {"app": "ARCHONTOS", "issue_id": m["issue_id"], "outcome": m["outcome"]}
        c, leader, t = m["circle"], m["leader"], m["mtext"]
        items.append(
            {
                "type": "circle",
                "layer": layer,
                "center": list(c["center"]),
                "radius": c["radius"],
                "color": c["color"],
                "xdata": dict(tag),
            }
        )
        items.append(
            {
                "type": "line",
                "layer": layer,
                "start": list(leader["start"]),
                "end": list(leader["end"]),
                "color": leader["color"],
                "xdata": dict(tag),
            }
        )
        items.append(
            {
                "type": "mtext",
                "layer": layer,
                "insert": list(t["insert"]),
                "text": t["text"],
                "height": t["height"],
                "color": t["color"],
                "xdata": dict(tag),
            }
        )
    return {"layers": [{"name": layer, "color": 7}], "items": items}


def _escape_mtext(text: str) -> str:
    """Escape MTEXT control characters so label text is shown literally."""
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def apply_markers_to_dxf(original_dxf_bytes: bytes, plan: dict[str, Any]) -> bytes:
    """Return NEW DXF bytes with the plan's markers added; the input bytes are never modified.

    Needs the optional ``cad`` extra (ezdxf). Raises :class:`archontos.cad.dxf.CadParseError`
    for unreadable input.
    """
    from archontos.cad.dxf import _read_document  # reuse the same tolerant reader

    doc = _read_document(bytes(original_dxf_bytes))
    layer = str(plan["layer"])
    if layer not in doc.layers:
        doc.layers.add(layer, color=7)
    msp = doc.modelspace()
    for m in plan["markers"]:
        attribs = {"layer": layer, "color": int(m["color"])}
        circle = m["circle"]
        msp.add_circle(tuple(circle["center"]), circle["radius"], dxfattribs=dict(attribs))
        leader = m["leader"]
        msp.add_line(tuple(leader["start"]), tuple(leader["end"]), dxfattribs=dict(attribs))
        mt = m["mtext"]
        msp.add_mtext(
            _escape_mtext(mt["text"]),
            dxfattribs={
                **attribs,
                "insert": (mt["insert"][0], mt["insert"][1], 0.0),
                "char_height": mt["height"],
                "attachment_point": 7,  # bottom-left: text grows up-right from the leader end
            },
        )
    import ezdxf

    stream = io.StringIO()
    with _META_LOCK:
        previous = ezdxf.options.write_fixed_meta_data_for_testing
        ezdxf.options.write_fixed_meta_data_for_testing = True
        try:
            doc.write(stream)
        finally:
            ezdxf.options.write_fixed_meta_data_for_testing = previous
    return stream.getvalue().encode("utf-8")
