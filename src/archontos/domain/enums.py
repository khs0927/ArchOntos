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
    REVIEWED = "reviewed"
    CONTESTED = "contested"


class DecisionOutcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    REVIEW = "REVIEW"


class QueryIntent(StrEnum):
    APPLICABILITY = "applicability"
    JURISDICTION_COMPARISON = "jurisdiction_comparison"
    TEMPORAL_COMPARISON = "temporal_comparison"
    AUTHORITY_CLASSIFICATION = "authority_classification"
    SOURCE_EVIDENCE = "source_evidence"
    GENERAL_SEARCH = "general_search"
