from __future__ import annotations

from archontos.domain.enums import ReviewStatus


class AssertionReviewError(ValueError):
    pass


_ALLOWED_TRANSITIONS: dict[ReviewStatus, frozenset[ReviewStatus]] = {
    ReviewStatus.UNREVIEWED: frozenset(
        {
            ReviewStatus.APPROVED,
            ReviewStatus.REJECTED,
            ReviewStatus.CONTESTED,
        }
    ),
    ReviewStatus.CONTESTED: frozenset(
        {
            ReviewStatus.APPROVED,
            ReviewStatus.REJECTED,
        }
    ),
    ReviewStatus.APPROVED: frozenset({ReviewStatus.CONTESTED}),
    ReviewStatus.REJECTED: frozenset({ReviewStatus.CONTESTED}),
}


def validate_review_transition(
    previous: ReviewStatus,
    new: ReviewStatus,
) -> None:
    if new not in _ALLOWED_TRANSITIONS.get(previous, frozenset()):
        raise AssertionReviewError(
            f"invalid assertion review transition: {previous.value} -> {new.value}"
        )


def promotable_to_rule(status: ReviewStatus) -> bool:
    return status is ReviewStatus.APPROVED
