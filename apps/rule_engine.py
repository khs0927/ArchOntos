from datetime import date
from time import perf_counter
from typing import Annotated
from uuid import UUID

from fastapi import HTTPException, Query

from apps.common import create_service, require
from archontos.authz import Permission
from archontos.db.session import get_session_factory
from archontos.domain.contracts import RuleEvaluationRequest, RuleEvaluationResult
from archontos.observability import RULE_EVAL_LATENCY
from archontos.query.contracts import (
    ApplicabilityView,
    AuthorityClassificationView,
    DecisionProvenanceView,
    JurisdictionComparisonView,
    QueryClassificationView,
    SourceEvidenceView,
    TemporalComparisonView,
)
from archontos.query.persistence import CanonicalQueryError, CanonicalQueryNotFound
from archontos.query.router import Mvp0QueryRouter
from archontos.query.service import CanonicalQueryService
from archontos.rules.compiler import RuleCompilationError
from archontos.rules.contracts import (
    CanonicalEvaluationRequest,
    CanonicalEvaluationView,
    RuleCompilationView,
)
from archontos.rules.engine import evaluate_rule
from archontos.rules.evaluation import RuleNotExecutableError, RuleVersionNotFoundError
from archontos.rules.evaluation_service import CanonicalEvaluationService
from archontos.rules.persistence import (
    AssertionNotApprovedError,
    RuleAssertionNotFoundError,
)
from archontos.rules.service import RuleCompilationService

app = create_service("rule-engine")


def _queries() -> CanonicalQueryService:
    return CanonicalQueryService(session_factory=get_session_factory())


@app.post(
    "/v1/evaluate",
    dependencies=[require(Permission.QUERY_READ)],
    response_model=RuleEvaluationResult,
)
async def evaluate(payload: RuleEvaluationRequest):
    started = perf_counter()
    try:
        return evaluate_rule(payload.rule, payload.facts)
    finally:
        RULE_EVAL_LATENCY.observe(perf_counter() - started)


@app.post(
    "/v1/rules/evaluate/{rule_version_id}",
    dependencies=[require(Permission.QUERY_READ)],
    response_model=CanonicalEvaluationView,
)
async def evaluate_canonical_rule(
    rule_version_id: UUID,
    payload: CanonicalEvaluationRequest,
):
    service = CanonicalEvaluationService(session_factory=get_session_factory())
    started = perf_counter()
    try:
        result = await service.evaluate(
            rule_version_id=rule_version_id,
            facts=payload.facts,
            object_version_id=payload.object_version_id,
            evaluated_at=payload.evaluated_at,
        )
    except RuleVersionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuleNotExecutableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        RULE_EVAL_LATENCY.observe(perf_counter() - started)

    return CanonicalEvaluationView(
        evaluation_id=result.evaluation_id,
        decision_id=result.decision_id,
        rule_version_id=result.rule_version_id,
        applicable=result.applicable,
        outcome=result.outcome,
        reason=result.reason,
        details=result.details,
        evaluated_at=result.evaluated_at,
        binding=result.binding,
    )


@app.post(
    "/v1/rules/compile/{assertion_id}",
    dependencies=[require(Permission.RULE_COMPILE)],
    response_model=RuleCompilationView,
)
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


@app.get(
    "/v1/query/classify",
    dependencies=[require(Permission.QUERY_READ)],
    response_model=QueryClassificationView,
)
async def classify_query(query: Annotated[str, Query(min_length=1)]):
    return QueryClassificationView(
        query=query,
        intent=Mvp0QueryRouter().classify(query),
    )


@app.get(
    "/v1/query/source-evidence/{rule_version_id}",
    dependencies=[require(Permission.QUERY_READ)],
    response_model=SourceEvidenceView,
)
async def query_source_evidence(rule_version_id: UUID):
    try:
        return await _queries().source_evidence(rule_version_id)
    except CanonicalQueryNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get(
    "/v1/query/decision-provenance/{decision_id}",
    dependencies=[require(Permission.QUERY_READ)],
    response_model=DecisionProvenanceView,
)
async def query_decision_provenance(decision_id: UUID):
    try:
        return await _queries().decision_provenance(decision_id)
    except CanonicalQueryNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get(
    "/v1/query/authority/{rule_version_id}",
    dependencies=[require(Permission.QUERY_READ)],
    response_model=AuthorityClassificationView,
)
async def query_authority(rule_version_id: UUID):
    try:
        return await _queries().authority(rule_version_id)
    except CanonicalQueryNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get(
    "/v1/query/applicability/{rule_version_id}",
    dependencies=[require(Permission.QUERY_READ)],
    response_model=ApplicabilityView,
)
async def query_applicability(rule_version_id: UUID):
    try:
        return await _queries().applicability(rule_version_id)
    except CanonicalQueryNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get(
    "/v1/query/temporal",
    dependencies=[require(Permission.QUERY_READ)],
    response_model=TemporalComparisonView,
)
async def query_temporal_comparison(
    source_key: Annotated[str, Query(min_length=1)],
    left_date: date,
    right_date: date,
):
    try:
        return await _queries().temporal_comparison(
            source_key=source_key,
            left_date=left_date,
            right_date=right_date,
        )
    except CanonicalQueryNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CanonicalQueryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get(
    "/v1/query/jurisdiction",
    dependencies=[require(Permission.QUERY_READ)],
    response_model=JurisdictionComparisonView,
)
async def query_jurisdiction_comparison(
    rule_title: Annotated[str, Query(min_length=1)],
    left_jurisdiction: Annotated[str, Query(min_length=2)],
    right_jurisdiction: Annotated[str, Query(min_length=2)],
    at_date: date,
):
    try:
        return await _queries().jurisdiction_comparison(
            rule_title=rule_title,
            left_jurisdiction=left_jurisdiction,
            right_jurisdiction=right_jurisdiction,
            at_date=at_date,
        )
    except CanonicalQueryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
