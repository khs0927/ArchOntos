"""Paginated similarity search and HNSW build-parameter rebuild."""

from __future__ import annotations

import json

import asyncpg
import pytest
from sqlalchemy import text

from archontos.db.vector_index import INDEX_NAME, rebuild, show
from archontos.projection.embedder import HashingEmbedder
from archontos.projection.search import (
    HnswQueryOptions,
    InvalidCursor,
    encode_cursor,
    similar_page,
)
from archontos.projection.worker import ProjectionWorker

pytestmark = pytest.mark.asyncio

DOCS = [f"건축물 높이 제한 조항 {i}" for i in range(7)]


async def _seed(session_factory):
    async with session_factory() as session, session.begin():
        for i, body in enumerate(DOCS):
            await session.execute(
                text(
                    """
                    INSERT INTO domain_event(
                        aggregate_type, aggregate_id, event_type, payload_json
                    )
                    VALUES ('provision', :agg, 'ProvisionNormalized', CAST(:p AS jsonb))
                    """
                ),
                {"agg": f"p{i}", "p": json.dumps({"text": body}, ensure_ascii=False)},
            )
    await ProjectionWorker(session_factory, embedder=HashingEmbedder()).rebuild()


async def test_pages_cover_every_row_once(mvp0_db):
    session_factory, _ = mvp0_db
    await _seed(session_factory)
    embedder = HashingEmbedder()
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        page = await similar_page(session_factory, embedder, "높이 제한", limit=3, cursor=cursor)
        seen += [hit.source_id for hit in page.hits]
        pages += 1
        cursor = page.next_cursor
        if cursor is None:
            break
    assert pages == 3
    assert sorted(seen) == sorted(f"p{i}" for i in range(7))
    assert len(seen) == len(set(seen))


async def test_cursor_is_bound_to_the_query(mvp0_db):
    session_factory, _ = mvp0_db
    await _seed(session_factory)
    embedder = HashingEmbedder()
    page = await similar_page(session_factory, embedder, "높이", limit=2)
    assert page.next_cursor is not None
    with pytest.raises(InvalidCursor, match="different search"):
        await similar_page(session_factory, embedder, "주차", limit=2, cursor=page.next_cursor)
    with pytest.raises(InvalidCursor, match="malformed"):
        await similar_page(session_factory, embedder, "높이", limit=2, cursor="!!!")
    with pytest.raises(InvalidCursor):
        await similar_page(
            session_factory, embedder, "높이", limit=2, cursor=encode_cursor(5000, "x")
        )


async def test_min_score_is_applied_before_paging(mvp0_db):
    session_factory, _ = mvp0_db
    await _seed(session_factory)
    page = await similar_page(
        session_factory, HashingEmbedder(), "높이 제한", limit=100, min_score=0.999
    )
    assert page.hits == []
    assert page.next_cursor is None


async def test_query_options_are_accepted(mvp0_db):
    session_factory, _ = mvp0_db
    await _seed(session_factory)
    page = await similar_page(
        session_factory,
        HashingEmbedder(),
        "높이",
        limit=7,
        options=HnswQueryOptions(ef_search=10, iterative_scan="relaxed_order", max_scan_tuples=500),
    )
    assert len(page.hits) == 7


async def test_hnsw_rebuild_with_build_parameters(mvp0_db, postgres_dsn):
    session_factory, schema = mvp0_db
    await _seed(session_factory)
    admin = await asyncpg.connect(postgres_dsn)
    try:
        await admin.execute(f'SET search_path TO "{schema}", public')
        assert await show(admin) == {}  # migration 010: pgvector defaults
        assert await rebuild(admin, m=24, ef_construction=128) == {
            "m": "24",
            "ef_construction": "128",
        }
        with pytest.raises(ValueError):
            await rebuild(admin, m=24, ef_construction=10)
        names = await admin.fetch(
            "SELECT indexname FROM pg_indexes WHERE schemaname = $1 AND indexname LIKE 'hnsw%'",
            schema,
        )
        assert [row["indexname"] for row in names] == [INDEX_NAME]
    finally:
        await admin.close()
    page = await similar_page(session_factory, HashingEmbedder(), "높이", limit=7)
    assert len(page.hits) == 7
