import re

from archontos.domain.enums import QueryIntent


class Mvp0QueryRouter:
    """Deterministic first-stage router for the five MVP-0 regulatory question classes."""

    _patterns: list[tuple[QueryIntent, tuple[str, ...]]] = [
        (QueryIntent.SOURCE_EVIDENCE, ("별표", "원문", "근거", "출처", "조문")),
        (
            QueryIntent.AUTHORITY_CLASSIFICATION,
            ("시행령", "시행규칙", "법률인지", "부령", "authority", "법령 종류"),
        ),
        (
            QueryIntent.TEMPORAL_COMPARISON,
            ("당시", "현재", "개정 전", "개정후", "개정 후", "년도", "년 허가", "시점"),
        ),
        (
            QueryIntent.JURISDICTION_COMPARISON,
            ("서울", "부산", "지자체", "관할", "지역별", "차이가", "조례"),
        ),
        (
            QueryIntent.APPLICABILITY,
            ("적용", "어느 건축물", "대상", "해당", "조건", "적용돼"),
        ),
    ]

    def classify(self, query: str) -> QueryIntent:
        normalized = re.sub(r"\s+", " ", query.strip().lower())
        for intent, needles in self._patterns:
            if any(needle.lower() in normalized for needle in needles):
                return intent
        return QueryIntent.GENERAL_SEARCH
