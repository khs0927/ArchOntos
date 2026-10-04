"""Changing canonical AEC subject scope must create a fresh unreviewed candidate identity."""

from uuid import UUID

import pytest
from pydantic import ValidationError

from archontos.assertions.contracts import AssertionCandidateCreate
from archontos.assertions.identity import assertion_key

EVIDENCE = UUID("00000000-0000-0000-0000-000000000001")


def key(refs=None):
    return assertion_key(
        evidence_span_id=EVIDENCE,
        natural_language="synthetic",
        structured_payload={},
        applies_to=refs,
    )


def test_subject_scope_and_revision_are_part_of_candidate_identity():
    first = {
        "project_id": "P",
        "source_id": "a" * 64,
        "source_byte_revision_id": "a" * 64,
        "parser_revision_id": "b" * 64,
    }
    assert key([first]) != key([dict(first, project_id="OTHER")])
    assert key([first]) != key([dict(first, parser_revision_id="c" * 64)])
    assert key([first, {"project_id": "OTHER"}]) == key([{"project_id": "OTHER"}, first, first])
    assert key() == key([])


def test_candidate_contract_validates_subject_revisions_and_bound():
    params = dict(
        evidence_span_id=EVIDENCE, natural_language="synthetic", interpreter_method="human"
    )
    candidate = AssertionCandidateCreate(**params, applies_to=[{"project_id": "P"}])
    assert candidate.applies_to[0].project_id == "P"
    with pytest.raises(ValidationError, match="supplied together"):
        AssertionCandidateCreate(**params, applies_to=[{"project_id": "P", "source_id": "a" * 64}])
    with pytest.raises(ValidationError):
        AssertionCandidateCreate(**params, applies_to=[{"project_id": "P"}] * 10001)
