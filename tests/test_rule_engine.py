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
