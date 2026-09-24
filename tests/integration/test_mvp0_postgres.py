import os
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from archontos.assertions.contracts import AssertionCandidateCreate
from archontos.assertions.persistence import CanonicalAssertionRepository
from archontos.domain.enums import ReviewStatus
from archontos.query.persistence import CanonicalQueryRepository
from archontos.rules.persistence import CanonicalRuleCompilerRepository


def _test_dsn() -> str | None:
    value = os.getenv("ARCHONTOS_TEST_DATABASE_URL")
    if not value:
        return None
    return value.replace("postgresql+asyncpg://", "postgresql://", 1)


def _sqlalchemy_url(dsn: str) -> str:
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.mark.asyncio
async def test_postgres_mvp0_golden_path():
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
            connect_args={
                "server_settings": {
                    "search_path": f"{schema},public",
                }
            },
        )
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with session_factory() as session:
            async with session.begin():
                source_id = (
                    await session.execute(
                        text(
                            """
                            INSERT INTO source_document(
                                source_key, title, issuer, jurisdiction_code,
                                document_type, source_url
                            )
                            VALUES (
                                'lawgo:law:test-building-act',
                                '테스트 건축법', '국토교통부', 'KR',
                                'statute', 'https://www.law.go.kr/'
                            )
                            RETURNING id
                            """
                        )
                    )
                ).scalar_one()
                artifact_id = (
                    await session.execute(
                        text(
                            """
                            INSERT INTO artifact(
                                artifact_type, mime, storage_uri, content_hash, byte_size
                            )
                            VALUES (
                                'raw-source', 'application/json',
                                'local://raw/test/v1.json',
                                :content_hash, 128
                            )
                            RETURNING id
                            """
                        ),
                        {"content_hash": "a" * 64},
                    )
                ).scalar_one()
                source_version_id = (
                    await session.execute(
                        text(
                            """
                            INSERT INTO source_version(
                                source_id, artifact_id, version_label,
                                effective_from, effective_to, raw_manifest_json, status
                            )
                            VALUES (
                                :source_id, :artifact_id, 'mst:test-v1',
                                DATE '2026-01-01', DATE '2026-12-31',
                                '{}'::jsonb, 'published'
                            )
                            RETURNING id
                            """
                        ),
                        {"source_id": source_id, "artifact_id": artifact_id},
                    )
                ).scalar_one()
                evidence_id = (
                    await session.execute(
                        text(
                            """
                            INSERT INTO evidence_span(
                                source_version_id, artifact_id, evidence_key,
                                locator_json, text_snippet, normalized_text_hash,
                                extractor_method, extraction_confidence
                            )
                            VALUES (
                                :source_version_id, :artifact_id, 'lawgo:article:test-1',
                                '{"source":"law.go.kr","article_no":"1"}'::jsonb,
                                '직통계단을 2개소 이상 설치하여야 한다.',
                                :text_hash, 'structured-parser', 1.0
                            )
                            RETURNING id
                            """
                        ),
                        {
                            "source_version_id": source_version_id,
                            "artifact_id": artifact_id,
                            "text_hash": "1" * 64,
                        },
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
            queries = CanonicalQueryRepository(session)
            evidence = await queries.source_evidence(compiled.rule_version_id)
            authority = await queries.authority(compiled.rule_version_id)
            applicability = await queries.applicability(compiled.rule_version_id)

            assert evidence.evidence[0].evidence_id == evidence_id
            assert evidence.evidence[0].assertion_review_status is ReviewStatus.APPROVED
            assert authority.authority_class == "statutory"
            assert authority.document_type == "statute"
            assert applicability.entries[0].jurisdiction_code == "KR"

        async with session_factory() as session:
            async with session.begin():
                artifact_v2 = (
                    await session.execute(
                        text(
                            """
                            INSERT INTO artifact(
                                artifact_type, mime, storage_uri, content_hash, byte_size
                            )
                            VALUES (
                                'raw-source', 'application/json',
                                'local://raw/test/v2.json',
                                :content_hash, 128
                            )
                            RETURNING id
                            """
                        ),
                        {"content_hash": "b" * 64},
                    )
                ).scalar_one()
                source_version_v2 = (
                    await session.execute(
                        text(
                            """
                            INSERT INTO source_version(
                                source_id, artifact_id, version_label,
                                effective_from, raw_manifest_json, status
                            )
                            VALUES (
                                :source_id, :artifact_id, 'mst:test-v2',
                                DATE '2027-01-01', '{}'::jsonb, 'published'
                            )
                            RETURNING id
                            """
                        ),
                        {"source_id": source_id, "artifact_id": artifact_v2},
                    )
                ).scalar_one()
                await session.execute(
                    text(
                        """
                        UPDATE source_version
                        SET superseded_by = :next_id
                        WHERE id = :previous_id
                        """
                    ),
                    {
                        "next_id": source_version_v2,
                        "previous_id": source_version_id,
                    },
                )
                await session.execute(
                    text(
                        """
                        INSERT INTO evidence_span(
                            source_version_id, artifact_id, evidence_key,
                            locator_json, text_snippet, normalized_text_hash,
                            extractor_method, extraction_confidence
                        )
                        VALUES (
                            :source_version_id, :artifact_id, 'lawgo:article:test-1',
                            '{"source":"law.go.kr","article_no":"1"}'::jsonb,
                            '직통계단을 3개소 이상 설치하여야 한다.',
                            :text_hash, 'structured-parser', 1.0
                        )
                        """
                    ),
                    {
                        "source_version_id": source_version_v2,
                        "artifact_id": artifact_v2,
                        "text_hash": "2" * 64,
                    },
                )

        async with session_factory() as session:
            temporal = await CanonicalQueryRepository(session).temporal_comparison(
                source_key="lawgo:law:test-building-act",
                left_date=__import__("datetime").date(2026, 6, 1),
                right_date=__import__("datetime").date(2027, 2, 1),
            )
            assert temporal.same_source_version is False
            assert temporal.changed_evidence_keys == ["lawgo:article:test-1"]

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

    finally:
        if engine is not None:
            await engine.dispose()
        try:
            await admin.execute("SET search_path TO public")
            await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            await admin.close()
