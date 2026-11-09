import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response

from archontos.authz import (
    AuthorizationError,
    Permission,
    authorize,
    parse_actor_jurisdictions,
    parse_actor_roles,
)
from archontos.config import get_settings
from archontos.db.roles import verify_service_login
from archontos.identity import (
    ANONYMOUS,
    UNNAMED_KEY_ACTOR,
    InvalidActor,
    Principal,
    current_principal,
    match_key,
    parse_api_keys,
    reset_principal,
    set_principal,
    validate_actor,
)
from archontos.ingestion.persistence import CanonicalizationError
from archontos.observability import REQUEST_COUNT, REQUEST_LOGGER, configure_logging
from archontos.oidc import OidcError, get_verifier
from archontos.rules.engine import RuleEvaluationError

OPEN_PATHS = {"/health", "/metrics"}


def api_keys() -> dict[str, str]:
    return parse_api_keys(get_settings().api_keys)


def key_matches(presented: str, keys: list[str] | dict[str, str]) -> bool:
    mapping = keys if isinstance(keys, dict) else dict.fromkeys(keys, UNNAMED_KEY_ACTOR)
    return match_key(presented, mapping) is not None


def resolve_principal(request: Request, keys: dict[str, str]) -> Principal | None:
    """Return the request principal, or None when authentication is required and fails."""
    if keys:
        actor = match_key(request.headers.get("x-api-key", ""), keys)
        if actor is None:
            return None
        settings = get_settings()
        roles = parse_actor_roles(settings.actor_roles).get(actor, frozenset())
        jurisdictions = parse_actor_jurisdictions(settings.actor_jurisdictions).get(actor)
        return Principal(actor=actor, authenticated=True, roles=roles, jurisdictions=jurisdictions)
    hint = request.headers.get("x-actor", "")
    try:
        actor = validate_actor(hint) if hint else ANONYMOUS
    except InvalidActor:
        actor = ANONYMOUS
    return Principal(actor=actor, authenticated=False)


_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_request_log = logging.getLogger(REQUEST_LOGGER)


async def authenticate(request: Request, keys: dict[str, str]) -> tuple[Principal | None, str]:
    """Return ``(principal, reason)``; the principal is None when authentication fails.

    A ``Bearer`` token is verified when OIDC is enabled; otherwise (and for ``X-API-Key``) the
    API-key path decides. With OIDC enabled and no API keys, anonymous access is refused.
    """
    verifier = get_verifier()
    auth = request.headers.get("authorization", "")
    if verifier is not None and auth[:7].lower() == "bearer ":
        try:
            verified = await asyncio.to_thread(verifier.verify, auth[7:].strip())
        except OidcError as exc:
            _request_log.warning(
                "oidc rejected", extra={"event": "auth_failed", "reason": str(exc)}
            )
            return None, "invalid bearer token"
        return verified, "oidc"
    if keys:
        principal = resolve_principal(request, keys)
        return principal, "api-key" if principal else "missing or invalid api key"
    if verifier is not None:
        return None, "missing bearer token"
    return resolve_principal(request, keys), "open"


def authorization_enabled() -> bool:
    return bool(get_settings().actor_roles.strip())


def require(permission: Permission):
    """FastAPI dependency: the request principal must hold ``permission`` (when enabled)."""

    async def _check() -> Principal | None:
        principal = current_principal()
        authorize(principal, permission, enabled=authorization_enabled())
        return principal

    return Depends(_check)


async def _authorization_error(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AuthorizationError)  # noqa: S101 - registered for this type only
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


def route_label(request: Request) -> str:
    """Use the route template (``/v1/actions/{action_id}/approve``), never the raw path.

    Raw paths embed ids, which would give the Prometheus counter unbounded cardinality.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "unmatched"


async def _unprocessable(_request: Request, exc: Exception) -> JSONResponse:
    # Invalid rule documents and non-canonicalizable source records are client/data errors that
    # must fail closed with a reason, not surface as an opaque HTTP 500.
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    get_verifier()  # a partial ARCHONTOS_OIDC_* configuration fails here, before serving
    await verify_service_login(settings.database_url, settings.db_privilege_check)
    yield


def create_service(name: str) -> FastAPI:
    app = FastAPI(title=f"ArchOntos {name}", version="0.1.0", lifespan=_lifespan)
    app.add_exception_handler(RuleEvaluationError, _unprocessable)
    app.add_exception_handler(CanonicalizationError, _unprocessable)
    app.add_exception_handler(AuthorizationError, _authorization_error)

    @app.middleware("http")
    async def _observe(request: Request, call_next):
        started = time.perf_counter()
        settings = get_settings()
        configure_logging(settings.log_format, settings.log_level)
        supplied = request.headers.get("x-request-id", "")
        request_id = supplied if _REQUEST_ID_RE.fullmatch(supplied) else uuid4().hex
        keys = api_keys()
        reason = "open-path"
        if request.url.path in OPEN_PATHS:
            principal: Principal | None = Principal(actor=ANONYMOUS, authenticated=False)
        else:
            principal, reason = await authenticate(request, keys)
        if principal is None:
            REQUEST_COUNT.labels(service=name, route="unauthorized").inc()
            response: Response = JSONResponse(status_code=401, content={"detail": reason})
            if reason == "invalid bearer token" or reason == "missing bearer token":
                response.headers["www-authenticate"] = "Bearer"
            actor = ANONYMOUS
        else:
            token = set_principal(principal)
            try:
                response = await call_next(request)
            finally:
                reset_principal(token)
            actor = principal.actor
            REQUEST_COUNT.labels(service=name, route=route_label(request)).inc()
            response.headers["x-archontos-actor"] = actor
        response.headers["x-request-id"] = request_id
        if request.url.path not in OPEN_PATHS:
            _request_log.info(
                "request",
                extra={
                    "event": "http_request",
                    "service": name,
                    "request_id": request_id,
                    "method": request.method,
                    "route": route_label(request),
                    "status": response.status_code,
                    "actor": actor,
                    "authenticated": principal.authenticated if principal else False,
                    "auth": reason,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
        return response

    @app.get("/health", tags=["system"])
    async def health():
        return {"status": "ok", "service": name}

    @app.get("/metrics", include_in_schema=False)
    async def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app
