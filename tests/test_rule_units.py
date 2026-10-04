"""Synthetic numerical rules validate units; these fixtures are not statutory requirements."""

import pytest

from archontos.rules.compiler import RuleCompilationError, compile_requirement
from archontos.rules.engine import evaluate_rule


def rule(unit="m", value=2):
    return compile_requirement(
        natural_language="synthetic",
        jurisdiction_code="KR",
        structured_payload={
            "fact_path": "building.height",
            "operator": ">=",
            "value": value,
            "unit": unit,
        },
    ).logic_expr


@pytest.mark.parametrize(
    "units", [None, {}, {"building.height": "mm"}, {"building.height": "unknown"}]
)
def test_missing_mismatched_or_unknown_fact_unit_is_review(units):
    result = evaluate_rule(
        rule(),
        {"context": {"jurisdiction": "KR"}, "building": {"height": 2000}, "fact_units": units},
    )
    assert result.outcome.value == "REVIEW" and result.details["unit_problems"]


@pytest.mark.parametrize("value", [None, True, "3", float("nan"), float("inf")])
def test_unit_bearing_fact_must_be_finite_numeric(value):
    result = evaluate_rule(
        rule(),
        {
            "context": {"jurisdiction": "KR"},
            "building": {"height": value},
            "fact_units": {"building.height": "m"},
        },
    )
    assert result.outcome.value == "REVIEW"


def test_matching_units_allow_definitive_branch_without_conversion():
    for value, expected in ((2, "PASS"), (1.5, "FAIL")):
        result = evaluate_rule(
            rule(),
            {
                "context": {"jurisdiction": "KR"},
                "building": {"height": value},
                "fact_units": {"building.height": "m"},
            },
        )
        assert result.outcome.value == expected


def test_existing_compiler_v1_units_cannot_bypass_validation():
    legacy = rule()
    legacy.pop("fact_units")
    facts = {"context": {"jurisdiction": "KR"}, "building": {"height": 2000}}
    assert evaluate_rule(legacy, facts).outcome.value == "REVIEW"
    facts["fact_units"] = {"building.height": "m"}
    assert evaluate_rule(legacy, facts).outcome.value == "PASS"


def test_scope_conditions_cannot_skip_unit_verification():
    scoped = rule()
    scoped["applicability"]["conditions"] = [{"<": [{"var": "building.height"}, 10]}]
    facts = {
        "context": {"jurisdiction": "KR"},
        "building": {"height": 2000},
        "fact_units": {"building.height": "mm"},
    }
    assert evaluate_rule(scoped, facts).outcome.value == "REVIEW"


@pytest.mark.parametrize(
    "unit,value",
    [("unknown", 2), ("", 2), ("m", True), ("m", float("nan")), ("count", 1.5), ("count", -1)],
)
def test_invalid_requirement_units_and_count_limits_cannot_compile(unit, value):
    with pytest.raises(RuleCompilationError):
        rule(unit, value)


@pytest.mark.parametrize("value", [1.5, -1, True])
def test_count_facts_need_nonnegative_integral_values(value):
    result = evaluate_rule(
        rule("count"),
        {
            "context": {"jurisdiction": "KR"},
            "building": {"height": value},
            "fact_units": {"building.height": "count"},
        },
    )
    assert result.outcome.value == "REVIEW"
