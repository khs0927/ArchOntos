"""Minimal, read-only, deterministic DXF normalizer (MVP-1).

Not a competing parser: the Ontology system owns full drawing parsing. This module only
turns a DXF file into a stable JSON document of layers, block counts and closed-polyline
area/perimeter, each fact carrying a (layer, handle) locator. It never writes to the input,
never guesses or converts units, and fails closed with ``DxfError``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

NORMALIZER_VERSION = "1"
SCHEMA = "archontos-cad-dxf/1"

# $INSUNITS code -> unit name. 0 (unitless) and unlisted codes are "unknown".
_INSUNITS = {
    1: "in",
    2: "ft",
    3: "mi",
    4: "mm",
    5: "cm",
    6: "m",
    7: "km",
    8: "uin",
    9: "mil",
    10: "yd",
    11: "angstrom",
    12: "nm",
    13: "um",
    14: "dm",
    15: "dam",
    16: "hm",
    17: "gm",
    18: "au",
    19: "ly",
    20: "pc",
}


class DxfError(Exception):
    """Input is missing, malformed or unsupported. No partial result is produced."""


def _load_ezdxf() -> Any:
    try:
        import ezdxf
    except ImportError as exc:
        raise DxfError(
            "ezdxf is not installed; install the optional extra: pip install 'archontos[cad]'"
        ) from exc
    return ezdxf


def parser_revision_id(ezdxf_version: str) -> str:
    seed = f"ezdxf={ezdxf_version};normalizer={NORMALIZER_VERSION}"
    return hashlib.sha256(seed.encode()).hexdigest()


def _area_perimeter(points: list[tuple[float, float]]) -> tuple[float, float]:
    n = len(points)
    twice_area = 0.0
    perimeter = 0.0
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        twice_area += x1 * y2 - x2 * y1
        perimeter += math.hypot(x2 - x1, y2 - y1)
    return abs(twice_area) / 2.0, perimeter


def _polyline_fact(entity: Any, unit: str) -> dict[str, Any] | None:
    kind = entity.dxftype()
    flags: list[str] = []
    if kind == "LWPOLYLINE":
        if not entity.closed:
            return None
        raw = list(entity.get_points("xyb"))
        points = [(float(x), float(y)) for x, y, _ in raw]
        has_bulge = any(b for _, _, b in raw)
    else:
        if not entity.is_2d_polyline or not entity.is_closed:
            return None
        points = [(float(v.dxf.location.x), float(v.dxf.location.y)) for v in entity.vertices]
        has_bulge = any(v.dxf.bulge for v in entity.vertices)
    if len(points) < 3:
        return None
    layer = str(entity.dxf.layer)
    handle = str(entity.dxf.handle)
    fact: dict[str, Any] = {
        "type": kind,
        "layer": layer,
        "handle": handle,
        "locator": {"layer": layer, "handle": handle},
        "vertex_count": len(points),
        "unit": unit,
        "area_unit": "unknown" if unit == "unknown" else f"{unit}^2",
        "area": None,
        "perimeter": None,
        "flags": flags,
    }
    if has_bulge:
        flags.append("bulge_arcs_unsupported")
    else:
        area, perimeter = _area_perimeter(points)
        fact["area"] = round(area, 9)
        fact["perimeter"] = round(perimeter, 9)
    if unit == "unknown":
        flags.append("unit_unknown")
    return fact


def normalize_dxf(path: str | Path) -> dict[str, Any]:
    """Return the normalized document for ``path``. Read-only; raises ``DxfError``."""
    ezdxf = _load_ezdxf()
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError as exc:
        raise DxfError(f"cannot read {p}: {exc}") from exc
    try:
        doc = ezdxf.readfile(str(p))
    except Exception as exc:  # noqa: BLE001  # ezdxf raises several unrelated types
        raise DxfError(f"malformed or unsupported DXF: {exc}") from exc

    try:
        code = int(doc.header.get("$INSUNITS", 0))
    except (TypeError, ValueError) as exc:
        raise DxfError("invalid $INSUNITS header value") from exc
    unit = _INSUNITS.get(code, "unknown")

    try:
        layers = sorted(str(layer.dxf.name) for layer in doc.layers)
        blocks: dict[str, int] = {}
        polylines: list[dict[str, Any]] = []
        for entity in doc.modelspace():
            kind = entity.dxftype()
            if kind == "INSERT":
                name = str(entity.dxf.name)
                blocks[name] = blocks.get(name, 0) + 1
            elif kind in ("LWPOLYLINE", "POLYLINE"):
                fact = _polyline_fact(entity, unit)
                if fact is not None:
                    polylines.append(fact)
    except Exception as exc:  # noqa: BLE001
        raise DxfError(f"malformed or unsupported DXF content: {exc}") from exc

    polylines.sort(key=lambda f: (f["layer"], f["handle"]))
    return {
        "schema": SCHEMA,
        "source_byte_revision_id": hashlib.sha256(data).hexdigest(),
        "parser_revision_id": parser_revision_id(ezdxf.__version__),
        "normalizer_version": NORMALIZER_VERSION,
        "ezdxf_version": ezdxf.__version__,
        "units": {"insunits_code": code, "unit": unit, "known": unit != "unknown"},
        "layers": layers,
        "block_counts": dict(sorted(blocks.items())),
        "closed_polylines": polylines,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m archontos.cad.dxf", description=__doc__)
    ap.add_argument("file", help="DXF file (read-only)")
    args = ap.parse_args(argv)
    try:
        doc = normalize_dxf(args.file)
    except DxfError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
