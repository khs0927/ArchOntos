import os
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from archontos.assertions.contracts import AssertionCandidateCreate
from archontos.assertions.persistence import CanonicalAssertionRepository
from archontos.domain.enums import ReviewStatus
from archontos.ingestion.adapters import (
    LawGoKrAdapter,
    LawSearchItem,
    RawSourceEnvelope,
)
from archontos.ingestion.persistence import CanonicalLawRepository
from archontos.normalization.worker import NormalizationOutboxWorker
from archontos.query.persistence import CanonicalQueryRepository
from archontos.rules.evaluation import (
    CanonicalEvaluationRepository,
    RuleNotExecutableError,
)
from archontos.rules.persistence import CanonicalRuleCompilerRepository
from archontos.storage.artifacts import LocalArtifactStore


def _test_dsn() -> str | None:
    value = os.getenv("ARCHONTOS_TEST_DATABASE_URL")
    if not value:
        return None
    return value.replace("postgresql+asyncpg://", "postgresql://", 1)


def _sqlalchemy_url(dsn: str) -> str:
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


def _body_payload(*, mst: str, enforcement_date: str, required_count: int):
    return {
        "법령": {
            "법령키": mst,
            "기본정보": {
                "법령명_한글": "테스트 건축법",
                "법령ID": "001823",
                "법령일련번호": mst,
                "법종구분": {"content": "법률"},
                "소관부처": {"content": "국토교통부"},
                "공포일자": "20260101",
                "시행일자": enforcement_date,
            },
            "조문": {
                "조문단위": {
                    "조문번호": "10",
                    "조문제목": "직통계단",
                    "조문내용": "직통계단 설치기준",
                    "항": {
                        "항번호": "1",
                        "항내용": "직통계단 수에 관한 기준",
                        "호": {
                            "호번호": "1",
                            "호내용": f"직통계단을 {required_count}개소 이상 설치한다.",
                        },
                    },
                }
            },
            "부칙": {"부칙단위": {"부칙공포일자": "20260101", "부칙내용": "부칙"}},
            "별표": {
                "별표단위": {
                    "별표번호": "1",
                    "별표제목": "테스트 별표",
                    "별표서식PDF파일링크": "/LSW/flDownload.do?flSeq=123",
                }
            },
        }
    }


def _law_item(*, mst: str, enforcement_date: date) -> LawSearchItem:
    return LawSearchItem(
        law_name="테스트 건축법",
        law_id="001823",
        mst=mst,
        law_type="법률",
        ministry="국토교통부",
        promulgation_date=date(2026, 1, 1),
        promulgation_number="21000",
        enforcement_date=enforcement_date,
        revision_type="일부개정",
        history_code="현행",
        detail_link=f"/DRF/lawService.do?target=law&MST={mst}",
    )


async def _persist_version(
    *,
    session_factory,
    store: LocalArtifactStore,
    mst: str,
    enforcement_date: date,
    required_count: int,
):
    envelope = RawSourceEnvelope(
        source_name="law.go.kr",
        endpoint="https://www.law.go.kr/DRF/lawService.do",
        params={"target": "law", "MST": mst, "ID": "001823"},
        payload=_body_payload(
            mst=mst,
            enforcement_date=enforcement_date.strftime("%Y%m%d"),
            required_count=required_count,
        ),
        fetched_at=datetime.now(UTC),
    )
    artifact = await store.put_envelope(envelope)
    body = LawGoKrAdapter.parse_body(envelope)
    item = _law_item(mst=mst, enforcement_date=enforcement_date)

    async with session_factory() as session:
        async with session.begin():
            return await CanonicalLawRepository(session).persist_law_version(
                item=item,
                body=body,
                body_artifact=artifact,
            )


