"""OIDC bearer-token authentication (external IdP).

Enabled by ``ARCHONTOS_OIDC_ISSUER`` + ``ARCHONTOS_OIDC_AUDIENCE``. A request carrying
``Authorization: Bearer <jwt>`` is verified against the issuer's JWKS and becomes a
:class:`~archontos.identity.Principal`. Verification fails closed: the algorithm allowlist is
fixed by configuration (never read from the token header, ``none``/HMAC are refused), and
``iss``, ``aud``, ``exp`` are required. A bad bearer token never falls back to another method.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import jwt
from jwt import PyJWK

from archontos.authz import ROLE_PERMISSIONS
from archontos.identity import InvalidActor, Principal, validate_actor

ASYMMETRIC_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"}
)
JWKS_MIN_REFETCH_SECONDS = 30.0

JwksFetcher = Callable[[str], Awaitable[dict[str, Any]]]


class OidcError(Exception):
    """The bearer token is missing, malformed, expired, or otherwise not acceptable."""


async def _http_fetch_jwks(url: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, dict) or not isinstance(data.get("keys"), list):
        raise OidcError("jwks document has no keys")
    return data


class OidcVerifier:
    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str,
        algorithms: tuple[str, ...] = ("RS256", "ES256"),
        actor_claim: str = "preferred_username",
        roles_claim: str = "roles",
        jurisdictions_claim: str = "",
        leeway: int = 30,
        jwks_ttl: float = 300.0,
        fetcher: JwksFetcher | None = None,
    ) -> None:
        bad = [a for a in algorithms if a not in ASYMMETRIC_ALGORITHMS]
        if not algorithms or bad:
            raise ValueError(f"OIDC algorithms must be asymmetric, got {list(algorithms)!r}")
        self.issuer = issuer
        self.audience = audience
        self.jwks_url = jwks_url
        self.algorithms = algorithms
        self.actor_claim = actor_claim
        self.roles_claim = roles_claim
        self.jurisdictions_claim = jurisdictions_claim
        self.leeway = leeway
        self._ttl = jwks_ttl
        self._fetch = fetcher or _http_fetch_jwks
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    async def _refresh(self, *, force: bool) -> None:
        async with self._lock:
            age = time.monotonic() - self._fetched_at
            if self._keys and not force and age < self._ttl:
                return
            if force and self._keys and age < JWKS_MIN_REFETCH_SECONDS:
                return  # unknown kid cannot be used to hammer the IdP
            try:
                document = await self._fetch(self.jwks_url)
            except (httpx.HTTPError, ValueError, OidcError) as exc:
                raise OidcError("identity provider keys unavailable") from exc
            keys: dict[str, PyJWK] = {}
            for entry in document.get("keys", []):
                kid = entry.get("kid") if isinstance(entry, dict) else None
                if not isinstance(kid, str) or entry.get("use", "sig") != "sig":
                    continue
                try:
                    keys[kid] = PyJWK(entry)
                except jwt.PyJWTError:
                    continue
            self._keys = keys
            self._fetched_at = time.monotonic()

    async def _signing_key(self, kid: str) -> PyJWK:
        await self._refresh(force=False)
        if kid not in self._keys:
            await self._refresh(force=True)
        key = self._keys.get(kid)
        if key is None:
            raise OidcError("unknown signing key")
        return key

    async def verify(self, token: str) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise OidcError("malformed token") from exc
        kid, alg = header.get("kid"), header.get("alg")
        if not isinstance(kid, str) or alg not in self.algorithms:
            raise OidcError("unsupported token header")
        key = await self._signing_key(kid)
        try:
            claims = jwt.decode(
                token,
                key.key,
                algorithms=list(self.algorithms),
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.leeway,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            raise OidcError("token rejected") from exc
        return self._principal(claims)

    def _principal(self, claims: dict[str, Any]) -> Principal:
        raw_actor = claims.get(self.actor_claim) or claims.get("sub")
        if not isinstance(raw_actor, str):
            raise OidcError("token has no usable subject")
        try:
            actor = validate_actor(raw_actor)
        except InvalidActor as exc:
            raise OidcError("token subject is not a valid actor") from exc
        roles = frozenset(
            r for r in _as_list(claims.get(self.roles_claim)) if r in ROLE_PERMISSIONS
        )
        jurisdictions: tuple[str, ...] | None = None
        if self.jurisdictions_claim and self.jurisdictions_claim in claims:
            jurisdictions = tuple(sorted(set(_as_list(claims[self.jurisdictions_claim]))))
        return Principal(actor=actor, authenticated=True, roles=roles, jurisdictions=jurisdictions)


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def bearer_token(authorization: str) -> str | None:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


_verifier: OidcVerifier | None = None
_verifier_key: tuple | None = None


def get_verifier() -> OidcVerifier | None:
    """Process-wide verifier built from settings; None when OIDC is not configured."""
    global _verifier, _verifier_key  # noqa: PLW0603 - cache keeps the JWKS between requests
    from archontos.config import get_settings

    s = get_settings()
    if not (s.oidc_issuer and s.oidc_audience):
        return None
    algorithms = tuple(a.strip() for a in s.oidc_algorithms.split(",") if a.strip())
    jwks_url = s.oidc_jwks_url or s.oidc_issuer.rstrip("/") + "/.well-known/jwks.json"
    cache_key = (
        s.oidc_issuer, s.oidc_audience, jwks_url, algorithms, s.oidc_actor_claim,
        s.oidc_roles_claim, s.oidc_jurisdictions_claim,
    )  # fmt: skip
    if _verifier is None or _verifier_key != cache_key:
        _verifier = OidcVerifier(
            issuer=s.oidc_issuer,
            audience=s.oidc_audience,
            jwks_url=jwks_url,
            algorithms=algorithms,
            actor_claim=s.oidc_actor_claim,
            roles_claim=s.oidc_roles_claim,
            jurisdictions_claim=s.oidc_jurisdictions_claim,
        )
        _verifier_key = cache_key
    return _verifier
