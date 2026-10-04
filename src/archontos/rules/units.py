"""Explicit units for numeric rule facts. Values are never silently converted."""

import math
from typing import Any

UNITS = frozenset(
    {
        "count",
        "ratio",
        "%",
        "mm",
        "cm",
        "m",
        "mm2",
        "cm2",
        "m2",
        "mm3",
        "cm3",
        "m3",
        "g",
        "kg",
        "N",
        "kN",
        "Pa",
        "kPa",
        "MPa",
        "deg",
        "rad",
        "kg/m",
        "kN/m",
        "kN/m2",
    }
)


def valid_unit_value(value: Any, unit: str) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        if not math.isfinite(value):
            return False
    except OverflowError:
        return False
    return unit != "count" or (value >= 0 and value == int(value))
