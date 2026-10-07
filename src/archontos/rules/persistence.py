from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from archontos.domain.enums import ReviewStatus
from archontos.rules.compiler import (
    RuleCompilationError,
    authority_from_document_type,
    compile_requirement,
)


class RulePersistenceError(ValueError):
    pass


class AssertionNotApprovedError(RulePersistenceError):
    pass


class RuleAssertionNotFoundError(RulePersistenceError):
    pass


@dataclass(frozen=True, slots=True)
class PersistedCompiledRule:
    rule_id: UUID
    rule_version_id: UUID
    assertion_id: UUID
    status: str
    created: bool


class CanonicalRuleCompilerRepository:
    """Compile approved assertions into constrained executable rule versions."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def compile_approved_assertion(
        self,
        assertion_id: UUID,
    ) -> PersistedCompiledRule:
        result = await self.session.execute(
            text(
                """
                SELECT
                    a.id,
                    a.review_status,
                    a.natural_language,
                    a.structured_payload_json,
                    a.source_version_id,
                    sv.version_label AS source_version_label,
                    sv.effective_from,
                    sv.effective_to,
                    sd.document_type,
                    sd.jurisdiction_code
                FROM assertion a
                JOIN evidence_span e
                  ON e.id = a.evidence_span_id
                 AND e.source_version_id = a.source_version_id
                JOIN source_version sv
                  ON sv.id = a.source_version_id
                JOIN source_document sd
                  ON sd.id = sv.source_id
                WHERE a.id = :assertion_id
                FOR UPDATE OF a
                """
            ),
            {"assertion_id": assertion_id},
        )
        row = result.first()
        if row is None:
            raise RuleAssertionNotFoundError(
                f"assertion not found or provenance is invalid: {assertion_id}"
            )
        if ReviewStatus(row.review_status) is not ReviewStatus.APPROVED:
            raise AssertionNotApprovedError(
                f"assertion {assertion_id} is {row.review_status}; only approved may compile"
            )

        try:
            compiled = compile_requirement(
                natural_language=row.natural_language,
                structured_payload=dict(row.structured_payload_json),
                jurisdiction_code=row.jurisdiction_code,
            )
            authority_class = authority_from_document_type(row.document_type)
        except RuleCompilationError:
            raise

        rule_key = f"assertion:{assertion_id}"
        inserted_rule = await self.session.execute(
            text(
                """
                INSERT INTO rule(
                    source_version_id, rule_key, title,
                    jurisdiction_scope_json, applicability_json
                )
                VALUES (
                    :source_version_id, :rule_key, :title,
                    CAST(:jurisdiction_scope_json AS jsonb),
                    CAST(:applicability_json AS jsonb)
                )
                ON CONFLICT (source_version_id, rule_key)
                WHERE rule_key IS NOT NULL
                DO NOTHING
                RETURNING id
                """
            ),
            {
                "source_version_id": row.source_version_id,
                "rule_key": rule_key,
                "title": compiled.title,
                "jurisdiction_scope_json": json.dumps(
                    {"jurisdiction": [row.jurisdiction_code]},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "applicability_json": json.dumps(
                    compiled.logic_expr.get("applicability", {}),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            },
        )
        rule_row = inserted_rule.first()
        if rule_row is not None:
            rule_id: UUID = rule_row[0]
        else:
            existing_rule = await self.session.execute(
                text(
                    """
                    SELECT id
                    FROM rule
                    WHERE source_version_id = :source_version_id
                      AND rule_key = :rule_key
                    """
                ),
                {
                    "source_version_id": row.source_version_id,
                    "rule_key": rule_key,
                },
            )
            rule_id = existing_rule.scalar_one()

        version_label = (
            f"{row.source_version_label}:assertion:{assertion_id}:{compiled.compiler_version}"
        )
        inserted_version = await self.session.execute(
            text(
                """
                INSERT INTO rule_version(
                    rule_id, version_label, logic_expr, valid_from, valid_to,
                    authority_class, binding, status, compiler_version, compiled_at
                )
                VALUES (
                    :rule_id, :version_label, CAST(:logic_expr AS jsonb), :valid_from, :valid_to,
                    :authority_class, true, 'active', :compiler_version, now()
                )
                ON CONFLICT (rule_id, version_label) DO NOTHING
                RETURNING id, status
                """
            ),
            {
                "rule_id": rule_id,
                "version_label": version_label,
                "logic_expr": json.dumps(
                    compiled.logic_expr,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "valid_from": row.effective_from,
                "valid_to": row.effective_to,
                "authority_class": authority_class,
                "compiler_version": compiled.compiler_version,
            },
        )
        version_row = inserted_version.first()
        version_created = version_row is not None
        if version_row is not None:
            rule_version_id: UUID = version_row[0]
            status = version_row[1]
        else:
            existing_version = await self.session.execute(
                text(
                    """
                    SELECT id, status
                    FROM rule_version
                    WHERE rule_id = :rule_id
                      AND version_label = :version_label
                    """
                ),
                {"rule_id": rule_id, "version_label": version_label},
            )
            existing_version_row = existing_version.one()
            rule_version_id = existing_version_row.id
            status = existing_version_row.status
            if status == "suspended":
                # The conflict check is keyed on version identity, not logic, so a
                # re-compile of a version that a later review suspended hits it
                # and returned the stale status as a success. Suspension is driven
                # by the assertion review lifecycle, and this method already
                # refuses anything that is not APPROVED, so reaching here means
                # the assertion is approved again and the version is executable
                # again. Leaving it suspended made a re-approved rule
                # permanently non-executable until someone re-called the review
                # endpoint.
                await self.session.execute(
                    text(
                        """
                        UPDATE rule_version
                        SET status = 'active'
                        WHERE id = :rule_version_id
                        """
                    ),
                    {"rule_version_id": rule_version_id},
                )
                status = "active"

        if version_created:
            await self.session.execute(
                text(
                    """
                    UPDATE rule_version
                    SET status = 'retired'
                    WHERE rule_id = :rule_id
                      AND id <> :rule_version_id
                      AND status <> 'retired'
                    """
                ),
                {
                    "rule_id": rule_id,
                    "rule_version_id": rule_version_id,
                },
            )

        await self.session.execute(
            text(
                """
                INSERT INTO rule_assertion(rule_version_id, assertion_id, role, ordinal)
                VALUES (:rule_version_id, :assertion_id, 'basis', 0)
                ON CONFLICT (rule_version_id, assertion_id, role) DO NOTHING
                """
            ),
            {
                "rule_version_id": rule_version_id,
                "assertion_id": assertion_id,
            },
        )

        jurisdiction_result = await self.session.execute(
            text("SELECT id FROM jurisdiction WHERE code = :code"),
            {"code": row.jurisdiction_code},
        )
        jurisdiction_row = jurisdiction_result.first()
        if jurisdiction_row is None:
            raise RulePersistenceError(f"unknown canonical jurisdiction: {row.jurisdiction_code}")

        await self.session.execute(
            text(
                """
                INSERT INTO applicability(
                    rule_version_id, jurisdiction_id, condition_expr, priority
                )
                VALUES (
                    :rule_version_id, :jurisdiction_id,
                    CAST(:condition_expr AS jsonb), 0
                )
                ON CONFLICT (rule_version_id, jurisdiction_id, priority)
                DO UPDATE SET condition_expr = EXCLUDED.condition_expr
                """
            ),
            {
                "rule_version_id": rule_version_id,
                "jurisdiction_id": jurisdiction_row.id,
                "condition_expr": json.dumps(
                    compiled.logic_expr.get("applicability", {}),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            },
        )

        if version_created:
            event_payload = {
                "rule_id": str(rule_id),
                "rule_version_id": str(rule_version_id),
                "assertion_id": str(assertion_id),
                "source_version_id": str(row.source_version_id),
                "compiler_version": compiled.compiler_version,
                "status": status,
            }
            event_result = await self.session.execute(
                text(
                    """
                    INSERT INTO domain_event(
                        aggregate_type, aggregate_id, event_type, payload_json
                    )
                    VALUES (
                        'rule_version', :aggregate_id, 'RuleCompiled',
                        CAST(:payload_json AS jsonb)
                    )
                    RETURNING event_id
                    """
                ),
                {
                    "aggregate_id": str(rule_version_id),
                    "payload_json": json.dumps(
                        event_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                },
            )
            event_id = event_result.scalar_one()
            await self.session.execute(
                text(
                    """
                    INSERT INTO outbox_message(event_id, topic, payload_json)
                    VALUES (
                        :event_id, 'rules.rule-compiled',
                        CAST(:payload_json AS jsonb)
                    )
                    """
                ),
                {
                    "event_id": event_id,
                    "payload_json": json.dumps(
                        event_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                },
            )

        return PersistedCompiledRule(
            rule_id=rule_id,
            rule_version_id=rule_version_id,
            assertion_id=assertion_id,
            status=status,
            created=version_created,
        )
