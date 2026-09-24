from time import perf_counter

from apps.common import create_service
from archontos.domain.contracts import RuleEvaluationRequest, RuleEvaluationResult
from archontos.observability import RULE_EVAL_LATENCY
from archontos.rules.engine import evaluate_rule

app = create_service("rule-engine")


@app.post("/v1/evaluate", response_model=RuleEvaluationResult)
async def evaluate(payload: RuleEvaluationRequest):
    started = perf_counter()
    try:
        return evaluate_rule(payload.rule, payload.facts)
    finally:
        RULE_EVAL_LATENCY.observe(perf_counter() - started)
