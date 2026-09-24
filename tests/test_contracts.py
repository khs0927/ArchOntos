from datetime import date

import pytest
from pydantic import ValidationError

from archontos.domain.contracts import RuleVersionContract, SourceVersionContract
from archontos.domain.enums import AuthorityClass


def test_source_version_rejects_inverted_interval():
    with pytest.raises(ValidationError):
        SourceVersionContract(
            version_label="v1",
            effective_from=date(2026, 1, 2),
            effective_to=date(2026, 1, 1),
        )


def test_authority_is_categorical_not_confidence():
    contract = RuleVersionContract(
        version_label="2026-01",
        logic_expr={"rule": {"if": {"==": [1, 1]}, "then": {"PASS": True}, "else": {"FAIL": True}}},
        valid_from=date(2026, 1, 1),
        authority_class=AuthorityClass.STATUTORY,
        binding=True,
    )
    assert contract.authority_class is AuthorityClass.STATUTORY
    assert not hasattr(contract, "authority_confidence")
