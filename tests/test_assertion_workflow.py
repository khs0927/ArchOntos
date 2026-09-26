from uuid import UUID

import pytest

from archontos.assertions.identity import assertion_key, normalize_assertion_text
from archontos.assertions.review import (
    AssertionReviewError,
    promotable_to_rule,
    validate_review_transition,
)
from archontos.domain.enums import ReviewStatus

EVIDENCE_ID = UUID("11111111-1111-1111-1111-111111111111")


def test_assertion_identity_is_deterministic_and_whitespace_stable():
    first = assertion_key(
        evidence_span_id=EVIDENCE_ID,
        natural_language="직통계단을   2개소 이상 설치한다.",
        structured_payload={"operator": ">=", "value": 2},
    )
    second = assertion_key(
        evidence_span_id=EVIDENCE_ID,
        natural_language="직통계단을 2개소 이상 설치한다.",
        structured_payload={"value": 2, "operator": ">="},
    )
    assert first == second
    assert len(first) == 64
    assert normalize_assertion_text(" a   b\n c ") == "a b c"


@pytest.mark.parametrize(
    ("previous", "new"),
    [
        (ReviewStatus.UNREVIEWED, ReviewStatus.APPROVED),
        (ReviewStatus.UNREVIEWED, ReviewStatus.REJECTED),
        (ReviewStatus.UNREVIEWED, ReviewStatus.CONTESTED),
        (ReviewStatus.CONTESTED, ReviewStatus.APPROVED),
        (ReviewStatus.CONTESTED, ReviewStatus.REJECTED),
        (ReviewStatus.APPROVED, ReviewStatus.CONTESTED),
        (ReviewStatus.REJECTED, ReviewStatus.CONTESTED),
    ],
)
def test_allowed_review_transitions(previous, new):
    validate_review_transition(previous, new)


@pytest.mark.parametrize(
    ("previous", "new"),
    [
        (ReviewStatus.UNREVIEWED, ReviewStatus.UNREVIEWED),
        (ReviewStatus.APPROVED, ReviewStatus.REJECTED),
        (ReviewStatus.REJECTED, ReviewStatus.APPROVED),
        (ReviewStatus.CONTESTED, ReviewStatus.CONTESTED),
    ],
)
def test_invalid_review_transitions_fail_closed(previous, new):
    with pytest.raises(AssertionReviewError):
        validate_review_transition(previous, new)


def test_only_approved_assertion_is_promotable_to_rule():
    assert promotable_to_rule(ReviewStatus.APPROVED) is True
    assert promotable_to_rule(ReviewStatus.UNREVIEWED) is False
    assert promotable_to_rule(ReviewStatus.REJECTED) is False
    assert promotable_to_rule(ReviewStatus.CONTESTED) is False
