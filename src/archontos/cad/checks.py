"""MVP-2: regulation-to-CAD checks (pure, no I/O).

Three steps, all deterministic and file/JSON based:

1. :func:`extract_fact_space` turns ``arch_object`` rows of one ``cad_revision`` into the rule
   DSL fact space. Every numeric fact carries an explicit unit in ``fact_units`` and the
   objects/entity handles it came from. Lengths are normalised to ``m`` and areas to ``m2``
   here (the only place a unit is converted); the rule engine itself never converts.
2. :func:`select_rules` binds rule versions to a run: only ``active`` versions whose basis
   assertions are all approved, effective on the reference date, in the run's jurisdiction.
3. :func:`run_checks` evaluates every bound rule. A rule that reads ``<kind>.<property>``
   (``door.width``) is evaluated once per object of that kind; a rule over ``drawing.*``,
   ``land_parcel.*`` or caller facts is evaluated once. Missing facts, unit mismatches and
   unexecutable rules give ``REVIEW`` (fail closed), never ``PASS``.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from archontos.domain.enums import DecisionOutcome
from archontos.rules.engine import RuleEvaluationError, evaluate_rule
from archontos.rules.units import UNITS

CHECKER_VERSION = "cad-checks-1.0"

OBJECT_KINDS = ("wall", "door", "window", "space", "stair", "column", "opening")
#: Spaces whose name matches this are also exposed as ``corridor`` objects.
CORRIDOR_NAME = re.compile(r"(복도|통로|corridor|hallway)", re.IGNORECASE)
FACT_KINDS = (*OBJECT_KINDS, "corridor")

_LENGTH_TO_M = {"mm": 0.001, "cm": 0.01, "m": 1.0}
_AREA_TO_M2 = {"mm2": 1e-6, "cm2": 1e-4, "m2": 1.0}

_OUTCOME_RANK = {DecisionOutcome.PASS: 0, DecisionOutcome.REVIEW: 1, DecisionOutcome.FAIL: 2}


class CadCheckInputError(ValueError):
    """The caller-supplied land parcel or facts cannot be used (HTTP 422)."""


def normalize_quantity(quantity: Any) -> tuple[float, str] | None:
    """``{"value": 900, "unit": "mm"}`` -> ``(0.9, "m")``. ``None`` if unusable.

    Lengths become metres and areas square metres; any other known unit is passed through
    unchanged, so the rule engine's unit gate decides (a rule expecting another unit gets
    ``REVIEW``). Unknown units, booleans and non-finite numbers are rejected.
    """
    if not isinstance(quantity, dict):
        return None
    value, unit = quantity.get("value"), quantity.get("unit")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isinstance(unit, str):
        return None
    if not math.isfinite(value) or unit not in UNITS:
        return None
    if unit in _LENGTH_TO_M:
        return round(value * _LENGTH_TO_M[unit], 9), "m"
    if unit in _AREA_TO_M2:
        return round(value * _AREA_TO_M2[unit], 9), "m2"
    return float(value), unit


@dataclass(frozen=True, slots=True)
class ObjectFacts:
    object_id: str
    kind: str
    name: str | None
    handles: tuple[str, ...]
    values: dict[str, float]
    units: dict[str, str]

    def facts(self) -> dict[str, Any]:
        return {self.kind: dict(self.values)}

    def fact_units(self) -> dict[str, str]:
        return {f"{self.kind}.{k}": u for k, u in self.units.items()}


@dataclass(frozen=True, slots=True)
class FactSource:
    object_ids: tuple[str, ...]
    handles: tuple[str, ...]


@dataclass(slots=True)
class CadFactSpace:
    """Drawing-level facts plus per-object facts, with units and provenance."""

    facts: dict[str, Any]
    fact_units: dict[str, str]
    sources: dict[str, FactSource]
    objects: dict[str, list[ObjectFacts]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "facts": self.facts,
            "fact_units": self.fact_units,
            "sources": {
                path: {"object_ids": list(s.object_ids), "entity_handles": list(s.handles)}
                for path, s in sorted(self.sources.items())
            },
        }


def _polygon_extent(geometry: Any) -> tuple[float, float] | None:
    """(short side, long side) in metres of a polygon's bounding box."""
    if not isinstance(geometry, dict) or geometry.get("type") != "polygon":
        return None
    points = geometry.get("coordinates") or []
    if len(points) < 3:
        return None
    xs = [float(p[0]) for p in points]
    ys = [float(p[1]) for p in points]
    dx, dy = max(xs) - min(xs), max(ys) - min(ys)
    return round(min(dx, dy), 9), round(max(dx, dy), 9)


