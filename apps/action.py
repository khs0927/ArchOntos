from fastapi import HTTPException
from pydantic import BaseModel, Field

from apps.common import create_service
from archontos.actions.gate import ApprovalDenied
from archontos.actions.persistence import MemoryActionStore, PostgresActionStore
from archontos.actions.service import propose_report
from archontos.config import get_settings
from archontos.db.session import get_session_factory
from archontos.domain.enums import QueryIntent
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
    actor: str = Field(min_length=1)
    reason: str = ""


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
    stored = await _store().propose(proposal)
    body = stored.as_api()
    # Keep the pre-persistence contract the smoke test asserts.
    body["proposed_output"]["status"] = "proposal"
    return body


@app.post("/v1/actions/{action_id}/approve")
async def approve_action(action_id: str, payload: DecisionRequest):
    try:
        stored = await _store().approve(action_id, payload.actor)
    except KeyError:
        raise HTTPException(status_code=404, detail="action not found") from None
    except ApprovalDenied as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return stored.as_api()


@app.post("/v1/actions/{action_id}/reject")
async def reject_action(action_id: str, payload: DecisionRequest):
    try:
        stored = await _store().reject(action_id, payload.actor, payload.reason)
    except KeyError:
        raise HTTPException(status_code=404, detail="action not found") from None
    except ApprovalDenied as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return stored.as_api()


@app.post("/v1/actions/{action_id}/execute")
async def execute_action(action_id: str, payload: DecisionRequest):
    try:
        stored = await _store().execute(action_id, payload.actor)
    except KeyError:
        raise HTTPException(status_code=404, detail="action not found") from None
    except ApprovalDenied as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return stored.as_api()
