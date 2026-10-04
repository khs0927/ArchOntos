from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from archontos.domain.enums import ReviewStatus
from archontos.query.contracts import (
    ApplicabilityEntryView,
    ApplicabilityView,
    AuthorityClassificationView,
    DecisionProvenanceView,
    EvidenceBasisView,
    JurisdictionComparisonView,
    JurisdictionRuleSnapshot,
    SourceEvidenceView,
    SourceVersionSnapshot,
    TemporalComparisonView,
)
from archontos.query.diff import diff_evidence_hashes


class CanonicalQueryError(ValueError):
    pass


class CanonicalQueryNotFound(CanonicalQueryError):
    pass


class CanonicalQueryRepository:
    """Read-only canonical query paths for the five MVP-0 intents."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def source_evidence(self, rule_version_id: UUID) -> SourceEvidenceView:
        result = await self.session.execute(
            text(
                """
                SELECT
                    r.id AS rule_id,
                    rv.id AS rule_version_id,
                    r.title AS rule_title,
                    rv.version_label AS rule_version_label,
                    rv.status AS rule_status,
                    rv.valid_from,
                    rv.valid_to,
                    rv.authority_class,
                    sd.source_key,
                    sd.title AS source_title,
                    sd.document_type,
                    sd.issuer,
                    sd.jurisdiction_code,
                    sv.id AS source_version_id,
                    sv.version_label AS source_version_label,
                    sv.effective_from AS source_effective_from,
                    sv.effective_to AS source_effective_to,
                    a.id AS assertion_id,
                    a.review_status AS assertion_review_status,
                    a.applies_to_json,
                    e.id AS evidence_id,
                    e.evidence_key,
                    e.locator_json,
                    e.text_snippet,
                    art.storage_uri AS artifact_uri,
                    art.content_hash AS artifact_hash
                FROM rule_version rv
                JOIN rule r ON r.id = rv.rule_id
                JOIN source_version sv ON sv.id = r.source_version_id
                JOIN source_document sd ON sd.id = sv.source_id
                JOIN rule_assertion ra ON ra.rule_version_id = rv.id
                JOIN assertion a ON a.id = ra.assertion_id
                JOIN evidence_span e
                  ON e.id = a.evidence_span_id
                 AND e.source_version_id = a.source_version_id
                LEFT JOIN artifact art ON art.id = e.artifact_id
                WHERE rv.id = :rule_version_id
                ORDER BY ra.ordinal, e.evidence_key NULLS LAST
                """
            ),
            {"rule_version_id": rule_version_id},
        )
        rows = result.all()
        if not rows:
            raise CanonicalQueryNotFound(f"rule version or provenance not found: {rule_version_id}")

        first = rows[0]
        evidence = [
            EvidenceBasisView(
                assertion_id=row.assertion_id,
                assertion_review_status=ReviewStatus(row.assertion_review_status),
                evidence_id=row.evidence_id,
                evidence_key=row.evidence_key,
                locator=dict(row.locator_json),
                text_snippet=row.text_snippet,
                artifact_uri=row.artifact_uri,
                artifact_hash=row.artifact_hash,
                applies_to=list(row.applies_to_json or []),
            )
            for row in rows
        ]
        return SourceEvidenceView(
            rule_id=first.rule_id,
            rule_version_id=first.rule_version_id,
            rule_title=first.rule_title,
            rule_version_label=first.rule_version_label,
            rule_status=first.rule_status,
            valid_from=first.valid_from,
            valid_to=first.valid_to,
            authority_class=first.authority_class,
            source_key=first.source_key,
            source_title=first.source_title,
            document_type=first.document_type,
            issuer=first.issuer,
            jurisdiction_code=first.jurisdiction_code,
            source_version_id=first.source_version_id,
            source_version_label=first.source_version_label,
            source_effective_from=first.source_effective_from,
            source_effective_to=first.source_effective_to,
            evidence=evidence,
        )

    async def decision_provenance(
        self,
        decision_id: UUID,
    ) -> DecisionProvenanceView:
        result = await self.session.execute(
            text(
                """
                SELECT
                    d.id AS decision_id,
                    d.outcome,
                    d.decided_at,
                    e.id AS evaluation_id,
                    e.rule_version_id,
                    e.object_version_id,
                    e.inputs_json,
                    e.result_json,
                    e.evaluated_at
                FROM decision d
                JOIN evaluation e ON e.id = d.evaluation_id
                WHERE d.id = :decision_id
                """
            ),
            {"decision_id": decision_id},
        )
        row = result.first()
        if row is None:
            raise CanonicalQueryNotFound(f"decision not found: {decision_id}")

        source_evidence = await self.source_evidence(row.rule_version_id)
        return DecisionProvenanceView(
            decision_id=row.decision_id,
            evaluation_id=row.evaluation_id,
            outcome=row.outcome,
            decided_at=row.decided_at,
            evaluated_at=row.evaluated_at,
            object_version_id=row.object_version_id,
            inputs=dict(row.inputs_json),
            result=dict(row.result_json),
            source_evidence=source_evidence,
        )

    async def authority(
        self,
        rule_version_id: UUID,
    ) -> AuthorityClassificationView:
        result = await self.session.execute(
            text(
                """
                SELECT
                    rv.id AS rule_version_id,
                    rv.authority_class,
                    rv.binding,
                    rv.status AS rule_status,
                    sd.source_key,
                    sd.title AS source_title,
                    sd.document_type,
                    sd.issuer,
                    sd.jurisdiction_code
                FROM rule_version rv
                JOIN rule r ON r.id = rv.rule_id
                JOIN source_version sv ON sv.id = r.source_version_id
                JOIN source_document sd ON sd.id = sv.source_id
                WHERE rv.id = :rule_version_id
                """
            ),
            {"rule_version_id": rule_version_id},
        )
        row = result.first()
        if row is None:
            raise CanonicalQueryNotFound(f"rule version not found: {rule_version_id}")
        return AuthorityClassificationView(**dict(row._mapping))

    async def applicability(
        self,
        rule_version_id: UUID,
    ) -> ApplicabilityView:
        result = await self.session.execute(
            text(
                """
                SELECT
                    rv.id AS rule_version_id,
                    r.title AS rule_title,
                    rv.status AS rule_status,
                    r.applicability_json,
                    j.code AS jurisdiction_code,
                    j.name AS jurisdiction_name,
                    ap.priority,
                    ap.condition_expr
                FROM rule_version rv
                JOIN rule r ON r.id = rv.rule_id
                LEFT JOIN applicability ap ON ap.rule_version_id = rv.id
                LEFT JOIN jurisdiction j ON j.id = ap.jurisdiction_id
                WHERE rv.id = :rule_version_id
                ORDER BY ap.priority DESC, j.code
                """
            ),
            {"rule_version_id": rule_version_id},
        )
        rows = result.all()
        if not rows:
            raise CanonicalQueryNotFound(f"rule version not found: {rule_version_id}")
        first = rows[0]
        entries = [
            ApplicabilityEntryView(
                jurisdiction_code=row.jurisdiction_code,
                jurisdiction_name=row.jurisdiction_name,
                priority=row.priority,
                condition=dict(row.condition_expr),
            )
            for row in rows
            if row.jurisdiction_code is not None
        ]
        return ApplicabilityView(
            rule_version_id=first.rule_version_id,
            rule_title=first.rule_title,
            rule_status=first.rule_status,
            applicability=dict(first.applicability_json),
            entries=entries,
        )

    async def temporal_comparison(
        self,
        *,
        source_key: str,
        left_date: date,
        right_date: date,
    ) -> TemporalComparisonView:
        left_row = await self._source_version_at(source_key, left_date)
        right_row = await self._source_version_at(source_key, right_date)

        left_hashes = await self._evidence_hashes(left_row.id)
        right_hashes = await self._evidence_hashes(right_row.id)
        diff = diff_evidence_hashes(left_hashes, right_hashes)

        return TemporalComparisonView(
            source_key=source_key,
            left_date=left_date,
            right_date=right_date,
            left=SourceVersionSnapshot(
                source_version_id=left_row.id,
                version_label=left_row.version_label,
                effective_from=left_row.effective_from,
                effective_to=left_row.effective_to,
                artifact_hash=left_row.artifact_hash,
                evidence_count=len(left_hashes),
            ),
            right=SourceVersionSnapshot(
                source_version_id=right_row.id,
                version_label=right_row.version_label,
                effective_from=right_row.effective_from,
                effective_to=right_row.effective_to,
                artifact_hash=right_row.artifact_hash,
                evidence_count=len(right_hashes),
            ),
            same_source_version=left_row.id == right_row.id,
            added_evidence_keys=list(diff.added),
            removed_evidence_keys=list(diff.removed),
            changed_evidence_keys=list(diff.changed),
            unchanged_evidence_count=diff.unchanged_count,
            indeterminate_evidence_keys=list(diff.indeterminate),
            comparison_completeness=diff.completeness,
        )

    async def jurisdiction_comparison(
        self,
        *,
        rule_title: str,
        left_jurisdiction: str,
        right_jurisdiction: str,
        at_date: date,
    ) -> JurisdictionComparisonView:
        left = await self._rule_at(
            rule_title=rule_title,
            jurisdiction_code=left_jurisdiction,
            at_date=at_date,
        )
        right = await self._rule_at(
            rule_title=rule_title,
            jurisdiction_code=right_jurisdiction,
            at_date=at_date,
        )
        same_logic = None
        if left is not None and right is not None:
            same_logic = left.logic_expr == right.logic_expr
        return JurisdictionComparisonView(
            rule_title=rule_title,
            at_date=at_date,
            left=left,
            right=right,
            same_logic=same_logic,
        )

    async def _source_version_at(self, source_key: str, at_date: date):
        result = await self.session.execute(
            text(
                """
                SELECT
                    sv.id,
                    sv.version_label,
                    sv.effective_from,
                    sv.effective_to,
                    art.content_hash AS artifact_hash
                FROM source_version sv
                JOIN source_document sd ON sd.id = sv.source_id
                LEFT JOIN artifact art ON art.id = sv.artifact_id
                WHERE sd.source_key = :source_key
                  AND sv.status = 'published'
                  AND sv.effective_from <= :at_date
                  AND (sv.effective_to IS NULL OR sv.effective_to >= :at_date)
                ORDER BY sv.effective_from DESC, sv.promulgated_at DESC NULLS LAST
                LIMIT 2
                """
            ),
            {"source_key": source_key, "at_date": at_date},
        )
        rows = result.all()
        if not rows:
            raise CanonicalQueryNotFound(
                f"no effective source version for {source_key!r} at {at_date.isoformat()}"
            )
        if len(rows) > 1:
            raise CanonicalQueryError(
                f"ambiguous effective source versions for {source_key!r} at {at_date.isoformat()}"
            )
        return rows[0]

    async def _evidence_hashes(self, source_version_id: UUID) -> dict[str, str | None]:
        result = await self.session.execute(
            text(
                """
                SELECT evidence_key, normalized_text_hash
                FROM evidence_span
                WHERE source_version_id = :source_version_id
                  AND evidence_key IS NOT NULL
                """
            ),
            {"source_version_id": source_version_id},
        )
        return {row.evidence_key: row.normalized_text_hash for row in result.all()}

    async def _rule_at(
        self,
        *,
        rule_title: str,
        jurisdiction_code: str,
        at_date: date,
    ) -> JurisdictionRuleSnapshot | None:
        result = await self.session.execute(
            text(
                """
                SELECT
                    j.code AS jurisdiction_code,
                    rv.id AS rule_version_id,
                    r.title AS rule_title,
                    rv.authority_class,
                    rv.binding,
                    rv.status AS rule_status,
                    rv.valid_from,
                    rv.valid_to,
                    rv.logic_expr,
                    ap.condition_expr,
                    sd.source_key,
                    sd.title AS source_title
                FROM rule_version rv
                JOIN rule r ON r.id = rv.rule_id
                JOIN applicability ap ON ap.rule_version_id = rv.id
                JOIN jurisdiction j ON j.id = ap.jurisdiction_id
                JOIN source_version sv ON sv.id = r.source_version_id
                JOIN source_document sd ON sd.id = sv.source_id
                WHERE r.title = :rule_title
                  AND j.code = :jurisdiction_code
                  AND rv.status = 'active'
                  AND rv.valid_from <= :at_date
                  AND (rv.valid_to IS NULL OR rv.valid_to >= :at_date)
                ORDER BY rv.valid_from DESC
                LIMIT 2
                """
            ),
            {
                "rule_title": rule_title,
                "jurisdiction_code": jurisdiction_code,
                "at_date": at_date,
            },
        )
        rows = result.all()
        if not rows:
            return None
        if len(rows) > 1:
            raise CanonicalQueryError(
                "ambiguous active rules for "
                f"{rule_title!r} in {jurisdiction_code!r} at {at_date.isoformat()}"
            )
        row = rows[0]
        return JurisdictionRuleSnapshot(
            jurisdiction_code=row.jurisdiction_code,
            rule_version_id=row.rule_version_id,
            rule_title=row.rule_title,
            authority_class=row.authority_class,
            binding=row.binding,
            rule_status=row.rule_status,
            valid_from=row.valid_from,
            valid_to=row.valid_to,
            logic_expr=dict(row.logic_expr),
            condition=dict(row.condition_expr),
            source_key=row.source_key,
            source_title=row.source_title,
        )
