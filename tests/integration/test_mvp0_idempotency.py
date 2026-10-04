"""Golden Scenario step 10: reprocessing the same fixture must not duplicate or drift.

`tests/MVP0-GOLDEN-SCENARIO.md` step 10 reads:

    10. 같은 fixture를 다시 처리해 중복과 결과 변동이 없는지 확인한다.

The existing golden-path test replays a *different* version (mst 999002) to
exercise the temporal-diff path, so it never re-processes an identical input.
This module covers that gap, plus the adjacent same-MST/different-bytes
conflict that `ops/RELEASE-CHECKLIST.md` lists under data integrity.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from conftest import body_payload, count_rows, law_item, persist_law_version
from sqlalchemy import text

from archontos.ingestion.adapters import LawGoKrAdapter, RawSourceEnvelope
from archontos.ingestion.persistence import CanonicalLawRepository
from archontos.normalization.worker import NormalizationOutboxWorker
from archontos.storage.artifacts import LocalArtifactStore

pytestmark = pytest.mark.asyncio

MST = "999001"
ENFORCEMENT = date(2026, 1, 1)
REQUIRED_COUNT = 2

TABLES = (
    "source_document",
    "source_version",
    "artifact",
    "outbox_message",
    "evidence_span",
)


def _envelope(payload: dict) -> RawSourceEnvelope:
    return RawSourceEnvelope(
        source_name="law.go.kr",
        endpoint="https://www.law.go.kr/DRF/lawService.do",
        params={"target": "law", "MST": MST, "ID": "001823"},
        payload=payload,
        fetched_at=datetime.now(UTC),
    )


async def _row_counts(session_factory) -> dict[str, int]:
    return {table: await count_rows(session_factory, table) for table in TABLES}


async def _evidence_snapshot(session_factory) -> list[tuple]:
    """Return evidence identity in a stable order so drift is comparable.

    `evidence_key` and `normalized_text_hash` come from migration 004; the
    locator kind lives inside the `locator_json` document.
    """
    async with session_factory() as session:
        rows = await session.execute(
            text(
                """
                SELECT locator_json->>'kind' AS kind, evidence_key, normalized_text_hash
                FROM evidence_span
                ORDER BY kind, evidence_key
                """
            )
        )
        return [tuple(row) for row in rows]


async def _stored_content_hash(session_factory, source_version_id) -> str:
    async with session_factory() as session:
        return (
            await session.execute(
                text(
                    """
                    SELECT a.content_hash
                    FROM source_version sv
                    JOIN artifact a ON a.id = sv.artifact_id
                    WHERE sv.id = :source_version_id
                    """
                ),
                {"source_version_id": source_version_id},
            )
        ).scalar_one()


async def test_reprocessing_identical_fixture_is_a_no_op(mvp0_db, tmp_path):
    """Step 10: a second identical ingest adds no rows and changes no result."""
    session_factory, _schema = mvp0_db
    store = LocalArtifactStore(tmp_path / "artifacts")

    first = await persist_law_version(
        session_factory=session_factory,
        store=store,
        mst=MST,
        enforcement_date=ENFORCEMENT,
        required_count=REQUIRED_COUNT,
    )
    assert first.created is True

    work = await NormalizationOutboxWorker(
        session_factory=session_factory,
        artifact_store=store,
    ).process_one()
    assert work is not None
    assert work.status == "published"

    counts_before = await _row_counts(session_factory)
    evidence_before = await _evidence_snapshot(session_factory)
    assert counts_before["evidence_span"] > 0
    assert counts_before["source_version"] == 1

    # --- replay the exact same fixture -----------------------------------
    second = await persist_law_version(
        session_factory=session_factory,
        store=store,
        mst=MST,
        enforcement_date=ENFORCEMENT,
        required_count=REQUIRED_COUNT,
    )

    # The upsert is keyed on (source_id, version_label) and must not create a
    # second version; it hands back the row that already exists.
    assert second.created is False
    assert second.source_version_id == first.source_version_id
    # Identical bytes are not a conflict, so the conflict flag must stay clear.
    assert second.conflict is False

    counts_after = await _row_counts(session_factory)
    assert counts_after == counts_before, (
        f"replay changed row counts: {counts_before} -> {counts_after}"
    )
    assert counts_after["source_version"] == 1
    assert counts_after["outbox_message"] == 1
    # Artifacts are content addressed, so identical bytes must not duplicate.
    assert counts_after["artifact"] == counts_before["artifact"]
    assert await count_rows(session_factory, "quality_flag") == 0

    # No new outbox work means the normalizer must not run again, so evidence
    # cannot be regenerated or drift.
    replay_work = await NormalizationOutboxWorker(
        session_factory=session_factory,
        artifact_store=store,
    ).process_one()
    assert replay_work is None

    assert await _evidence_snapshot(session_factory) == evidence_before


async def test_same_version_label_with_different_bytes_is_flagged_not_overwritten(
    mvp0_db, tmp_path
):
    """A changed body under an existing version label must not rewrite canonical state.

    The persistence layer answers this with a recorded conflict rather than an
    exception: it keeps the original row, marks the result ``conflict=True`` and
    writes a high-severity ``quality_flag``. A bare "did not raise" assertion
    would still pass if the row had been silently overwritten, so both the
    canonical content hash and the flag are asserted here.
    """
    session_factory, _schema = mvp0_db
    store = LocalArtifactStore(tmp_path / "artifacts")

    first = await persist_law_version(
        session_factory=session_factory,
        store=store,
        mst=MST,
        enforcement_date=ENFORCEMENT,
        required_count=REQUIRED_COUNT,
    )
    assert first.created is True
    original_hash = await _stored_content_hash(session_factory, first.source_version_id)

    # Same MST and same effective date, but the article text now demands a
    # different stair count, so the bytes differ under a taken version label.
    conflicting = body_payload(
        mst=MST,
        enforcement_date=ENFORCEMENT.strftime("%Y%m%d"),
        required_count=99,
    )
    envelope = _envelope(conflicting)
    artifact = await store.put_envelope(envelope)
    assert artifact.content_hash != original_hash

    body = LawGoKrAdapter.parse_body(envelope)
    item = law_item(mst=MST, enforcement_date=ENFORCEMENT)

    async with session_factory() as session:
        async with session.begin():
            result = await CanonicalLawRepository(session).persist_law_version(
                item=item,
                body=body,
                body_artifact=artifact,
            )

    assert result.created is False
    assert result.conflict is True
    assert result.source_version_id == first.source_version_id

    # The canonical row must still point at the original bytes.
    assert await count_rows(session_factory, "source_version") == 1
    assert await _stored_content_hash(session_factory, first.source_version_id) == original_hash

    async with session_factory() as session:
        flag = (
            await session.execute(
                text(
                    """
                    SELECT flag_type, severity, message
                    FROM quality_flag
                    WHERE target_table = 'source_version'
                      AND target_id = :target_id
                    """
                ),
                {"target_id": first.source_version_id},
            )
        ).one()
    assert flag.flag_type == "conflict"
    assert flag.severity == "high"
    assert "different bytes" in flag.message
