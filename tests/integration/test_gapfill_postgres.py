"""Real-PostgreSQL coverage for action persistence, the projection worker and hyperedges.

These are the canonical backends behind ``ARCHONTOS_ACTION_BACKEND=postgres``; the
memory stores in ``tests/test_gapfill_*`` cannot catch SQL or schema drift.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from archontos.actions.gate import ApprovalDenied
from archontos.actions.persistence import PostgresActionStore
from archontos.actions.service import propose_report
from archontos.graph.hyperedges import HyperedgeMember, PostgresHyperedgeStore
from archontos.projection.embedder import HashingEmbedder
from archontos.projection.worker import ProjectionWorker

pytestmark = pytest.mark.asyncio


async def _emit(session_factory, n: int, topic: str = "projection.embedding") -> None:
    async with session_factory() as session, session.begin():
        for i in range(n):
            event_id = (
                await session.execute(
                    text(
                        """
                        INSERT INTO domain_event(
                            aggregate_type, aggregate_id, event_type, payload_json
                        )
                        VALUES ('source_version', :agg, 'SourceVersionNormalized',
                                CAST(:payload AS jsonb))
                        RETURNING event_id
                        """
                    ),
                    {"agg": f"sv-{i}", "payload": json.dumps({"i": i})},
                )
            ).scalar_one()
            await session.execute(
                text(
                    """
                    INSERT INTO outbox_message(event_id, topic, payload_json)
                    VALUES (:event_id, :topic, '{}'::jsonb)
                    """
                ),
                {"event_id": event_id, "topic": topic},
            )


async def _scalar(session_factory, sql: str):
    async with session_factory() as session:
        return (await session.execute(text(sql))).scalar_one()


async def test_action_gate_is_enforced_in_postgres(mvp0_db):
    session_factory, _ = mvp0_db
    store = PostgresActionStore(session_factory)
    proposed = await store.propose(propose_report(["rule:1"], {"note": "pg"}))
    assert proposed.status == "proposed"
    with pytest.raises(ApprovalDenied):
        await store.execute(proposed.id, "reviewer")
    assert (await store.approve(proposed.id, "reviewer")).status == "approved"
    done = await store.execute(proposed.id, "reviewer")
    assert done.status == "succeeded"
    assert done.runs[0]["result"]["status"] == "generated"
    with pytest.raises(ApprovalDenied):
        await store.execute(proposed.id, "reviewer")
    audit = await _scalar(
        session_factory, "SELECT count(*) FROM audit_log WHERE target_table = 'action'"
    )
    assert audit == 3  # propose, approved, execute


async def test_action_reject_and_missing(mvp0_db):
    session_factory, _ = mvp0_db
    store = PostgresActionStore(session_factory)
    proposed = await store.propose(propose_report(["rule:2"], {}))
    assert (await store.reject(proposed.id, "r", "nope")).status == "rejected"
    with pytest.raises(ApprovalDenied):
        await store.approve(proposed.id, "r")
    with pytest.raises(KeyError):
        await store.approve("00000000-0000-0000-0000-000000000000", "r")
    assert await store.get("00000000-0000-0000-0000-000000000000") is None


async def test_projection_drain_checkpoint_and_rebuild(mvp0_db):
    session_factory, _ = mvp0_db
    await _emit(session_factory, 5)
    await _emit(session_factory, 1, topic="normalization.other")
    worker = ProjectionWorker(session_factory)

    first = await worker.process_batch(limit=2)
    assert first.applied == 2
    # Only the outbox rows whose events were covered are acknowledged.
    published = await _scalar(
        session_factory, "SELECT count(*) FROM outbox_message WHERE status = 'published'"
    )
    assert published == 2

    rest = await worker.process_batch(limit=100)
    assert rest.applied == 4
    assert (await worker.process_batch()).applied == 0
    assert await _scalar(session_factory, "SELECT count(*) FROM embedding_projection") == 6
    # Non-projection topics are left to their own consumers.
    pending_other = await _scalar(
        session_factory,
        "SELECT count(*) FROM outbox_message WHERE topic = 'normalization.other' "
        "AND status = 'pending'",
    )
    assert pending_other == 1
    assert worker.projection.rows == {}

    rebuilt = await worker.rebuild(limit=2)
    assert rebuilt.reset is True
    assert rebuilt.applied == 6
    assert rebuilt.last_sequence == rest.last_sequence
    assert await _scalar(session_factory, "SELECT count(*) FROM embedding_projection") == 6
    status = await worker.status()
    assert status["checkpoint"]["last_sequence"] == rest.last_sequence


async def test_hyperedge_round_trip(mvp0_db):
    session_factory, _ = mvp0_db
    store = PostgresHyperedgeStore(session_factory)
    created = await store.create(
        "applies-to",
        [HyperedgeMember("object", "space", "s1", 1), HyperedgeMember("rule", "rule", "r1")],
        {"source": "test"},
    )
    loaded = await store.get(created.id)
    assert loaded is not None
    assert [m.role for m in loaded.members] == ["rule", "object"]
    assert loaded.properties == {"source": "test"}


async def test_projection_writes_vectors_with_model_id(mvp0_db):
    session_factory, _ = mvp0_db
    await _emit(session_factory, 3)
    worker = ProjectionWorker(session_factory, embedder=HashingEmbedder())
    assert (await worker.process_batch()).applied == 3
    async with session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT embedding_model, vector_dims(embedding) AS dims, "
                    "embedding <=> embedding AS self_distance FROM embedding_projection"
                )
            )
        ).all()
    assert {row.embedding_model for row in rows} == {"hashing-v1/256"}
    assert {row.dims for row in rows} == {1536}
    assert all(abs(row.self_distance) < 1e-6 for row in rows)
    assert (await worker.status())["embedder"] == "hashing-v1/256"


async def test_no_embedder_keeps_vector_and_model_null(mvp0_db):
    session_factory, _ = mvp0_db
    await _emit(session_factory, 1)
    await ProjectionWorker(session_factory).process_batch()
    nulls = await _scalar(
        session_factory,
        "SELECT count(*) FROM embedding_projection "
        "WHERE embedding IS NULL AND embedding_model IS NULL",
    )
    assert nulls == 1
