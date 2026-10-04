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


@pytest.mark.parametrize(
    "condition",
    [
        {">=": [{"var": "stair.label"}, 2]},  # str vs int
        {"<": [{"var": "stair.missing"}, 2]},  # missing fact (None) vs int
        {"in": [1, {"var": "stair.direct_count"}]},  # membership in an int
        {">": [1, 2, 3]},  # wrong arity
    ],
    ids=["str-vs-int", "missing-fact", "in-non-container", "arity"],
)
def test_type_mismatch_is_a_rule_error_not_a_crash(condition):
    with pytest.raises(RuleEvaluationError, match="Operator"):
        evaluate_rule(_rule(condition), {"stair": {"direct_count": 1, "label": "two"}})


def test_rule_engine_api_returns_422_for_type_mismatch():
    from apps.rule_engine import app

    client = TestClient(app)
    res = client.post(
        "/v1/evaluate",
        json={
            "rule": _rule({">=": [{"var": "stair.label"}, 2]}),
            "facts": {"stair": {"label": "two"}},
        },
    )
    assert res.status_code == 422
    assert "cannot compare str with int" in res.json()["detail"]


def test_numeric_comparisons_still_mix_int_and_float():
    rule = _rule({">=": [{"var": "stair.width"}, 1.2]})
    assert evaluate_rule(rule, {"stair": {"width": 2}}).outcome is DecisionOutcome.PASS
    assert evaluate_rule(_rule({"in": ["KR", ["KR", "JP"]]}), {}).outcome is DecisionOutcome.PASS
