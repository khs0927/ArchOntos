import pytest

from archontos.rules.compiler import (
    RuleCompilationError,
    authority_from_document_type,
    compile_requirement,
)
from archontos.rules.engine import evaluate_rule


def test_safe_requirement_compiles_and_evaluates():
    compiled = compile_requirement(
        natural_language="직통계단을 2개소 이상 설치하여야 한다.",
        structured_payload={
            "fact_path": "stair.direct_count",
            "operator": ">=",
            "value": 2,
            "unit": "count",
            "title": "직통계단 수",
            "applicability": {"building_use_groups": ["공동주택"]},
        },
        jurisdiction_code="KR",
    )

    applicability = compiled.logic_expr["applicability"]
    assert applicability["jurisdiction"] == ["KR"]

    passed = evaluate_rule(
        compiled.logic_expr,
        {
            "context": {"jurisdiction": "KR"},
            "building": {"use_group": "공동주택"},
            "stair": {"direct_count": 2},
        },
    )
    failed = evaluate_rule(
        compiled.logic_expr,
        {
            "context": {"jurisdiction": "KR"},
            "building": {"use_group": "공동주택"},
            "stair": {"direct_count": 1},
        },
    )
    assert passed.outcome.value == "PASS"
    assert failed.outcome.value == "FAIL"
    assert failed.details["required"]["value"] == 2
    assert failed.details["actual"] == 1


def test_rule_value_is_literal_not_nested_dsl():
    compiled = compile_requirement(
        natural_language="특정 값을 요구한다.",
        structured_payload={
            "fact_path": "building.code",
            "operator": "==",
            "value": {"var": "attacker.controlled"},
        },
        jurisdiction_code="KR",
    )
    result = evaluate_rule(
        compiled.logic_expr,
        {
            "context": {"jurisdiction": "KR"},
            "building": {"code": {"var": "attacker.controlled"}},
            "attacker": {"controlled": "different"},
        },
    )
    assert result.outcome.value == "PASS"


def test_conflicting_jurisdiction_fails_closed():
    with pytest.raises(RuleCompilationError):
        compile_requirement(
            natural_language="서울 전용 규정",
            structured_payload={
                "fact_path": "building.height",
                "operator": ">",
                "value": 10,
                "applicability": {"jurisdiction": ["KR-11"]},
            },
            jurisdiction_code="KR-26",
        )


@pytest.mark.parametrize(
    ("document_type", "authority"),
    [
        ("statute", "statutory"),
        ("regulation", "regulatory"),
        ("rule", "regulatory"),
        ("ordinance", "ordinance"),
        ("standard", "standard"),
        ("guide", "guideline"),
    ],
)
def test_document_type_maps_to_authority(document_type, authority):
    assert authority_from_document_type(document_type) == authority
