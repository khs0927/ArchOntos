from time import perf_counter
from uuid import UUID

from fastapi import HTTPException

from apps.common import create_service
from archontos.db.session import get_session_factory
from archontos.domain.contracts import RuleEvaluationRequest, RuleEvaluationResult
from archontos.observability import RULE_EVAL_LATENCY
from archontos.rules.compiler import RuleCompilationError
from archontos.rules.contracts import RuleCompilationView
from archontos.rules.engine import evaluate_rule
from archontos.rules.persistence import (
    AssertionNotApprovedError,
    RuleAssertionNotFoundError,
)
from archontos.rules.service import RuleCompilationService

app = create_service("rule-engine")


@app.post("/v1/evaluate", response_model=RuleEvaluationResult)
async def evaluate(payload: RuleEvaluationRequest):
    started = perf_counter()
    try:
        return evaluate_rule(payload.rule, payload.facts)
    finally:
        RULE_EVAL_LATENCY.observe(perf_counter() - started)


@app.post("/v1/rules/compile/{assertion_id}", response_model=RuleCompilationView)
async def compile_assertion(assertion_id: UUID):
    service = RuleCompilationService(session_factory=get_session_factory())
    try:
        result = await service.compile_assertion(assertion_id)
    except RuleAssertionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AssertionNotApprovedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuleCompilationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return RuleCompilationView(
        rule_id=result.rule_id,
        rule_version_id=result.rule_version_id,
        assertion_id=result.assertion_id,
        status=result.status,
        created=result.created,
    )
