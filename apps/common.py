from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

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
from archontos.observability import REQUEST_COUNT
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
    await verify_service_login(settings.database_url, settings.db_privilege_check)
    yield


def create_service(name: str) -> FastAPI:
    app = FastAPI(title=f"ArchOntos {name}", version="0.1.0", lifespan=_lifespan)
    app.add_exception_handler(RuleEvaluationError, _unprocessable)
    app.add_exception_handler(CanonicalizationError, _unprocessable)
    app.add_exception_handler(AuthorizationError, _authorization_error)

    @app.middleware("http")
    async def _observe(request: Request, call_next):
        keys = api_keys()
        if request.url.path in OPEN_PATHS:
            principal: Principal | None = Principal(actor=ANONYMOUS, authenticated=False)
        else:
            principal = resolve_principal(request, keys)
        if principal is None:
            REQUEST_COUNT.labels(service=name, route="unauthorized").inc()
            return JSONResponse(status_code=401, content={"detail": "missing or invalid api key"})
        token = set_principal(principal)
        try:
            response = await call_next(request)
        finally:
            reset_principal(token)
        REQUEST_COUNT.labels(service=name, route=route_label(request)).inc()
        response.headers["x-archontos-actor"] = principal.actor
        return response

    @app.get("/health", tags=["system"])
    async def health():
        return {"status": "ok", "service": name}

    @app.get("/metrics", include_in_schema=False)
    async def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app