def _object_facts(obj: dict[str, Any], kind: str) -> ObjectFacts:
    values: dict[str, float] = {}
    units: dict[str, str] = {}
    for name, prop in sorted((obj.get("properties") or {}).items()):
        normalized = normalize_quantity(prop)
        if normalized is not None:
            values[name], units[name] = normalized
    if obj["kind"] == "space":
        extent = _polygon_extent(obj.get("geometry"))
        if extent is not None:
            values.setdefault("width", extent[0])
            values.setdefault("length", extent[1])
            units.setdefault("width", "m")
            units.setdefault("length", "m")
    name = (obj.get("properties") or {}).get("name")
    return ObjectFacts(
        object_id=str(obj["id"]),
        kind=kind,
        name=name if isinstance(name, str) else None,
        handles=tuple(obj.get("source_handles") or ()),
        values=values,
        units=units,
    )


def _source(items: list[ObjectFacts]) -> FactSource:
    return FactSource(
        object_ids=tuple(o.object_id for o in items),
        handles=tuple(h for o in items for h in o.handles),
    )


def extract_fact_space(objects: list[dict[str, Any]]) -> CadFactSpace:
    """Fact space of one revision from its ``arch_object`` rows (API/DB shape)."""
    per_kind: dict[str, list[ObjectFacts]] = {kind: [] for kind in FACT_KINDS}
    for obj in sorted(objects, key=lambda o: str(o["id"])):
        if obj.get("kind") not in OBJECT_KINDS:
            continue
        facts = _object_facts(obj, obj["kind"])
        per_kind[obj["kind"]].append(facts)
        if obj["kind"] == "space" and facts.name and CORRIDOR_NAME.search(facts.name):
            per_kind["corridor"].append(_object_facts(obj, "corridor"))

    drawing: dict[str, Any] = {}
    units: dict[str, str] = {}
    sources: dict[str, FactSource] = {}
    for kind, items in per_kind.items():
        path = f"drawing.{kind}_count"
        drawing[f"{kind}_count"] = len(items)
        units[path] = "count"
        sources[path] = _source(items)

    def total(kind: str, prop: str, unit: str) -> None:
        items = [o for o in per_kind[kind] if prop in o.values and o.units[prop] == unit]
        if items:
            path = f"drawing.{kind}_{prop}_total"
            drawing[f"{kind}_{prop}_total"] = round(sum(o.values[prop] for o in items), 9)
            units[path] = unit
            sources[path] = _source(items)

    def minimum(kind: str, prop: str, unit: str) -> None:
        items = [o for o in per_kind[kind] if prop in o.values and o.units[prop] == unit]
        if items:
            low = min(items, key=lambda o: (o.values[prop], o.object_id))
            path = f"drawing.{kind}_min_{prop}"
            drawing[f"{kind}_min_{prop}"] = low.values[prop]
            units[path] = unit
            sources[path] = _source([low])

    total("space", "area", "m2")
    total("wall", "length", "m")
    minimum("space", "area", "m2")
    for kind in ("door", "window", "stair", "corridor"):
        minimum(kind, "width", "m")
    return CadFactSpace(
        facts={"drawing": drawing},
        fact_units=units,
        sources=sources,
        objects=per_kind,
    )


def land_parcel_facts(land_parcel: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, str]]:
    """Normalise a land parcel (site-pipeline ``parcel_facts`` / Ontology evaluate shape).

    Accepted keys: ``pnu``, ``zone`` (용도지역), ``districts`` (용도지구/구역 list) and the
    site area as ``site_area: {"value", "unit"}``, ``site_area_m2`` or ``area_m2``.
    """
    if not land_parcel:
        return {}, {}
    if not isinstance(land_parcel, dict):
        raise CadCheckInputError("land_parcel must be an object")
    out: dict[str, Any] = {}
    units: dict[str, str] = {}
    for key in ("pnu", "zone"):
        value = land_parcel.get(key)
        if value is not None:
            if not isinstance(value, str) or not value.strip():
                raise CadCheckInputError(f"land_parcel.{key} must be a non-empty string")
            out[key] = value.strip()
    districts = land_parcel.get("districts")
    if districts is not None:
        if not isinstance(districts, list) or not all(
            isinstance(d, str) and d.strip() for d in districts
        ):
            raise CadCheckInputError("land_parcel.districts must be a list of strings")
        out["districts"] = sorted({d.strip() for d in districts})
    area: Any = land_parcel.get("site_area")
    if area is None:
        for key in ("site_area_m2", "area_m2"):
            if land_parcel.get(key) is not None:
                area = {"value": land_parcel[key], "unit": "m2"}
                break
    if area is not None:
        normalized = normalize_quantity(area)
        if normalized is None or normalized[1] != "m2" or normalized[0] <= 0:
            raise CadCheckInputError("land_parcel site area must be a positive area quantity")
        out["site_area"] = normalized[0]
        units["land_parcel.site_area"] = "m2"
    return ({"land_parcel": out} if out else {}), units


