from enum import StrEnum


class AuthorityClass(StrEnum):
    STATUTORY = "statutory"
    REGULATORY = "regulatory"
    ADMINISTRATIVE = "administrative"
    ORDINANCE = "ordinance"
    GUIDELINE = "guideline"
    STANDARD = "standard"


class ReviewStatus(StrEnum):
    UNREVIEWED = "unreviewed"
    APPROVED = "approved"
    REJECTED = "rejected"
    CONTESTED = "contested"


class DecisionOutcome(StrEnum):
    # S105: this is a decision outcome, not a credential. The rule matches the
    # word PASS inside a string.
    PASS = "PASS"  # noqa: S105
    FAIL = "FAIL"
    REVIEW = "REVIEW"


class QueryIntent(StrEnum):
    APPLICABILITY = "applicability"
    JURISDICTION_COMPARISON = "jurisdiction_comparison"
    TEMPORAL_COMPARISON = "temporal_comparison"
    AUTHORITY_CLASSIFICATION = "authority_classification"
    SOURCE_EVIDENCE = "source_evidence"
    GENERAL_SEARCH = "general_search"
