"""Building / storey facts from DXF title blocks (P2).

A drawing's title block (도곽 / 표제란) usually states the building use (주용도), the number of
storeys (지상 N층 / 지하 M층) and which storey the sheet shows (``1층 평면도``). This module reads
those values from

* INSERT attributes (block ``ATTRIB`` tag -> value) whose tag matches a field's tag pattern, on
  any layer (title blocks are blocks), and
* TEXT / MTEXT on the configured title-block layers, matched with a per-field regex that has a
  named group ``v``.

Rules are versioned like the layer mapping (:class:`BuildingInfoRuleSet.version`); the version is
recorded with the extracted values in ``model_meta["building_info"]``.

Fail-closed: a field without any source yields **no** value (so a rule that needs
``building.use_group`` evaluates to REVIEW, never PASS by default). Two sources that disagree
(e.g. two title blocks with different floor counts) also yield no value; the conflict is recorded
in ``conflicts`` so a reviewer can see why the fact is missing. Pure: no ezdxf, no database.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

FIELDS = ("use_group", "floor_count", "basement_count", "storey_label")
INT_FIELDS = frozenset({"floor_count", "basement_count"})


class BuildingRuleError(ValueError):
    pass


def _compile(pattern: str, where: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise BuildingRuleError(f"{where}: invalid regex: {exc}") from exc


@dataclass(frozen=True, slots=True)
class BuildingInfoRuleSet:
    version: str
    #: Layers whose TEXT / MTEXT is searched with ``text_patterns``.
    text_layer_pattern: str
    #: field -> regex over the (upper-cased, stripped) INSERT attribute tag.
    attribute_tags: dict[str, str]
    #: field -> regex with a named group ``v`` over TEXT / MTEXT content.
    text_patterns: dict[str, str]
    _compiled: dict[str, re.Pattern[str]] = field(
        default_factory=dict, init=False, compare=False, repr=False
    )

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", self.version):
            raise BuildingRuleError("rule-set version must match [A-Za-z0-9._-]{1,64}")
        compiled = {"__layer__": _compile(self.text_layer_pattern, "text_layer_pattern")}
        for name, pattern in self.attribute_tags.items():
            if name not in FIELDS:
                raise BuildingRuleError(f"unknown field {name!r}")
            compiled[f"tag:{name}"] = _compile(pattern, f"attribute_tags.{name}")
        for name, pattern in self.text_patterns.items():
            if name not in FIELDS:
                raise BuildingRuleError(f"unknown field {name!r}")
            rx = _compile(pattern, f"text_patterns.{name}")
            if "v" not in rx.groupindex:
                raise BuildingRuleError(f"text_patterns.{name}: needs a named group 'v'")
            compiled[f"text:{name}"] = rx
        object.__setattr__(self, "_compiled", compiled)

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "text_layer_pattern": self.text_layer_pattern,
            "attribute_tags": dict(sorted(self.attribute_tags.items())),
            "text_patterns": dict(sorted(self.text_patterns.items())),
        }

    def fingerprint(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, ensure_ascii=False)


def building_rule_set_from_dict(data: dict[str, Any]) -> BuildingInfoRuleSet:
    try:
        return BuildingInfoRuleSet(
            version=str(data["version"]),
            text_layer_pattern=str(data["text_layer_pattern"]),
            attribute_tags={
                str(k): str(v) for k, v in dict(data.get("attribute_tags", {})).items()
            },
            text_patterns={str(k): str(v) for k, v in dict(data.get("text_patterns", {})).items()},
        )
    except (KeyError, TypeError) as exc:
        raise BuildingRuleError(f"malformed building rule set: {exc}") from exc


DEFAULT_BUILDING_RULES = BuildingInfoRuleSet(
    version="kr-titleblock-2026.11",
    text_layer_pattern=r"(?i)^(A-ANNO-TTLB|A-TTLB|TITLE|TTLB|TB|도곽|표제)",
    attribute_tags={
        "use_group": r"^(USE|BLDG_?USE|USE_?GROUP|MAIN_?USE|용도|주용도|건축물용도)$",
        "floor_count": r"^(FLOORS?|FLOOR_?COUNT|STOREYS?|ABOVE_?GROUND|층수|지상층수|지상)$",
        "basement_count": r"^(BASEMENTS?|BASEMENT_?COUNT|BELOW_?GROUND|지하층수|지하)$",
        "storey_label": r"^(LEVEL|STOREY_?NAME|FLOOR_?NAME|SHEET_?LEVEL|해당층|층명)$",
    },
    text_patterns={
        "use_group": r"(?:주\s*용도|건축물\s*용도|용\s*도)\s*[:：]?\s*(?P<v>[^\n,/|()]+)",
        "floor_count": r"지상\s*(?P<v>\d{1,3})\s*층",
        "basement_count": r"지하\s*(?P<v>\d{1,2})\s*층",
        "storey_label": r"(?P<v>(?:지하\s*\d{1,2}\s*층|지상\s*\d{1,3}\s*층|\d{1,3}\s*층|B\d{1,2}F?|"
        r"\d{1,3}F|옥탑\s*층?))\s*평\s*면\s*도",
    },
)

_INT = re.compile(r"\d{1,3}")
_NO_BASEMENT = re.compile(r"^\s*(없음|-|0|N/?A|NONE)\s*$", re.IGNORECASE)


def _normalise(name: str, raw: str) -> str | int | None:
    value = " ".join(str(raw).split())
    if not value:
        return None
    if name in INT_FIELDS:
        if name == "basement_count" and _NO_BASEMENT.match(value):
            return 0
        keyword = "지상" if name == "floor_count" else "지하"
        m = re.search(keyword + r"\s*(\d{1,3})", value) or _INT.search(value)
        return int(m.group(0) if m.lastindex is None else m.group(1)) if m else None
    if name == "storey_label":
        return re.sub(r"\s+", "", value).upper()
    return value


def extract_building_info(
    entities: Any, rules: BuildingInfoRuleSet = DEFAULT_BUILDING_RULES
) -> dict[str, Any]:
    """Extract title-block values from parsed DXF entities (``CadEntity``-like objects).

    Returns ``{"rule_set_version", "values", "sources", "conflicts"}``; ``values`` only holds
    fields with exactly one distinct value. Deterministic for identical entities.
    """
    found: dict[str, dict[str | int, list[str]]] = {name: {} for name in FIELDS}
    layer_rx = rules._compiled["__layer__"]
    for ent in entities:
        geometry = ent.geometry or {}
        if ent.entity_type == "INSERT":
            for tag, raw in (geometry.get("attributes") or {}).items():
                for name in FIELDS:
                    rx = rules._compiled.get(f"tag:{name}")
                    if rx is not None and rx.search(str(tag).strip().upper()):
                        value = _normalise(name, raw)
                        if value is not None:
                            found[name].setdefault(value, []).append(f"{ent.handle}:{tag}")
        elif ent.entity_type in ("TEXT", "MTEXT") and layer_rx.search(ent.layer):
            content = str(geometry.get("text", ""))
            for name in FIELDS:
                rx = rules._compiled.get(f"text:{name}")
                if rx is None:
                    continue
                for m in rx.finditer(content):
                    value = _normalise(name, m.group("v"))
                    if value is not None:
                        found[name].setdefault(value, []).append(ent.handle)
    values: dict[str, Any] = {}
    sources: dict[str, list[str]] = {}
    conflicts: list[dict[str, Any]] = []
    for name in FIELDS:
        options = found[name]
        if len(options) == 1:
            ((value, handles),) = options.items()
            values[name] = value
            sources[name] = sorted(set(handles))
        elif len(options) > 1:
            conflicts.append({"field": name, "values": sorted((str(v) for v in options), key=str)})
    return {
        "rule_set_version": rules.version,
        "values": values,
        "sources": sources,
        "conflicts": conflicts,
    }


def building_info_facts(info: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, str]]:
    """Facts from ``model_meta["building_info"]``: ``building.*`` and ``sheet.storey_label``.

    Only numeric facts get a unit (``count``); strings have none. Missing -> nothing (REVIEW).
    """
    values = (info or {}).get("values") or {}
    building: dict[str, Any] = {}
    units: dict[str, str] = {}
    for name in ("use_group", "floor_count", "basement_count"):
        if name in values:
            building[name] = values[name]
            if name in INT_FIELDS:
                units[f"building.{name}"] = "count"
    facts: dict[str, Any] = {}
    if building:
        facts["building"] = building
    if "storey_label" in values:
        facts["sheet"] = {"storey_label": values["storey_label"]}
    return facts, units
