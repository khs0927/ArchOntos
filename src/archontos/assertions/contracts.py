from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from archontos.domain.enums import ReviewStatus

InterpreterMethod = Literal["structured-parser", "llm", "human"]


class AssertionCandidateCreate(BaseModel):
    evidence_span_id: UUID
    natural_language: str = Field(min_length=1)
    structured_payload: dict[str, Any] = Field(default_factory=dict)
    interpreter_method: InterpreterMethod
    interpretation_confidence: float | None = Field(default=None, ge=0, le=1)


class AssertionCandidateView(BaseModel):
    assertion_id: UUID
    source_version_id: UUID
    evidence_span_id: UUID
    assertion_key: str
    review_status: ReviewStatus
    created: bool


class AssertionReviewRequest(BaseModel):
    decision: Literal["approved", "rejected", "contested"]
    reviewer_id: str = Field(min_length=1)
    note: str | None = None


class AssertionReviewView(BaseModel):
    assertion_id: UUID
    previous_status: ReviewStatus
    review_status: ReviewStatus
    reviewer_id: str
    promotable_to_rule: bool
