import pytest
from fastapi.testclient import TestClient

from archontos.domain.enums import DecisionOutcome
from archontos.rules.engine import RuleEvaluationError, evaluate_rule


def _rule(condition):
    return {"rule": {"if": condition, "then": {"PASS": True}, "else": {"FAIL": {"reason": "x"}}}}


FACTS = {"stair": {"direct_count": 1}}


@pytest.mark.parametrize(
    "condition",
    [
        # A stray key next to the operator used to turn the whole condition into a truthy dict.
        {">=": [{"var": "stair.direct_count"}, 2], "note": "minimum two stairs"},
        {},
        {"and": [{">=": [{"var": "stair.direct_count"}, 2], "comment": "x"}]},
    ],
    ids=["extra-key", "empty", "nested-extra-key"],
)
def test_malformed_condition_fails_closed_instead_of_passing(condition):
    with pytest.raises(RuleEvaluationError, match="exactly one operator"):
        evaluate_rule(_rule(condition), FACTS)


def test_malformed_applicability_condition_fails_closed():
    rule = _rule({">=": [{"var": "stair.direct_count"}, 0]})
    rule["applicability"] = {"conditions": [{"==": [1, 1], "why": "always"}]}
    with pytest.raises(RuleEvaluationError):
        evaluate_rule(rule, FACTS)


def test_literal_objects_still_compare():
    rule = _rule({"==": [{"var": "meta"}, {"literal": {"a": 1, "b": 2}}]})
    assert evaluate_rule(rule, {"meta": {"a": 1, "b": 2}}).outcome is DecisionOutcome.PASS


def test_well_formed_rule_unchanged():
    rule = _rule({">=": [{"var": "stair.direct_count"}, 2]})
    assert evaluate_rule(rule, FACTS).outcome is DecisionOutcome.FAIL
    assert evaluate_rule(rule, {"stair": {"direct_count": 3}}).outcome is DecisionOutcome.PASS


def test_rule_engine_api_returns_422_for_invalid_rules():
    from apps.rule_engine import app

    client = TestClient(app)
    bad_operator = {"rule": _rule({"magic": [1, 2]}), "facts": FACTS}
    res = client.post("/v1/evaluate", json=bad_operator)
    assert res.status_code == 422
    assert "Unsupported rule operator" in res.json()["detail"]

    malformed = {"rule": _rule({">=": [1, 0], "note": "x"}), "facts": FACTS}
    assert client.post("/v1/evaluate", json=malformed).status_code == 422

    ok = {"rule": _rule({">=": [{"var": "stair.direct_count"}, 1]}), "facts": FACTS}
    res = client.post("/v1/evaluate", json=ok)
    assert res.status_code == 200 and res.json()["outcome"] == "PASS"