@pytest.mark.asyncio
async def test_postgres_mvp0_golden_path(tmp_path):
    dsn = _test_dsn()
    if not dsn:
        pytest.skip("ARCHONTOS_TEST_DATABASE_URL is not configured")

    schema = f"archontos_test_{uuid4().hex}"
    migrations_dir = Path(__file__).resolve().parents[2] / "db" / "migrations"

    admin = await asyncpg.connect(dsn)
    engine = None
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        await admin.execute(f'SET search_path TO "{schema}", public')
        for migration in sorted(migrations_dir.glob("*.sql")):
            await admin.execute(migration.read_text(encoding="utf-8"))

        engine = create_async_engine(
            _sqlalchemy_url(dsn),
            connect_args={"server_settings": {"search_path": f"{schema},public"}},
        )
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        store = LocalArtifactStore(tmp_path / "artifacts")

        v1 = await _persist_version(
            session_factory=session_factory,
            store=store,
            mst="999001",
            enforcement_date=date(2026, 1, 1),
            required_count=2,
        )
        assert v1.created is True

        first_work = await NormalizationOutboxWorker(
            session_factory=session_factory,
            artifact_store=store,
        ).process_one()
        assert first_work is not None
        assert first_work.status == "published"
        assert first_work.evidence_count >= 4

        async with session_factory() as session:
            evidence_id = (
                await session.execute(
                    text(
                        """
                        SELECT id
                        FROM evidence_span
                        WHERE source_version_id = :source_version_id
                          AND locator_json->>'kind' = 'subparagraph'
                        LIMIT 1
                        """
                    ),
                    {"source_version_id": v1.source_version_id},
                )
            ).scalar_one()

        candidate_payload = AssertionCandidateCreate(
            evidence_span_id=evidence_id,
            natural_language="직통계단을 2개소 이상 설치하여야 한다.",
            structured_payload={
                "fact_path": "stair.direct_count",
                "operator": ">=",
                "value": 2,
                "unit": "count",
                "title": "직통계단 수",
            },
            interpreter_method="human",
            interpretation_confidence=1.0,
        )

        async with session_factory() as session:
            async with session.begin():
                assertions = CanonicalAssertionRepository(session)
                candidate = await assertions.create_candidate(candidate_payload)
                assert candidate.review_status is ReviewStatus.UNREVIEWED

        async with session_factory() as session:
            async with session.begin():
                review = await CanonicalAssertionRepository(session).review(
                    assertion_id=candidate.assertion_id,
                    decision=ReviewStatus.APPROVED,
                    reviewer_id="integration-test",
                    note="golden scenario approval",
                )
                assert review.review_status is ReviewStatus.APPROVED

        async with session_factory() as session:
            async with session.begin():
                compiled = await CanonicalRuleCompilerRepository(
                    session
                ).compile_approved_assertion(candidate.assertion_id)
                assert compiled.status == "active"

        async with session_factory() as session:
            async with session.begin():
                evaluation = await CanonicalEvaluationRepository(session).evaluate(
                    rule_version_id=compiled.rule_version_id,
                    facts={
                        "context": {"jurisdiction": "KR"},
                        "stair": {"direct_count": 1},
                    },
                    evaluated_at=datetime(2026, 6, 1, tzinfo=UTC),
                )
                assert evaluation.applicable is True
                assert evaluation.outcome is not None
                assert evaluation.outcome.value == "FAIL"
                assert evaluation.decision_id is not None

        async with session_factory() as session:
            queries = CanonicalQueryRepository(session)
            evidence = await queries.source_evidence(compiled.rule_version_id)
            authority = await queries.authority(compiled.rule_version_id)
            applicability = await queries.applicability(compiled.rule_version_id)
            provenance = await queries.decision_provenance(evaluation.decision_id)
            jurisdiction = await queries.jurisdiction_comparison(
                rule_title="직통계단 수",
                left_jurisdiction="KR",
                right_jurisdiction="KR",
                at_date=date(2026, 6, 1),
            )

            assert evidence.evidence[0].assertion_review_status is ReviewStatus.APPROVED
            assert authority.authority_class == "statutory"
            assert authority.document_type == "statute"
            assert applicability.entries[0].jurisdiction_code == "KR"
            assert provenance.decision_id == evaluation.decision_id
            assert provenance.evaluation_id == evaluation.evaluation_id
            assert provenance.outcome == "FAIL"
            assert provenance.source_evidence.evidence[0].evidence_id == evidence_id
            assert jurisdiction.left is not None
            assert jurisdiction.right is not None
            assert jurisdiction.same_logic is True

        v2 = await _persist_version(
            session_factory=session_factory,
            store=store,
            mst="999002",
            enforcement_date=date(2027, 1, 1),
            required_count=3,
        )
        assert v2.created is True

        second_work = await NormalizationOutboxWorker(
            session_factory=session_factory,
            artifact_store=store,
        ).process_one()
        assert second_work is not None
        assert second_work.status == "published"

        async with session_factory() as session:
            previous = (
                await session.execute(
                    text(
                        """
                        SELECT effective_to, superseded_by
                        FROM source_version
                        WHERE id = :source_version_id
                        """
                    ),
                    {"source_version_id": v1.source_version_id},
                )
            ).one()
            assert previous.effective_to == date(2026, 12, 31)
            assert previous.superseded_by == v2.source_version_id

            rule_valid_to = (
                await session.execute(
                    text("SELECT valid_to FROM rule_version WHERE id = :id"),
                    {"id": compiled.rule_version_id},
                )
            ).scalar_one()
            assert rule_valid_to == date(2026, 12, 31)

            temporal = await CanonicalQueryRepository(session).temporal_comparison(
                source_key="lawgo:law:001823",
                left_date=date(2026, 6, 1),
                right_date=date(2027, 2, 1),
            )
            assert temporal.same_source_version is False
            assert temporal.changed_evidence_keys

        async with session_factory() as session:
            with pytest.raises(RuleNotExecutableError):
                async with session.begin():
                    await CanonicalEvaluationRepository(session).evaluate(
                        rule_version_id=compiled.rule_version_id,
                        facts={
                            "context": {"jurisdiction": "KR"},
                            "stair": {"direct_count": 3},
                        },
                        evaluated_at=datetime(2027, 2, 1, tzinfo=UTC),
                    )

        async with session_factory() as session:
            async with session.begin():
                contested = await CanonicalAssertionRepository(session).review(
                    assertion_id=candidate.assertion_id,
                    decision=ReviewStatus.CONTESTED,
                    reviewer_id="integration-test",
                    note="challenge interpretation",
                )
                assert contested.review_status is ReviewStatus.CONTESTED

        async with session_factory() as session:
            status = (
                await session.execute(
                    text("SELECT status FROM rule_version WHERE id = :id"),
                    {"id": compiled.rule_version_id},
                )
            ).scalar_one()
            assert status == "suspended"

            outbox_states = (
                await session.execute(
                    text(
                        """
                        SELECT status, attempts
                        FROM outbox_message
                        WHERE topic = 'normalization.source-version'
                        ORDER BY id
                        """
                    )
                )
            ).all()
            assert [row.status for row in outbox_states] == ["published", "published"]
            assert all(row.attempts == 1 for row in outbox_states)

        async with session_factory() as session:
            with pytest.raises(RuleNotExecutableError):
                async with session.begin():
                    await CanonicalEvaluationRepository(session).evaluate(
                        rule_version_id=compiled.rule_version_id,
                        facts={
                            "context": {"jurisdiction": "KR"},
                            "stair": {"direct_count": 3},
                        },
                        evaluated_at=datetime(2026, 6, 2, tzinfo=UTC),
                    )

        async with session_factory() as session:
            async with session.begin():
                malformed_event_id = (
                    await session.execute(
                        text(
                            """
                            INSERT INTO domain_event(
                                aggregate_type, aggregate_id, event_type, payload_json
                            )
                            VALUES (
                                'source_version', 'malformed-test',
                                'SourceVersionIngested', '{}'::jsonb
                            )
                            RETURNING event_id
                            """
                        )
                    )
                ).scalar_one()
                malformed_outbox_id = (
                    await session.execute(
                        text(
                            """
                            INSERT INTO outbox_message(event_id, topic, payload_json)
                            VALUES (
                                :event_id, 'normalization.source-version', '{}'::jsonb
                            )
                            RETURNING id
                            """
                        ),
                        {"event_id": malformed_event_id},
                    )
                ).scalar_one()

        malformed_work = await NormalizationOutboxWorker(
            session_factory=session_factory,
            artifact_store=store,
            max_attempts=1,
        ).process_one()
        assert malformed_work is not None
        assert malformed_work.outbox_id == malformed_outbox_id
        assert malformed_work.source_version_id is None
        assert malformed_work.status == "failed"
        assert malformed_work.attempts == 1

        async with session_factory() as session:
            failure_row = (
                await session.execute(
                    text(
                        """
                        SELECT status, attempts, last_error
                        FROM outbox_message
                        WHERE id = :outbox_id
                        """
                    ),
                    {"outbox_id": malformed_outbox_id},
                )
            ).one()
            assert failure_row.status == "failed"
            assert failure_row.attempts == 1
            assert failure_row.last_error

            flag = (
                await session.execute(
                    text(
                        """
                        SELECT target_table, flag_type, severity
                        FROM quality_flag
                        WHERE target_table = 'outbox_message'
                          AND flag_type = 'contract_violation'
                        ORDER BY created_at DESC
                        LIMIT 1
                        """
                    )
                )
            ).one()
            assert flag.target_table == "outbox_message"
            assert flag.flag_type == "contract_violation"
            assert flag.severity == "high"

    finally:
        if engine is not None:
            await engine.dispose()
        try:
            await admin.execute("SET search_path TO public")
            await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            await admin.close()
