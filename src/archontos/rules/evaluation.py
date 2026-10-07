from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from archontos.domain.enums import DecisionOutcome
from archontos.rules.engine import RuleEvaluationError, evaluate_rule


class CanonicalEvaluationError(ValueError):
    pass


class RuleVersionNotFoundError(CanonicalEvaluationError):
    pass


class RuleNotExecutableError(CanonicalEvaluationError):
    pass


@dataclass(frozen=True, slots=True)
class PersistedEvaluation:
    evaluation_id: UUID
    decision_id: UUID | None
    rule_version_id: UUID
    applicable: bool
    outcome: DecisionOutcome | None
    reason: str | None
    details: dict[str, Any]
    evaluated_at: datetime
    binding: bool


class CanonicalEvaluationRepository:
    """Execute an active canonical rule version and persist evaluation/decision."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def evaluate(
        self,
        *,
        rule_version_id: UUID,
        facts: dict[str, Any],
        object_version_id: UUID | None = None,
        evaluated_at: datetime | None = None,
    ) -> PersistedEvaluation:
        effective_evaluated_at = evaluated_at or datetime.now(UTC)
        if effective_evaluated_at.tzinfo is None:
            effective_evaluated_at = effective_evaluated_at.replace(tzinfo=UTC)

        rule_result = await self.session.execute(
            text(
                """
                SELECT
                    rv.logic_expr,
                    rv.status,
                    rv.valid_from,
                    rv.valid_to,
                    rv.binding
                FROM rule_version rv
                WHERE rv.id = :rule_version_id
                FOR UPDATE
                """
            ),
            {"rule_version_id": rule_version_id},
        )
        rule_row = rule_result.first()
        if rule_row is None:
            raise RuleVersionNotFoundError(f"rule version not found: {rule_version_id}")

        if rule_row.status != "active":
            raise RuleNotExecutableError(
                f"rule version {rule_version_id} is {rule_row.status}; only active rules execute"
            )

        evaluated_date = effective_evaluated_at.date()
        if evaluated_date < rule_row.valid_from or (
            rule_row.valid_to is not None and evaluated_date > rule_row.valid_to
        ):
            raise RuleNotExecutableError(
                f"rule version {rule_version_id} is not effective on {evaluated_date.isoformat()}"
            )

        basis_result = await self.session.execute(
            text(
                """
                SELECT
                    COUNT(*) AS basis_count,
                    COUNT(*) FILTER (WHERE a.review_status <> 'approved') AS unsafe_count
                FROM rule_assertion ra
                JOIN assertion a ON a.id = ra.assertion_id
                WHERE ra.rule_version_id = :rule_version_id
                  AND ra.role = 'basis'
                """
            ),
            {"rule_version_id": rule_version_id},
        )
        basis_row = basis_result.one()
        if int(basis_row.basis_count) == 0:
            raise RuleNotExecutableError(
                f"rule version {rule_version_id} has no assertion provenance"
            )
        if int(basis_row.unsafe_count) > 0:
            raise RuleNotExecutableError(
                f"rule version {rule_version_id} has non-approved assertion provenance"
            )

        try:
            result = evaluate_rule(dict(rule_row.logic_expr), facts)
        except RuleEvaluationError as exc:
            raise RuleNotExecutableError(
                f"canonical rule version {rule_version_id} is not executable: {exc}"
            ) from exc

        result_payload = {
            "applicable": result.applicable,
            "outcome": result.outcome.value if result.outcome is not None else None,
            "reason": result.reason,
            "details": result.details,
        }

        evaluation_result = await self.session.execute(
            text(
                """
                INSERT INTO evaluation(
                    rule_version_id, object_version_id, inputs_json,
                    result_json, evaluated_at
                )
                VALUES (
                    :rule_version_id, :object_version_id,
                    CAST(:inputs_json AS jsonb), CAST(:result_json AS jsonb),
                    :evaluated_at
                )
                RETURNING id
                """
            ),
            {
                "rule_version_id": rule_version_id,
                "object_version_id": object_version_id,
                "inputs_json": json.dumps(facts, ensure_ascii=False, sort_keys=True),
                "result_json": json.dumps(
                    result_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "evaluated_at": effective_evaluated_at,
            },
        )
        evaluation_id: UUID = evaluation_result.scalar_one()

        decision_id: UUID | None = None
        if result.applicable and result.outcome is not None:
            decision_result = await self.session.execute(
                text(
                    """
                    INSERT INTO decision(
                        evaluation_id, outcome, rationale_json, decided_at
                    )
                    VALUES (
                        :evaluation_id, :outcome,
                        CAST(:rationale_json AS jsonb), :decided_at
                    )
                    RETURNING id
                    """
                ),
                {
                    "evaluation_id": evaluation_id,
                    "outcome": result.outcome.value,
                    "rationale_json": json.dumps(
                        {
                            "reason": result.reason,
                            "details": result.details,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    "decided_at": effective_evaluated_at,
                },
            )
            decision_id = decision_result.scalar_one()

        event_payload = {
            "evaluation_id": str(evaluation_id),
            "decision_id": str(decision_id) if decision_id is not None else None,
            "rule_version_id": str(rule_version_id),
            "applicable": result.applicable,
            "outcome": result.outcome.value if result.outcome is not None else None,
        }
        event_result = await self.session.execute(
            text(
                """
                INSERT INTO domain_event(
                    aggregate_type, aggregate_id, event_type, payload_json
                )
                VALUES (
                    'evaluation', :aggregate_id, 'RuleEvaluated',
                    CAST(:payload_json AS jsonb)
                )
                RETURNING event_id
                """
            ),
            {
                "aggregate_id": str(evaluation_id),
                "payload_json": json.dumps(event_payload, sort_keys=True),
            },
        )
        event_id: UUID = event_result.scalar_one()
        await self.session.execute(
            text(
                """
                INSERT INTO outbox_message(event_id, topic, payload_json)
                VALUES (
                    :event_id, 'decisions.rule-evaluated',
                    CAST(:payload_json AS jsonb)
                )
                """
            ),
            {
                "event_id": event_id,
                "payload_json": json.dumps(event_payload, sort_keys=True),
            },
        )

        return PersistedEvaluation(
            evaluation_id=evaluation_id,
            decision_id=decision_id,
            rule_version_id=rule_version_id,
            applicable=result.applicable,
            outcome=result.outcome,
            reason=result.reason,
            details=result.details,
            evaluated_at=effective_evaluated_at,
            binding=bool(rule_row.binding),
        )
