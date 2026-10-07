from fastapi import HTTPException
from pydantic import BaseModel, Field

from apps.common import create_service
from archontos.actions.gate import ApprovalDenied
from archontos.actions.persistence import (
    MemoryActionStore,
    PostgresActionStore,
    parse_action_id,
)
from archontos.actions.service import propose_report
from archontos.config import get_settings
from archontos.db.session import get_session_factory
from archontos.domain.enums import QueryIntent
from archontos.identity import InvalidActor, current_actor, current_principal, validate_actor
from archontos.query.router import Mvp0QueryRouter

app = create_service("action")
router = Mvp0QueryRouter()
_memory = MemoryActionStore()


class QueryRequest(BaseModel):
    query: str = Field(min_length=2)


class ReportProposeRequest(BaseModel):
    target_refs: list[str] = Field(min_length=1)
    context: dict = Field(default_factory=dict)


class DecisionRequest(BaseModel):
    # Optional: an authenticated (named API key) request always acts as its own identity.
    actor: str | None = Field(default=None, min_length=1, max_length=128)
    reason: str = ""


def _effective_actor(requested: str | None) -> str:
    principal = current_principal()
    if principal is not None and principal.authenticated:
        if requested is not None and requested != principal.actor:
            raise HTTPException(status_code=403, detail="actor does not match the api key")
        return principal.actor
    if requested is not None:
        try:
            return validate_actor(requested)
        except InvalidActor as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return current_actor(default="anonymous")


def _store():
    settings = get_settings()
    if settings.action_backend == "postgres":
        return PostgresActionStore(get_session_factory())
    return _memory


@app.post("/v1/query/classify")
async def classify_query(payload: QueryRequest):
    intent: QueryIntent = router.classify(payload.query)
    return {"intent": intent}


@app.post("/v1/actions/report/propose")
async def report_proposal(payload: ReportProposeRequest):
    proposal = propose_report(payload.target_refs, payload.context)
    stored = await _store().propose(proposal, created_by=current_actor(default="anonymous"))
    body = stored.as_api()
    # Keep the pre-persistence contract the smoke test asserts.
    body["proposed_output"]["status"] = "proposal"
    return body


def _normalize_id(store, action_id: str) -> str:
    if isinstance(store, PostgresActionStore):
        try:
            return parse_action_id(action_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="action not found") from None
    return action_id


async def _decide(op: str, action_id: str, *args: str):
    store = _store()
    action_id = _normalize_id(store, action_id)
    try:
        stored = await getattr(store, op)(action_id, *args)
    except KeyError:
        raise HTTPException(status_code=404, detail="action not found") from None
    except ApprovalDenied as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return stored.as_api()


@app.get("/v1/actions/{action_id}")
async def get_action(action_id: str):
    store = _store()
    found = await store.get(_normalize_id(store, action_id))
    if found is None:
        raise HTTPException(status_code=404, detail="action not found")
    return found.as_api()


@app.post("/v1/actions/{action_id}/approve")
async def approve_action(action_id: str, payload: DecisionRequest):
    return await _decide("approve", action_id, _effective_actor(payload.actor))


@app.post("/v1/actions/{action_id}/reject")
async def reject_action(action_id: str, payload: DecisionRequest):
    return await _decide("reject", action_id, _effective_actor(payload.actor), payload.reason)


@app.post("/v1/actions/{action_id}/execute")
async def execute_action(action_id: str, payload: DecisionRequest):
    return await _decide("execute", action_id, _effective_actor(payload.actor))
