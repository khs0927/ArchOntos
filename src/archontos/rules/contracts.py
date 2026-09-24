from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from archontos.domain.enums import DecisionOutcome


class RuleCompilationView(BaseModel):
    rule_id: UUID
    rule_version_id: UUID
    assertion_id: UUID
    status: str
    created: bool


class CanonicalEvaluationRequest(BaseModel):
    facts: dict[str, Any] = Field(default_factory=dict)
    object_version_id: UUID | None = None
    evaluated_at: datetime | None = None


class CanonicalEvaluationView(BaseModel):
    evaluation_id: UUID
    decision_id: UUID | None = None
    rule_version_id: UUID
    applicable: bool
    outcome: DecisionOutcome | None = None
    reason: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    evaluated_at: datetime
    binding: bool
