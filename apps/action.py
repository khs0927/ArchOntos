from pydantic import BaseModel, Field

from apps.common import create_service
from archontos.actions.service import propose_report
from archontos.domain.enums import QueryIntent
from archontos.query.router import Mvp0QueryRouter

app = create_service("action")
router = Mvp0QueryRouter()


class QueryRequest(BaseModel):
    query: str = Field(min_length=2)


@app.post("/v1/query/classify")
async def classify_query(payload: QueryRequest):
    intent: QueryIntent = router.classify(payload.query)
    return {"intent": intent}


@app.post("/v1/actions/report/propose")
async def report_proposal(target_refs: list[str], context: dict):
    proposal = propose_report(target_refs, context)
    return proposal
