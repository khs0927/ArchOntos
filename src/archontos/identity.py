"""Per-request identity (P3 groundwork).

``ARCHONTOS_API_KEYS`` accepts ``key`` or ``actor:key`` entries. A request authenticated
with a named key runs as that actor; an unnamed key runs as ``api-key``. With no keys
configured (local MVP-0) the ``X-Actor`` header is trusted as a hint, else ``anonymous``.

The actor lives in a ``ContextVar`` for the request and is copied into PostgreSQL per
transaction with ``set_config('app.actor', ..., true)`` (transaction-local), matching the
``app.*`` session-attribute convention of ``db/templates/rls_policy_template.sql``.
"""

from __future__ import annotations

import hmac
import re
from contextvars import ContextVar
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

ANONYMOUS = "anonymous"
UNNAMED_KEY_ACTOR = "api-key"
_ACTOR_RE = re.compile(r"^[A-Za-z0-9._@-]{1,128}$")


@dataclass(frozen=True, slots=True)
class Principal:
    actor: str
    authenticated: bool


_current: ContextVar[Principal | None] = ContextVar("archontos_principal", default=None)


class InvalidActor(ValueError):
    pass


def validate_actor(value: str) -> str:
    if not _ACTOR_RE.fullmatch(value):
        raise InvalidActor("actor must match [A-Za-z0-9._@-]{1,128}")
    return value


def parse_api_keys(raw: str) -> dict[str, str]:
    """Map each configured key to its actor. ``actor:key`` names the key; bare keys are unnamed."""
    keys: dict[str, str] = {}
    for item in raw.split(","):
        entry = item.strip()
        if not entry:
            continue
        actor, sep, key = entry.partition(":")
        if sep and actor.strip() and key.strip():
            keys[key.strip()] = validate_actor(actor.strip())
        else:
            keys[entry] = UNNAMED_KEY_ACTOR
    return keys


def match_key(presented: str, keys: dict[str, str]) -> str | None:
    """Return the actor for ``presented``; compares against every key in constant time."""
    encoded = presented.encode()
    found: str | None = None
    for key, actor in keys.items():
        if hmac.compare_digest(encoded, key.encode()):
            found = actor
    return found


def set_principal(principal: Principal | None):
    return _current.set(principal)


def reset_principal(token) -> None:
    _current.reset(token)


def current_principal() -> Principal | None:
    return _current.get()


def current_actor(default: str = "system") -> str:
    principal = _current.get()
    return principal.actor if principal is not None else default


async def bind_actor(session: AsyncSession, actor: str | None = None) -> None:
    """Expose the request actor to SQL for the current transaction only."""
    await session.execute(
        text("SELECT set_config('app.actor', :actor, true)"),
        {"actor": actor or current_actor()},
    )
