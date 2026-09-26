import pytest

from archontos.domain.enums import DecisionOutcome
from archontos.rules.engine import RuleEvaluationError, evaluate_rule


def sample_rule():
    return {
        "applicability": {
            "jurisdiction": ["KR", "KR-11"],
            "building_use_groups": ["공동주택"],
            "conditions": [{">=": [{"var": "building.floor_count"}, 7]}],
        },
        "rule": {
            "if": {">=": [{"var": "stair.direct_count"}, 2]},
            "then": {"PASS": {"reason": "minimum satisfied"}},
            "else": {
                "FAIL": {
                    "reason": "직통계단 부족",
                    "required": 2,
                    "actual": {"var": "stair.direct_count"},
                }
            },
        },
    }


def facts(count=2, jurisdiction="KR-11"):
    return {
        "context": {"jurisdiction": jurisdiction},
        "building": {"use_group": "공동주택", "floor_count": 10},
        "stair": {"direct_count": count},
    }


def test_rule_passes():
    result = evaluate_rule(sample_rule(), facts(2))
    assert result.applicable is True
    assert result.outcome == DecisionOutcome.PASS


def test_rule_fails_and_renders_actual():
    result = evaluate_rule(sample_rule(), facts(1))
    assert result.outcome == DecisionOutcome.FAIL
    assert result.details["actual"] == 1
    assert result.details["required"] == 2


def test_rule_not_applicable_for_jurisdiction():
    result = evaluate_rule(sample_rule(), facts(2, jurisdiction="KR-26"))
    assert result.applicable is False
    assert result.outcome is None


def test_unknown_operator_fails_closed():
    rule = {"rule": {"if": {"magic": [1, 2]}, "then": {"PASS": True}, "else": {"FAIL": True}}}
    try:
        evaluate_rule(rule, facts())
    except RuleEvaluationError as exc:
        assert "Unsupported" in str(exc)
    else:
        raise AssertionError("unsupported operator must fail closed")


def test_missing_required_fact_routes_to_review():
    rule = {
        "applicability": {"jurisdiction": ["KR"]},
        "rule": {
            "if": {">=": [{"var": "stair.direct_count"}, 2]},
            "then": {"PASS": {"reason": "ok"}},
            "else": {"FAIL": {"reason": "too few"}},
        },
    }
    result = evaluate_rule(rule, {"context": {"jurisdiction": "KR"}})
    assert result.applicable is True
    assert result.outcome is DecisionOutcome.REVIEW
    assert result.reason == "Insufficient facts to evaluate rule"


def test_missing_applicability_fact_routes_to_review():
    payload = facts()
    del payload["context"]["jurisdiction"]
    result = evaluate_rule(sample_rule(), payload)
    assert result.applicable is True
    assert result.outcome is DecisionOutcome.REVIEW
    assert result.reason == "Insufficient facts to determine rule applicability"


def test_missing_exception_fact_routes_to_review():
    rule = {
        "exceptions": [
            {
                "condition": {"==": [{"var": "building.is_special_case"}, True]},
                "override": {"stair.direct_count": 2},
            }
        ],
        "rule": {
            "if": {">=": [{"var": "stair.direct_count"}, 2]},
            "then": {"PASS": True},
            "else": {"FAIL": True},
        },
    }
    result = evaluate_rule(rule, {"stair": {"direct_count": 1}})
    assert result.applicable is True
    assert result.outcome is DecisionOutcome.REVIEW
    assert result.reason == "Insufficient facts to evaluate rule exceptions"


@pytest.mark.parametrize(
    "condition",
    [
        {"and": [{"var": "stair.direct_count"}, {"var": "stair.missing"}]},
        {"all": [{"var": "stair.direct_count"}, {"var": "stair.missing"}]},
        {"or": [{"var": "stair.direct_count"}, {"var": "stair.missing"}]},
        {"any": [{"var": "stair.direct_count"}, {"var": "stair.missing"}]},
        {"not": {"var": "stair.missing"}},
    ],
    ids=["and", "all", "or", "any", "not"],
)
def test_logical_operators_fail_closed_on_a_missing_fact(condition):
    """A logical operator must not silently resolve an absent fact.

    The comparison operators already have coverage for this. The logical
    operators (`and`/`all`/`or`/`any`/`not`) were reachable with a missing
    operand and no test exercised them, so a regression that turned a missing
    fact into a plain ``False`` would have shipped as a silent FAIL.
    """
    rule = {
        "rule": {
            "if": condition,
            "then": {"PASS": {"reason": "satisfied"}},
            "else": {"FAIL": {"reason": "not satisfied"}},
        },
    }
    result = evaluate_rule(rule, {"stair": {"direct_count": 2}})

    assert result.applicable is True
    assert result.outcome is DecisionOutcome.REVIEW
    # The reason is what distinguishes "we could not evaluate" from a real
    # FAIL, so it is asserted rather than just the absence of a verdict.
    assert result.reason == "Insufficient facts to evaluate rule"
