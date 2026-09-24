from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError


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
    compiler_version: str = "safe-requirement-v1"


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

    applicability = dict(spec.applicability)
    requested_jurisdictions = applicability.get("jurisdiction")
    if requested_jurisdictions:
        if not isinstance(requested_jurisdictions, list):
            raise RuleCompilationError("applicability.jurisdiction must be a list")
        if jurisdiction_code not in requested_jurisdictions:
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
    return CompiledRule(
        title=spec.title or natural_language,
        logic_expr=logic_expr,
    )
