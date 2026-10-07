"""Similarity search, HNSW index and identity/RLS against a real PostgreSQL."""

from __future__ import annotations

import json
from uuid import uuid4

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

import apps.action as action_app
import apps.projection as projection_app
from archontos.actions.persistence import PostgresActionStore
from archontos.actions.service import propose_report
from archontos.config import get_settings
from archontos.identity import Principal, reset_principal, set_principal
from archontos.projection.embedder import HashingEmbedder
from archontos.projection.search import similar
from archontos.projection.worker import ProjectionWorker

pytestmark = pytest.mark.asyncio

TEXTS = {
    "height": "건축물의 높이 제한 일조 확보 기준",
    "height2": "건축물 높이 제한 완화",
    "parking": "부설주차장 설치 기준과 주차대수 산정",
}


async def _seed(session_factory, embedder=None):
    async with session_factory() as session, session.begin():
        for agg, body in TEXTS.items():
            await session.execute(
                text(
                    """
                    INSERT INTO domain_event(
                        aggregate_type, aggregate_id, event_type, payload_json
                    )
                    VALUES ('provision', :agg, 'ProvisionNormalized', CAST(:p AS jsonb))
                    """
                ),
                {"agg": agg, "p": json.dumps({"text": body}, ensure_ascii=False)},
            )
    await ProjectionWorker(session_factory, embedder=embedder or HashingEmbedder()).rebuild()


async def test_similar_ranks_related_text_first(mvp0_db):
    session_factory, _ = mvp0_db
    await _seed(session_factory)
    hits = await similar(session_factory, HashingEmbedder(), "건축물 높이 제한", limit=3)
    assert [hit.source_id for hit in hits][:2] in (["height", "height2"], ["height2", "height"])
    assert hits[0].score >= hits[-1].score
    assert all(-1.0 <= hit.score <= 1.0 + 1e-9 for hit in hits)

    only = await similar(session_factory, HashingEmbedder(), "주차", limit=5, source_type="nope")
    assert only == []
    strict = await similar(session_factory, HashingEmbedder(), "주차대수", limit=5, min_score=0.99)
    assert strict == []


async def test_similar_ignores_vectors_from_other_models(mvp0_db):
    session_factory, _ = mvp0_db
    await _seed(session_factory, embedder=HashingEmbedder(dim=128))
    assert await similar(session_factory, HashingEmbedder(dim=256), "높이", limit=5) == []
    assert len(await similar(session_factory, HashingEmbedder(dim=128), "높이", limit=5)) == 3


async def test_similar_rejects_bad_arguments(mvp0_db):
    session_factory, _ = mvp0_db
    with pytest.raises(ValueError):
        await similar(session_factory, HashingEmbedder(), "x", limit=0)
    with pytest.raises(ValueError):
        await similar(session_factory, HashingEmbedder(), "   ")


async def test_hnsw_index_serves_the_search_query(mvp0_db):
    session_factory, _ = mvp0_db
    await _seed(session_factory)
    vector = "[" + ",".join(["0"] * 1535 + ["1"]) + "]"
    async with session_factory() as session, session.begin():
        await session.execute(text("SET LOCAL enable_seqscan = off"))
        plan = (
            await session.execute(
                text(
                    """
                    EXPLAIN SELECT projection_key FROM embedding_projection
                    WHERE embedding IS NOT NULL
                    ORDER BY embedding <=> CAST(:q AS vector) LIMIT 5
                    """
                ),
                {"q": vector},
            )
        ).all()
    assert "hnsw_embedding_projection_cosine" in "\n".join(row[0] for row in plan)


async def test_actor_is_recorded_on_action_and_audit(mvp0_db):
    session_factory, _ = mvp0_db
    store = PostgresActionStore(session_factory)
    token = set_principal(Principal(actor="alice", authenticated=True))
    try:
        proposed = await store.propose(propose_report(["rule:1"], {}), created_by="alice")
        await store.approve(proposed.id, "bob")
    finally:
        reset_principal(token)
    assert (await store.get(proposed.id)).created_by == "alice"
    async with session_factory() as session:
        actors = (
            (
                await session.execute(
                    text("SELECT actor FROM audit_log WHERE target_id = :id ORDER BY id"),
                    {"id": proposed.id},
                )
            )
            .scalars()
            .all()
        )
    assert actors == ["alice", "bob"]


