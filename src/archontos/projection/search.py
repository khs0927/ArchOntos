"""Cosine similarity search over ``embedding_projection``.

The query is embedded with the configured embedder and compared only against rows written
by the same ``embedding_model``; vectors from different models are not comparable. The
``embedding IS NOT NULL`` predicate matches the partial HNSW index from migration 010.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from archontos.projection.embedder import Embedder, fit_dimension, to_pgvector

MAX_LIMIT = 100


@dataclass(frozen=True, slots=True)
class SimilarityHit:
    projection_key: str
    source_type: str
    source_id: str
    content: str
    score: float

    def as_api(self) -> dict[str, object]:
        return {
            "projection_key": self.projection_key,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "content": self.content,
            "score": self.score,
        }


async def similar(
    session_factory: async_sessionmaker,
    embedder: Embedder,
    query: str,
    *,
    limit: int = 10,
    source_type: str | None = None,
    min_score: float | None = None,
) -> list[SimilarityHit]:
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be in [1, {MAX_LIMIT}]")
    if not query.strip():
        raise ValueError("query must not be blank")
    vectors = await asyncio.to_thread(embedder.embed, [query])
    literal = to_pgvector(fit_dimension(vectors[0]))
    async with session_factory() as session:
        async with session.begin():
            # HNSW returns at most ef_search candidates; keep it >= limit. With the model and
            # source_type filters applied after the index scan, pgvector >= 0.8 iterative scans
            # keep searching until enough rows pass (strict_order keeps exact ordering).
            await session.execute(
                text(
                    "SELECT set_config('hnsw.ef_search', :ef, true), "
                    "set_config('hnsw.iterative_scan', 'strict_order', true)"
                ),
                {"ef": str(max(40, limit))},
            )
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT projection_key, source_type, source_id, content,
                               1 - (embedding <=> CAST(:q AS vector)) AS score
                        FROM embedding_projection
                        WHERE embedding IS NOT NULL
                          AND embedding_model = :model
                          AND (CAST(:source_type AS text) IS NULL
                               OR source_type = CAST(:source_type AS text))
                        ORDER BY embedding <=> CAST(:q AS vector)
                        LIMIT :limit
                        """
                    ),
                    {
                        "q": literal,
                        "model": embedder.model_id,
                        "source_type": source_type,
                        "limit": limit,
                    },
                )
            ).all()
    hits = [
        SimilarityHit(
            projection_key=row.projection_key,
            source_type=row.source_type,
            source_id=row.source_id,
            content=row.content,
            score=float(row.score),
        )
        for row in rows
    ]
    if min_score is not None:
        hits = [hit for hit in hits if hit.score >= min_score]
    return hits
