from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, model_validator

from archontos.domain.enums import AuthorityClass, DecisionOutcome, ReviewStatus


class SourceDocumentContract(BaseModel):
    title: str = Field(min_length=1)
    issuer: str = Field(min_length=1)
    jurisdiction_code: str = Field(min_length=2)
    document_type: Literal["statute", "regulation", "rule", "ordinance", "standard", "guide"]
    url: HttpUrl | None = None


class SourceVersionContract(BaseModel):
    version_label: str = Field(min_length=1)
    effective_from: date
    effective_to: date | None = None
    status: Literal["published", "draft", "withdrawn"] = "published"
    raw_manifest: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_interval(self):
        if self.effective_to and self.effective_to < self.effective_from:
            raise ValueError("effective_to must be on or after effective_from")
        return self


class EvidenceSpanContract(BaseModel):
    locator: dict[str, Any]
    text_snippet: str | None = None
    extractor_method: Literal["structured-parser", "llm", "human"]
    extraction_confidence: float | None = Field(default=None, ge=0, le=1)


class AssertionContract(BaseModel):
    natural_language: str = Field(min_length=1)
    structured_payload: dict[str, Any] = Field(default_factory=dict)
    interpreter_method: Literal["structured-parser", "llm", "human"]
    interpretation_confidence: float | None = Field(default=None, ge=0, le=1)
    review_status: ReviewStatus = ReviewStatus.UNREVIEWED


class RuleVersionContract(BaseModel):
    version_label: str
    logic_expr: dict[str, Any]
    valid_from: date
    valid_to: date | None = None
    authority_class: AuthorityClass
    binding: bool = True

    @model_validator(mode="after")
    def validate_interval(self):
        if self.valid_to and self.valid_to < self.valid_from:
            raise ValueError("valid_to must be on or after valid_from")
        return self


class RuleEvaluationRequest(BaseModel):
    rule: dict[str, Any]
    facts: dict[str, Any]
    evaluated_at: datetime | None = None


class RuleEvaluationResult(BaseModel):
    applicable: bool
    outcome: DecisionOutcome | None
    reason: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
