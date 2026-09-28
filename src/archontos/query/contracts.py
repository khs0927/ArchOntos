from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from archontos.domain.enums import QueryIntent, ReviewStatus


class QueryClassificationView(BaseModel):
    query: str
    intent: QueryIntent


class EvidenceBasisView(BaseModel):
    assertion_id: UUID
    assertion_review_status: ReviewStatus
    evidence_id: UUID
    evidence_key: str | None = None
    locator: dict[str, Any]
    text_snippet: str | None = None
    artifact_uri: str | None = None
    artifact_hash: str | None = None


class SourceEvidenceView(BaseModel):
    rule_id: UUID
    rule_version_id: UUID
    rule_title: str
    rule_version_label: str
    rule_status: str
    valid_from: date
    valid_to: date | None = None
    authority_class: str
    source_key: str
    source_title: str
    document_type: str
    issuer: str
    jurisdiction_code: str
    source_version_id: UUID
    source_version_label: str
    source_effective_from: date
    source_effective_to: date | None = None
    evidence: list[EvidenceBasisView] = Field(default_factory=list)


class AuthorityClassificationView(BaseModel):
    rule_version_id: UUID
    authority_class: str
    binding: bool
    rule_status: str
    source_key: str
    source_title: str
    document_type: str
    issuer: str
    jurisdiction_code: str


class ApplicabilityEntryView(BaseModel):
    jurisdiction_code: str
    jurisdiction_name: str
    priority: int
    condition: dict[str, Any]


class ApplicabilityView(BaseModel):
    rule_version_id: UUID
    rule_title: str
    rule_status: str
    applicability: dict[str, Any]
    entries: list[ApplicabilityEntryView] = Field(default_factory=list)


class SourceVersionSnapshot(BaseModel):
    source_version_id: UUID
    version_label: str
    effective_from: date
    effective_to: date | None = None
    artifact_hash: str | None = None
    evidence_count: int = 0


class TemporalComparisonView(BaseModel):
    source_key: str
    left_date: date
    right_date: date
    left: SourceVersionSnapshot
    right: SourceVersionSnapshot
    same_source_version: bool
    added_evidence_keys: list[str] = Field(default_factory=list)
    removed_evidence_keys: list[str] = Field(default_factory=list)
    changed_evidence_keys: list[str] = Field(default_factory=list)
    unchanged_evidence_count: int = 0
    # Keys present in both versions where at least one side produced no text
    # hash, so the comparison could not be made. Present so a caller can
    # distinguish "no evidence changed" from "we could not tell".
    indeterminate_evidence_keys: list[str] = Field(default_factory=list)
    comparison_completeness: Literal["complete", "partial"] = "complete"


class JurisdictionRuleSnapshot(BaseModel):
    jurisdiction_code: str
    rule_version_id: UUID
    rule_title: str
    authority_class: str
    binding: bool
    rule_status: str
    valid_from: date
    valid_to: date | None = None
    logic_expr: dict[str, Any]
    condition: dict[str, Any]
    source_key: str
    source_title: str


class JurisdictionComparisonView(BaseModel):
    rule_title: str
    at_date: date
    left: JurisdictionRuleSnapshot | None = None
    right: JurisdictionRuleSnapshot | None = None
    same_logic: bool | None = None


class DecisionProvenanceView(BaseModel):
    decision_id: UUID
    evaluation_id: UUID
    outcome: str
    decided_at: datetime
    evaluated_at: datetime
    object_version_id: UUID | None = None
    inputs: dict[str, Any]
    result: dict[str, Any]
    source_evidence: SourceEvidenceView
