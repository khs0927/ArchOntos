from datetime import date

import pytest
from pydantic import ValidationError

from archontos.domain.contracts import (
    AecSubjectRef,
    AssertionContract,
    RuleVersionContract,
    SourceVersionContract,
)
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



def test_legal_assertion_can_reference_stable_ontology_subject():
    subject = AecSubjectRef(
        project_id="P-001",
        object_id="door-1",
        source_id="a" * 64,
        source_byte_revision_id="b" * 64,
        parser_revision_id="c" * 64,
        locator={"layout": "Model", "handle": "2F3"},
    )
    assertion = AssertionContract(
        natural_language="This opening requires the applicable fire-door rule.",
        interpreter_method="human",
        applies_to=[subject],
    )
    assert assertion.applies_to[0].project_id == "P-001"
    assert assertion.applies_to[0].object_id == "door-1"
    assert "document_id" not in assertion.applies_to[0].model_dump()


def test_ontology_revision_identity_must_be_complete():
    with pytest.raises(ValidationError, match="must be supplied together"):
        AecSubjectRef(
            project_id="P-001",
            object_id="door-1",
            source_id="a" * 64,
        )
