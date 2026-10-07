"""Cosine similarity search over ``embedding_projection``, paginated.

The query is embedded with the configured embedder and compared only against rows written
by the same ``embedding_model``; vectors from different models are not comparable. The
``embedding IS NOT NULL`` predicate matches the partial HNSW index from migration 010.

Pagination uses an opaque cursor bound to the query (model, text and filters), so a cursor
cannot be replayed against a different search. Approximate search cannot seek, so a page is
``OFFSET``-based and the total depth (offset + limit) is capped at ``MAX_DEPTH``. Rows written
between two page requests, or exact distance ties, can shift results across a page boundary.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from archontos.projection.embedder import Embedder, fit_dimension, to_pgvector

MAX_LIMIT = 100
MAX_DEPTH = 1000


class InvalidCursor(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class HnswQueryOptions:
    ef_search: int = 40
    iterative_scan: Literal["off", "strict_order", "relaxed_order"] = "strict_order"
    max_scan_tuples: int = 20000


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


@dataclass(frozen=True, slots=True)
class SimilarityPage:
    hits: list[SimilarityHit] = field(default_factory=list)
    next_cursor: str | None = None


def _fingerprint(
    model_id: str, query: str, source_type: str | None, min_score: float | None
) -> str:
    material = json.dumps([model_id, query, source_type, min_score], ensure_ascii=False)
    return hashlib.sha256(material.encode()).hexdigest()[:24]


def encode_cursor(offset: int, fingerprint: str) -> str:
    raw = json.dumps({"o": offset, "f": fingerprint}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, fingerprint: str) -> int:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        offset = int(data["o"])
        presented = str(data["f"])
    except (binascii.Error, ValueError, KeyError, TypeError) as exc:
        raise InvalidCursor("malformed cursor") from exc
    if presented != fingerprint:
        raise InvalidCursor("cursor belongs to a different search")
    if not 0 <= offset < MAX_DEPTH:
        raise InvalidCursor("cursor offset out of range")
    return offset


async def similar_page(
    session_factory: async_sessionmaker,
    embedder: Embedder,
    query: str,
    *,
    limit: int = 10,
    source_type: str | None = None,
    min_score: float | None = None,
    cursor: str | None = None,
    options: HnswQueryOptions | None = None,
) -> SimilarityPage:
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be in [1, {MAX_LIMIT}]")
    if not query.strip():
        raise ValueError("query must not be blank")
    opts = options or HnswQueryOptions()
    fingerprint = _fingerprint(embedder.model_id, query, source_type, min_score)
    offset = decode_cursor(cursor, fingerprint) if cursor else 0
    limit = min(limit, MAX_DEPTH - offset)

    vectors = await asyncio.to_thread(embedder.embed, [query])
    literal = to_pgvector(fit_dimension(vectors[0]))
    async with session_factory() as session:
        async with session.begin():
            # HNSW yields at most ef_search candidates, so cover the whole requested depth.
            # Iterative scans (pgvector >= 0.8) keep scanning while filters reject rows.
            await session.execute(
                text(
                    "SELECT set_config('hnsw.ef_search', :ef, true), "
                    "set_config('hnsw.iterative_scan', :scan, true), "
                    "set_config('hnsw.max_scan_tuples', :tuples, true)"
                ),
                {
                    "ef": str(min(1000, max(opts.ef_search, offset + limit))),
                    "scan": opts.iterative_scan,
                    "tuples": str(opts.max_scan_tuples),
                },
            )
            # Fetch one extra row to know whether another page exists.
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
                          AND (CAST(:min_score AS float8) IS NULL
                               OR 1 - (embedding <=> CAST(:q AS vector))
                                  >= CAST(:min_score AS float8))
                        -- Distance only: a tie-breaker column would stop the HNSW index
                        -- from serving the ORDER BY. Pages over an ANN result are
                        -- approximate by nature (documented in the module docstring).
                        ORDER BY embedding <=> CAST(:q AS vector)
                        LIMIT :fetch OFFSET :offset
                        """
                    ),
                    {
                        "q": literal,
                        "model": embedder.model_id,
                        "source_type": source_type,
                        "min_score": min_score,
                        "fetch": limit + 1,
                        "offset": offset,
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
        for row in rows[:limit]
    ]
    more = len(rows) > limit and offset + limit < MAX_DEPTH
    return SimilarityPage(
        hits=hits, next_cursor=encode_cursor(offset + limit, fingerprint) if more else None
    )


async def similar(
    session_factory: async_sessionmaker,
    embedder: Embedder,
    query: str,
    *,
    limit: int = 10,
    source_type: str | None = None,
    min_score: float | None = None,
    options: HnswQueryOptions | None = None,
) -> list[SimilarityHit]:
    """First page only (kept for callers that do not paginate)."""
    page = await similar_page(
        session_factory,
        embedder,
        query,
        limit=limit,
        source_type=source_type,
        min_score=min_score,
        options=options,
    )
    return page.hits
