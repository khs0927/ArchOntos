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
    evidence_key: str | None = Field(default=None, min_length=1)
    locator: dict[str, Any]
    text_snippet: str | None = None
    normalized_text_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    extractor_method: Literal["structured-parser", "llm", "human"]
    extraction_confidence: float | None = Field(default=None, ge=0, le=1)


class AecSubjectRef(BaseModel):
    """Stable reference from a legal assertion to an Ontology AEC subject."""

    schema: Literal["archontos-aec-subject-ref/1"] = "archontos-aec-subject-ref/1"
    project_id: str = Field(min_length=1)
    object_id: str | None = Field(default=None, min_length=1)
    source_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_byte_revision_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    parser_revision_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    locator: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_revision_identity(self):
        revision_fields = (
            self.source_id,
            self.source_byte_revision_id,
            self.parser_revision_id,
        )
        if any(value is not None for value in revision_fields) and not all(
            value is not None for value in revision_fields
        ):
            raise ValueError(
                "source_id, source_byte_revision_id and parser_revision_id "
                "must be supplied together"
            )
        return self


class AssertionContract(BaseModel):
    natural_language: str = Field(min_length=1)
    structured_payload: dict[str, Any] = Field(default_factory=dict)
    applies_to: list[AecSubjectRef] = Field(default_factory=list, max_length=10_000)
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
    status: Literal["draft", "active", "suspended", "retired"] = "active"

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
