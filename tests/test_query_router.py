import pytest

from archontos.domain.enums import QueryIntent
from archontos.query.router import Mvp0QueryRouter


@pytest.mark.parametrize(
    ("query", "intent"),
    [
        ("이 규정이 어느 건축물에 적용돼?", QueryIntent.APPLICABILITY),
        ("서울과 부산의 기준 차이가 있어?", QueryIntent.JURISDICTION_COMPARISON),
        ("2025년 허가 당시와 현재 기준이 달라?", QueryIntent.TEMPORAL_COMPARISON),
        ("이 조항이 시행령인지 시행규칙인지 알려줘", QueryIntent.AUTHORITY_CLASSIFICATION),
        ("이 기준의 별표 원문을 보여줘", QueryIntent.SOURCE_EVIDENCE),
    ],
)
def test_five_mvp0_routes(query, intent):
    assert Mvp0QueryRouter().classify(query) == intent
