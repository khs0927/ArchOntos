"""Optional OIDC/JWT identity next to the API-key identity.

Disabled unless ``ARCHONTOS_OIDC_ISSUER``, ``ARCHONTOS_OIDC_AUDIENCE`` and
``ARCHONTOS_OIDC_JWKS_URL`` are all set (a partial configuration refuses to start).
A request presenting ``Authorization: Bearer <jwt>`` is verified (RS256 only, signature,
``exp``, ``iss`` and ``aud``) and turned into the same :class:`Principal` the API keys produce:
the actor comes from a configurable claim, roles from a configurable claim (unknown roles are
dropped), jurisdictions from an optional claim. Roles and jurisdictions listed for the same actor
in ``ARCHONTOS_ACTOR_ROLES`` / ``ARCHONTOS_ACTOR_JURISDICTIONS`` are merged in.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import jwt

from archontos.authz import ROLE_PERMISSIONS, parse_actor_jurisdictions, parse_actor_roles
from archontos.config import Settings, get_settings
from archontos.identity import InvalidActor, Principal, validate_actor

ALGORITHMS = ["RS256"]
_SPLIT = re.compile(r"[\s,+]+")


class OidcError(Exception):
    """The bearer token is not acceptable. The message is for logs, never for the client."""


KeyResolver = Callable[[str], Any]


@dataclass(frozen=True, slots=True)
class OidcConfig:
    issuer: str
    audience: str
    jwks_url: str
    actor_claim: str = "sub"
    roles_claim: str = "roles"
    jurisdictions_claim: str = "jurisdictions"
    leeway_seconds: int = 30

    @classmethod
    def from_settings(cls, settings: Settings) -> OidcConfig | None:
        required = (settings.oidc_issuer, settings.oidc_audience, settings.oidc_jwks_url)
        if not any(required):
            return None
        if not all(required):
            raise ValueError(
                "ARCHONTOS_OIDC_ISSUER, ARCHONTOS_OIDC_AUDIENCE and ARCHONTOS_OIDC_JWKS_URL "
                "must be set together"
            )
        return cls(
            issuer=settings.oidc_issuer or "",
            audience=settings.oidc_audience or "",
            jwks_url=settings.oidc_jwks_url or "",
            actor_claim=settings.oidc_actor_claim,
            roles_claim=settings.oidc_roles_claim,
            jurisdictions_claim=settings.oidc_jurisdictions_claim,
            leeway_seconds=settings.oidc_leeway_seconds,
        )


def _as_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part for part in _SPLIT.split(value) if part]
    if isinstance(value, list | tuple):
        return [str(item) for item in value if isinstance(item, str) and item]
    return []


def _claim(claims: dict[str, Any], path: str) -> object:
    """Resolve ``a.b.c`` through nested objects (e.g. Keycloak ``realm_access.roles``)."""
    if path in claims:
        return claims[path]
    node: object = claims
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


class OidcVerifier:
    def __init__(
        self,
        config: OidcConfig,
        key_resolver: KeyResolver | None = None,
        *,
        actor_roles: str = "",
        actor_jurisdictions: str = "",
    ) -> None:
        self.config = config
        self._key_resolver = key_resolver or self._jwks_resolver()
        self._extra_roles = parse_actor_roles(actor_roles)
        self._extra_jurisdictions = parse_actor_jurisdictions(actor_jurisdictions)

    def _jwks_resolver(self) -> KeyResolver:
        client = jwt.PyJWKClient(self.config.jwks_url, cache_keys=True, lifespan=300)

        def resolve(token: str) -> Any:
            return client.get_signing_key_from_jwt(token).key

        return resolve

    def verify(self, token: str) -> Principal:
        """Blocking (may fetch the JWKS); call from a worker thread in async code."""
        try:
            key = self._key_resolver(token)
            claims = jwt.decode(
                token,
                key,
                algorithms=ALGORITHMS,
                audience=self.config.audience,
                issuer=self.config.issuer,
                leeway=self.config.leeway_seconds,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            raise OidcError(f"{type(exc).__name__}: {exc}") from exc
        return self.principal_from_claims(claims)

    def principal_from_claims(self, claims: dict[str, Any]) -> Principal:
        raw_actor = _claim(claims, self.config.actor_claim)
        if not isinstance(raw_actor, str) or not raw_actor:
            raise OidcError(f"missing actor claim {self.config.actor_claim!r}")
        try:
            actor = validate_actor(raw_actor)
        except InvalidActor as exc:
            raise OidcError("actor claim is not a valid actor name") from exc
        roles = {
            r for r in _as_list(_claim(claims, self.config.roles_claim)) if r in ROLE_PERMISSIONS
        }
        roles |= self._extra_roles.get(actor, frozenset())
        raw_juris = _claim(claims, self.config.jurisdictions_claim)
        jurisdictions: tuple[str, ...] | None
        if raw_juris is not None:
            jurisdictions = tuple(sorted(_as_list(raw_juris)))
        else:
            jurisdictions = self._extra_jurisdictions.get(actor)
        return Principal(
            actor=actor,
            authenticated=True,
            roles=frozenset(roles),
            jurisdictions=jurisdictions,
        )


@lru_cache
def _cached_verifier(settings_key: tuple[object, ...]) -> OidcVerifier | None:
    settings = get_settings()
    config = OidcConfig.from_settings(settings)
    if config is None:
        return None
    return OidcVerifier(
        config,
        actor_roles=settings.actor_roles,
        actor_jurisdictions=settings.actor_jurisdictions,
    )


def get_verifier() -> OidcVerifier | None:
    """The process verifier, or None when OIDC is disabled. Rebuilt when the config changes."""
    s = get_settings()
    return _cached_verifier(
        (
            s.oidc_issuer,
            s.oidc_audience,
            s.oidc_jwks_url,
            s.oidc_actor_claim,
            s.oidc_roles_claim,
            s.oidc_jurisdictions_claim,
            s.oidc_leeway_seconds,
            s.actor_roles,
            s.actor_jurisdictions,
        )
    )
