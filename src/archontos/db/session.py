from __future__ import annotations

from functools import lru_cache
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from archontos.config import get_settings
from archontos.identity import current_actor, current_jurisdictions


class ContextSession(Session):
    """Session whose every transaction carries the request identity into PostgreSQL.

    ``app.actor`` and ``app.allowed_jurisdictions`` are set transaction-locally (the third
    argument of ``set_config``), so pooled connections never leak one request's identity into
    the next. Row-level security policies (migrations 011, 013) read these settings.
    """


@event.listens_for(ContextSession, "after_begin")
def _bind_request_context(_session: Session, _transaction: Any, connection: Any) -> None:
    connection.execute(
        text(
            "SELECT set_config('app.actor', :actor, true), "
            "set_config('app.allowed_jurisdictions', :jurisdictions, true)"
        ),
        {"actor": current_actor(), "jurisdictions": ",".join(current_jurisdictions())},
    )


def install_session_context(
    factory: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    """Return a factory on the same engine whose sessions bind the request context."""
    return async_sessionmaker(
        bind=factory.kw.get("bind"),
        expire_on_commit=False,
        class_=AsyncSession,
        sync_session_class=ContextSession,
    )


def schema_connect_args(schema: str | None) -> dict[str, Any]:
    """asyncpg connect_args that put ``schema`` first on the search path (public stays visible)."""
    if not schema:
        return {}
    from archontos.db.migrate import validate_schema_name

    return {"server_settings": {"search_path": f"{validate_schema_name(schema)},public"}}


@lru_cache
def get_engine():
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args=schema_connect_args(settings.db_schema),
    )


@lru_cache
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        get_engine(),
        expire_on_commit=False,
        class_=AsyncSession,
        sync_session_class=ContextSession,
    )


async def get_session():
    session_factory = get_session_factory()
    async with session_factory() as session:
        yield session