def _flatten(prefix: str, value: Any) -> list[str]:
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in _flatten(f"{prefix}.{k}", v)]
    return [prefix]


def build_base_facts(
    space: CadFactSpace,
    *,
    jurisdiction: str,
    reference_date: date,
    land_parcel: dict[str, Any] | None = None,
    extra_facts: dict[str, Any] | None = None,
    extra_fact_units: dict[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Drawing facts + land parcel + caller facts + context, refusing to override CAD facts."""
    facts: dict[str, Any] = {k: dict(v) for k, v in space.facts.items()}
    units = dict(space.fact_units)
    parcel, parcel_units = land_parcel_facts(land_parcel)
    extra = dict(extra_facts or {})
    for reserved in ("fact_units", "context", "drawing", *FACT_KINDS):
        if reserved in extra:
            raise CadCheckInputError(f"facts.{reserved} is derived from the drawing/run")
    if "land_parcel" in extra and parcel:
        raise CadCheckInputError("give land_parcel either as land_parcel or in facts, not both")
    extra_units = dict(extra_fact_units or {})
    for path, unit in extra_units.items():
        if not isinstance(path, str) or unit not in UNITS:
            raise CadCheckInputError(f"invalid fact unit for {path!r}: {unit!r}")
        if path.split(".", 1)[0] not in extra:
            raise CadCheckInputError(f"fact_units names {path!r}, which is not in facts")
    facts.update(parcel)
    facts.update(extra)
    units.update(parcel_units)
    units.update(extra_units)
    facts["context"] = {"jurisdiction": jurisdiction, "reference_date": reference_date.isoformat()}
    return facts, units


@dataclass(frozen=True, slots=True)
class CheckRule:
    """An executable rule version with everything a result must report."""

    rule_version_id: str
    title: str
    logic_expr: dict[str, Any]
    jurisdiction_code: str
    valid_from: date
    valid_to: date | None
    status: str = "active"
    binding: bool = True
    basis_approved: bool = True
    evidence_locators: tuple[dict[str, Any], ...] = ()


def jurisdiction_covers(rule_jurisdiction: str, run_jurisdiction: str) -> bool:
    """``KR`` rules apply to a ``KR-11`` run; ``KR-11`` rules do not apply to a ``KR`` run."""
    return run_jurisdiction == rule_jurisdiction or run_jurisdiction.startswith(
        rule_jurisdiction + "-"
    )


def select_rules(
    rules: list[CheckRule], *, jurisdiction: str, reference_date: date
) -> tuple[list[CheckRule], list[dict[str, str]]]:
    """Bound rules (sorted by id) and the reasons the others were skipped."""
    bound: list[CheckRule] = []
    skipped: list[dict[str, str]] = []
    for rule in sorted(rules, key=lambda r: r.rule_version_id):
        if rule.status != "active":
            reason = f"status {rule.status}"
        elif not rule.basis_approved:
            reason = "basis assertions not all approved"
        elif reference_date < rule.valid_from or (
            rule.valid_to is not None and reference_date > rule.valid_to
        ):
            reason = f"not effective on {reference_date.isoformat()}"
        elif not jurisdiction_covers(rule.jurisdiction_code, jurisdiction):
            reason = f"jurisdiction {rule.jurisdiction_code}"
        else:
            bound.append(rule)
            continue
        skipped.append({"rule_version_id": rule.rule_version_id, "reason": reason})
    return bound, skipped


def referenced_paths(expr: Any) -> set[str]:
    """Every ``var`` path in a rule document (rule body, scope, exceptions, fact_units)."""
    found: set[str] = set()
    if isinstance(expr, dict):
        if set(expr) == {"var"}:
            var = expr["var"]
            path = var[0] if isinstance(var, list) and var else var
            if isinstance(path, str):
                found.add(path)
            return found
        for key, value in expr.items():
            if key == "fact_units" and isinstance(value, dict):
                found.update(p for p in value if isinstance(p, str))
            else:
                found |= referenced_paths(value)
    elif isinstance(expr, list):
        for item in expr:
            found |= referenced_paths(item)
    return found


@dataclass(slots=True)
class CheckResult:
    rule: CheckRule
    applicable: bool
    outcome: DecisionOutcome | None
    reason: str | None
    details: dict[str, Any] = field(default_factory=dict)
    fact_paths: tuple[str, ...] = ()
    object_ids: tuple[str, ...] = ()
    handles: tuple[str, ...] = ()


def _evaluate(logic: dict[str, Any], facts: dict[str, Any]) -> tuple[Any, ...]:
    try:
        result = evaluate_rule(logic, facts)
    except RuleEvaluationError as exc:
        return True, DecisionOutcome.REVIEW, "Rule is not executable", {"error": str(exc)}
    return result.applicable, result.outcome, result.reason, result.details


def _dedupe(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def check_rule(
    rule: CheckRule, space: CadFactSpace, base_facts: dict[str, Any], base_units: dict[str, str]
) -> CheckResult:
    paths = referenced_paths(rule.logic_expr)
    kinds = sorted({p.split(".", 1)[0] for p in paths} & set(FACT_KINDS))
    fact_paths = tuple(sorted(paths))
    if len(kinds) > 1:
        return CheckResult(
            rule,
            True,
            DecisionOutcome.REVIEW,
            "Rule reads several object kinds; pairing objects is not supported",
            {"kinds": kinds},
            fact_paths,
        )
    if not kinds:
        facts = {**base_facts, "fact_units": dict(base_units)}
        applicable, outcome, reason, details = _evaluate(rule.logic_expr, facts)
        object_ids: list[str] = []
        handles: list[str] = []
        if outcome in (DecisionOutcome.FAIL, DecisionOutcome.REVIEW):
            for path in fact_paths:
                source = space.sources.get(path)
                if source is not None:
                    object_ids += source.object_ids
                    handles += source.handles
        return CheckResult(
            rule,
            applicable,
            outcome,
            reason,
            dict(details or {}),
            fact_paths,
            _dedupe(object_ids),
            _dedupe(handles),
        )

    kind = kinds[0]
    items = space.objects.get(kind, [])
    if not items:
        return CheckResult(
            rule,
            True,
            DecisionOutcome.REVIEW,
            f"Insufficient facts: the drawing has no {kind} objects",
            {"missing_kind": kind},
            fact_paths,
        )
    per_object: list[dict[str, Any]] = []
    for item in items:
        facts = {**base_facts, **item.facts(), "fact_units": {**base_units, **item.fact_units()}}
        applicable, outcome, reason, details = _evaluate(rule.logic_expr, facts)
        per_object.append(
            {
                "object_id": item.object_id,
                "name": item.name,
                "entity_handles": list(item.handles),
                "applicable": applicable,
                "outcome": outcome.value if outcome is not None else None,
                "reason": reason,
                "details": details or {},
            }
        )
    decided = [p for p in per_object if p["applicable"] and p["outcome"] is not None]
    if not decided:
        return CheckResult(rule, False, None, "Rule not applicable", {}, fact_paths)
    worst = max(decided, key=lambda p: _OUTCOME_RANK[DecisionOutcome(p["outcome"])])
    outcome = DecisionOutcome(worst["outcome"])
    offending = (
        [] if outcome is DecisionOutcome.PASS else [p for p in decided if p["outcome"] == outcome]
    )
    counts = {o.value: sum(1 for p in decided if p["outcome"] == o.value) for o in DecisionOutcome}
    return CheckResult(
        rule,
        True,
        outcome,
        worst["reason"] if outcome is not DecisionOutcome.PASS else None,
        {"kind": kind, "object_outcomes": counts, "objects": per_object},
        fact_paths,
        _dedupe([p["object_id"] for p in offending]),
        _dedupe([h for p in offending for h in p["entity_handles"]]),
    )


@dataclass(slots=True)
class CheckRun:
    jurisdiction: str
    reference_date: date
    facts: dict[str, Any]
    fact_units: dict[str, str]
    results: list[CheckResult]
    skipped: list[dict[str, str]]

    def summary(self) -> dict[str, int]:
        applicable = [r for r in self.results if r.applicable and r.outcome is not None]
        return {
            "rule_count": len(self.results),
            "pass_count": sum(1 for r in applicable if r.outcome is DecisionOutcome.PASS),
            "fail_count": sum(1 for r in applicable if r.outcome is DecisionOutcome.FAIL),
            "review_count": sum(1 for r in applicable if r.outcome is DecisionOutcome.REVIEW),
            "not_applicable_count": len(self.results) - len(applicable),
        }


def run_checks(
    rules: list[CheckRule],
    space: CadFactSpace,
    *,
    jurisdiction: str,
    reference_date: date,
    land_parcel: dict[str, Any] | None = None,
    extra_facts: dict[str, Any] | None = None,
    extra_fact_units: dict[str, str] | None = None,
) -> CheckRun:
    base_facts, base_units = build_base_facts(
        space,
        jurisdiction=jurisdiction,
        reference_date=reference_date,
        land_parcel=land_parcel,
        extra_facts=extra_facts,
        extra_fact_units=extra_fact_units,
    )
    bound, skipped = select_rules(rules, jurisdiction=jurisdiction, reference_date=reference_date)
    results = [check_rule(rule, space, base_facts, base_units) for rule in bound]
    return CheckRun(jurisdiction, reference_date, base_facts, base_units, results, skipped)
