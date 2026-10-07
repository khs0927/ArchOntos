from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from archontos.assertions.contracts import AssertionCandidateCreate
from archontos.assertions.identity import assertion_key
from archontos.assertions.review import validate_review_transition
from archontos.domain.enums import ReviewStatus


class AssertionPersistenceError(ValueError):
    pass


class AssertionNotFoundError(AssertionPersistenceError):
    pass


class EvidenceNotFoundError(AssertionPersistenceError):
    pass


@dataclass(frozen=True, slots=True)
class PersistedAssertion:
    assertion_id: UUID
    source_version_id: UUID
    evidence_span_id: UUID
    assertion_key: str
    review_status: ReviewStatus
    created: bool


@dataclass(frozen=True, slots=True)
class PersistedAssertionReview:
    assertion_id: UUID
    previous_status: ReviewStatus
    review_status: ReviewStatus
    reviewer_id: str
    reviewed_at: datetime


class CanonicalAssertionRepository:
    """Canonical assertion candidate and review persistence."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_candidate(
        self,
        payload: AssertionCandidateCreate,
    ) -> PersistedAssertion:
        evidence_result = await self.session.execute(
            text(
                """
                SELECT source_version_id
                FROM evidence_span
                WHERE id = :evidence_span_id
                """
            ),
            {"evidence_span_id": payload.evidence_span_id},
        )
        evidence_row = evidence_result.first()
        if evidence_row is None:
            raise EvidenceNotFoundError(f"unknown evidence_span_id: {payload.evidence_span_id}")

        source_version_id: UUID = evidence_row[0]
        key = assertion_key(
            evidence_span_id=payload.evidence_span_id,
            natural_language=payload.natural_language,
            structured_payload=payload.structured_payload,
            applies_to=[ref.model_dump(mode="json") for ref in payload.applies_to],
        )
        inserted = await self.session.execute(
            text(
                """
                INSERT INTO assertion(
                    source_version_id, evidence_span_id, assertion_key,
                    natural_language, structured_payload_json, applies_to_json,
                    interpreter_method, interpretation_confidence, review_status
                )
                VALUES (
                    :source_version_id, :evidence_span_id, :assertion_key,
                    :natural_language, CAST(:structured_payload_json AS jsonb),
                    CAST(:applies_to_json AS jsonb),
                    :interpreter_method, :interpretation_confidence, 'unreviewed'
                )
                ON CONFLICT (evidence_span_id, assertion_key)
                WHERE assertion_key IS NOT NULL
                DO NOTHING
                RETURNING id
                """
            ),
            {
                "source_version_id": source_version_id,
                "evidence_span_id": payload.evidence_span_id,
                "assertion_key": key,
                "natural_language": payload.natural_language,
                "structured_payload_json": json.dumps(
                    payload.structured_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "applies_to_json": json.dumps(
                    [ref.model_dump(mode="json") for ref in payload.applies_to],
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "interpreter_method": payload.interpreter_method,
                "interpretation_confidence": payload.interpretation_confidence,
            },
        )
        row = inserted.first()
        created = row is not None

        if row is not None:
            assertion_id: UUID = row[0]
            await self._emit_event(
                assertion_id=assertion_id,
                event_type="AssertionCandidateCreated",
                topic="review.assertion-candidate",
                payload={
                    "assertion_id": str(assertion_id),
                    "source_version_id": str(source_version_id),
                    "evidence_span_id": str(payload.evidence_span_id),
                    "assertion_key": key,
                    "review_status": ReviewStatus.UNREVIEWED.value,
                },
            )
            status = ReviewStatus.UNREVIEWED
        else:
            existing = await self.session.execute(
                text(
                    """
                    SELECT id, source_version_id, review_status
                    FROM assertion
                    WHERE evidence_span_id = :evidence_span_id
                      AND assertion_key = :assertion_key
                    """
                ),
                {
                    "evidence_span_id": payload.evidence_span_id,
                    "assertion_key": key,
                },
            )
            existing_row = existing.one()
            assertion_id = existing_row.id
            source_version_id = existing_row.source_version_id
            status = ReviewStatus(existing_row.review_status)

        return PersistedAssertion(
            assertion_id=assertion_id,
            source_version_id=source_version_id,
            evidence_span_id=payload.evidence_span_id,
            assertion_key=key,
            review_status=status,
            created=created,
        )

    async def review(
        self,
        *,
        assertion_id: UUID,
        decision: ReviewStatus,
        reviewer_id: str,
        note: str | None = None,
    ) -> PersistedAssertionReview:
        if decision is ReviewStatus.UNREVIEWED:
            raise AssertionPersistenceError("review decision cannot be unreviewed")

        current = await self.session.execute(
            text(
                """
                SELECT
                    a.id,
                    a.review_status,
                    a.source_version_id,
                    a.evidence_span_id
                FROM assertion a
                JOIN evidence_span e
                  ON e.id = a.evidence_span_id
                 AND e.source_version_id = a.source_version_id
                WHERE a.id = :assertion_id
                FOR UPDATE
                """
            ),
            {"assertion_id": assertion_id},
        )
        row = current.first()
        if row is None:
            raise AssertionNotFoundError(
                f"assertion not found or provenance is invalid: {assertion_id}"
            )

        previous = ReviewStatus(row.review_status)
        validate_review_transition(previous, decision)

        updated = await self.session.execute(
            text(
                """
                UPDATE assertion
                SET review_status = :review_status,
                    reviewer_id = :reviewer_id,
                    review_note = :review_note,
                    reviewed_at = now()
                WHERE id = :assertion_id
                RETURNING reviewed_at
                """
            ),
            {
                "assertion_id": assertion_id,
                "review_status": decision.value,
                "reviewer_id": reviewer_id,
                "review_note": note,
            },
        )
        reviewed_at: datetime = updated.scalar_one()

        await self.session.execute(
            text(
                """
                INSERT INTO assertion_review(
                    assertion_id, previous_status, new_status, reviewer_id, note, reviewed_at
                )
                VALUES (
                    :assertion_id, :previous_status, :new_status,
                    :reviewer_id, :note, :reviewed_at
                )
                """
            ),
            {
                "assertion_id": assertion_id,
                "previous_status": previous.value,
                "new_status": decision.value,
                "reviewer_id": reviewer_id,
                "note": note,
                "reviewed_at": reviewed_at,
            },
        )

        linked_rule_status = "active" if decision is ReviewStatus.APPROVED else "suspended"
        await self.session.execute(
            text(
                """
                UPDATE rule_version rv
                SET status = :rule_status
                FROM rule_assertion ra
                WHERE ra.rule_version_id = rv.id
                  AND ra.assertion_id = :assertion_id
                  AND ra.role = 'basis'
                  AND rv.status <> 'retired'
                """
            ),
            {
                "assertion_id": assertion_id,
                "rule_status": linked_rule_status,
            },
        )

        event_type = {
            ReviewStatus.APPROVED: "AssertionApproved",
            ReviewStatus.REJECTED: "AssertionRejected",
            ReviewStatus.CONTESTED: "AssertionContested",
        }[decision]
        topic = (
            "rules.assertion-approved"
            if decision is ReviewStatus.APPROVED
            else "review.assertion-reviewed"
        )
        await self._emit_event(
            assertion_id=assertion_id,
            event_type=event_type,
            topic=topic,
            payload={
                "assertion_id": str(assertion_id),
                "source_version_id": str(row.source_version_id),
                "evidence_span_id": str(row.evidence_span_id),
                "previous_status": previous.value,
                "review_status": decision.value,
                "reviewer_id": reviewer_id,
                "linked_rule_status": linked_rule_status,
            },
        )

        return PersistedAssertionReview(
            assertion_id=assertion_id,
            previous_status=previous,
            review_status=decision,
            reviewer_id=reviewer_id,
            reviewed_at=reviewed_at,
        )

    async def _emit_event(
        self,
        *,
        assertion_id: UUID,
        event_type: str,
        topic: str,
        payload: dict[str, str],
    ) -> None:
        event_result = await self.session.execute(
            text(
                """
                INSERT INTO domain_event(
                    aggregate_type, aggregate_id, event_type, payload_json
                )
                VALUES (
                    'assertion', :aggregate_id, :event_type,
                    CAST(:payload_json AS jsonb)
                )
                RETURNING event_id
                """
            ),
            {
                "aggregate_id": str(assertion_id),
                "event_type": event_type,
                "payload_json": json.dumps(payload, ensure_ascii=False, sort_keys=True),
            },
        )
        event_id = event_result.scalar_one()
        await self.session.execute(
            text(
                """
                INSERT INTO outbox_message(event_id, topic, payload_json)
                VALUES (
                    :event_id, :topic, CAST(:payload_json AS jsonb)
                )
                """
            ),
            {
                "event_id": event_id,
                "topic": topic,
                "payload_json": json.dumps(payload, ensure_ascii=False, sort_keys=True),
            },
        )