async def test_app_role_cannot_rewrite_audit_log(mvp0_db):
    """The service login (non-superuser, archontos_app) can append but has no UPDATE/DELETE."""
    session_factory, _ = mvp0_db
    async with session_factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO audit_log(action, target_table, target_id) "
                "VALUES ('probe', 'action', 'x')"
            )
        )
    for statement in ("UPDATE audit_log SET actor = 'mallory'", "DELETE FROM audit_log"):
        with pytest.raises(DBAPIError, match="permission denied"):
            async with session_factory() as session, session.begin():
                await session.execute(text(statement))


async def test_audit_log_rls_blocks_rewrites_even_with_table_privileges(mvp0_db, postgres_dsn):
    """Forced RLS (migration 011) holds even for a role that was granted UPDATE/DELETE."""
    _, schema = mvp0_db
    role = f"archontos_rls_{uuid4().hex[:12]}"
    admin = await asyncpg.connect(postgres_dsn)
    try:
        await admin.execute(f'CREATE ROLE "{role}" NOLOGIN')
        await admin.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"')
        await admin.execute(
            f'GRANT SELECT, INSERT, UPDATE, DELETE ON "{schema}".audit_log TO "{role}"'
        )
        await admin.execute(f'GRANT USAGE ON ALL SEQUENCES IN SCHEMA "{schema}" TO "{role}"')
        async with admin.transaction():
            await admin.execute(f'SET LOCAL search_path TO "{schema}", public')
            await admin.execute(f'SET LOCAL ROLE "{role}"')
            await admin.execute("SELECT set_config('app.actor', 'carol', true)")
            await admin.execute(
                "INSERT INTO audit_log(action, target_table, target_id) "
                "VALUES ('probe', 'action', 'x')"
            )
            assert (
                await admin.fetchval("SELECT actor FROM audit_log WHERE action = 'probe'")
                == "carol"
            )
            assert await admin.execute("UPDATE audit_log SET actor = 'mallory'") == "UPDATE 0"
            assert await admin.execute("DELETE FROM audit_log") == "DELETE 0"
    finally:
        await admin.execute(f'DROP OWNED BY "{role}"')
        await admin.execute(f'DROP ROLE "{role}"')
        await admin.close()


@pytest.fixture
def postgres_apps(mvp0_db, monkeypatch):
    session_factory, _ = mvp0_db
    monkeypatch.setenv("ARCHONTOS_ACTION_BACKEND", "postgres")
    monkeypatch.setenv("ARCHONTOS_EMBEDDER", "hashing")
    monkeypatch.setenv("ARCHONTOS_API_KEYS", "alice:key-a,key-b")
    get_settings.cache_clear()
    monkeypatch.setattr(projection_app, "get_session_factory", lambda: session_factory)
    monkeypatch.setattr(action_app, "get_session_factory", lambda: session_factory)
    projection_app._embedder_cache.clear()
    yield session_factory
    get_settings.cache_clear()
    projection_app._embedder_cache.clear()


async def test_http_search_and_identity(postgres_apps):
    session_factory = postgres_apps
    await _seed(session_factory)
    projection = AsyncClient(transport=ASGITransport(app=projection_app.app), base_url="http://t")
    response = await projection.post(
        "/v1/search/similar",
        json={"query": "건축물 높이", "limit": 2},
        headers={"x-api-key": "key-a"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["embedding_model"] == "hashing-v1/256"
    assert len(body["hits"]) == 2
    assert response.headers["x-archontos-actor"] == "alice"
    blank = await projection.post(
        "/v1/search/similar", json={"query": "  "}, headers={"x-api-key": "key-a"}
    )
    assert blank.status_code == 422

    actions = AsyncClient(transport=ASGITransport(app=action_app.app), base_url="http://t")
    proposed = await actions.post(
        "/v1/actions/report/propose",
        json={"target_refs": ["rule:1"], "context": {}},
        headers={"x-api-key": "key-a"},
    )
    created = proposed.json()
    assert created["created_by"] == "alice"
    spoof = await actions.post(
        f"/v1/actions/{created['id']}/approve",
        json={"actor": "mallory"},
        headers={"x-api-key": "key-a"},
    )
    assert spoof.status_code == 403
    approved = await actions.post(
        f"/v1/actions/{created['id']}/approve", json={}, headers={"x-api-key": "key-b"}
    )
    assert approved.status_code == 200
    async with session_factory() as session:
        actors = (
            (
                await session.execute(
                    text("SELECT actor FROM audit_log WHERE target_id = :id ORDER BY id"),
                    {"id": created["id"]},
                )
            )
            .scalars()
            .all()
        )
    assert actors == ["alice", "api-key"]
