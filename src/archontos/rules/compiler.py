from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from archontos.rules.units import UNITS, valid_unit_value


class RuleCompilationError(ValueError):
    pass


class RequirementSpec(BaseModel):
    fact_path: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]+$")
    operator: Literal["==", "!=", ">=", "<=", ">", "<", "in"]
    value: Any
    unit: str | None = None
    title: str | None = None
    pass_reason: str | None = None
    failure_reason: str | None = None
    applicability: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CompiledRule:
    title: str
    logic_expr: dict[str, Any]
    compiler_version: str = "safe-requirement-v2-units"


def authority_from_document_type(document_type: str) -> str:
    mapping = {
        "statute": "statutory",
        "regulation": "regulatory",
        "rule": "regulatory",
        "ordinance": "ordinance",
        "standard": "standard",
        "guide": "guideline",
    }
    try:
        return mapping[document_type]
    except KeyError as exc:
        raise RuleCompilationError(
            f"unsupported source document type for authority: {document_type!r}"
        ) from exc


def compile_requirement(
    *,
    natural_language: str,
    structured_payload: dict[str, Any],
    jurisdiction_code: str,
) -> CompiledRule:
    try:
        spec = RequirementSpec.model_validate(structured_payload)
    except ValidationError as exc:
        raise RuleCompilationError(f"invalid executable requirement payload: {exc}") from exc
    if spec.unit is not None:
        if spec.unit not in UNITS:
            raise RuleCompilationError(f"unsupported requirement unit: {spec.unit!r}")
        if not valid_unit_value(spec.value, spec.unit):
            raise RuleCompilationError(
                "unit-bearing requirements need a finite numeric value; "
                "counts need non-negative integers"
            )

    applicability = dict(spec.applicability)
    if "jurisdiction" in applicability:
        # A present key is an assertion about scope, so it is held to a shape.
        # An absent key means the author did not assert one and the canonical
        # jurisdiction is filled in below. An empty list is not the same thing:
        # it says "resolved to nothing" or "not yet resolved", and treating it as
        # absent silently widens the rule to a jurisdiction nobody asserted.
        requested = applicability["jurisdiction"]
        if not isinstance(requested, list) or not requested:
            raise RuleCompilationError(
                "applicability.jurisdiction must be a non-empty list when present"
            )
        if not all(isinstance(entry, str) and entry for entry in requested):
            # A bare string would also pass a containment test by substring, so
            # "KR" would appear to match "KR-11".
            raise RuleCompilationError("applicability.jurisdiction entries must be strings")
        if jurisdiction_code not in requested:
            raise RuleCompilationError(
                "assertion applicability jurisdiction conflicts with source jurisdiction"
            )
    applicability["jurisdiction"] = [jurisdiction_code]

    required: dict[str, Any] = {
        "operator": spec.operator,
        "value": spec.value,
    }
    if spec.unit:
        required["unit"] = spec.unit

    logic_expr = {
        "applicability": applicability,
        "rule": {
            "if": {
                spec.operator: [
                    {"var": spec.fact_path},
                    {"literal": spec.value},
                ]
            },
            "then": {
                "PASS": {
                    "reason": spec.pass_reason or "requirement satisfied",
                    "required": required,
                    "actual": {"var": spec.fact_path},
                }
            },
            "else": {
                "FAIL": {
                    "reason": spec.failure_reason or natural_language,
                    "required": required,
                    "actual": {"var": spec.fact_path},
                }
            },
        },
    }
    if spec.unit is not None:
        logic_expr["fact_units"] = {spec.fact_path: spec.unit}
    return CompiledRule(
        title=spec.title or natural_language,
        logic_expr=logic_expr,
    )
