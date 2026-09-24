from datetime import date

import pytest

from archontos.ingestion.adapters import LawSearchItem
from archontos.ingestion.persistence import (
    CanonicalizationError,
    classify_document_type,
    law_source_key,
    law_version_label,
)


def item(**overrides):
    values = dict(
        law_name="건축법",
        law_id="001823",
        mst="999001",
        law_type="법률",
        ministry="국토교통부",
        promulgation_date=date(2026, 1, 1),
        promulgation_number="21000",
        enforcement_date=date(2026, 7, 1),
        revision_type="일부개정",
        history_code="현행",
        detail_link="/DRF/lawService.do?target=law&MST=999001",
    )
    values.update(overrides)
    return LawSearchItem(**values)


def test_law_source_and_version_identity_are_stable():
    law = item()
    assert law_source_key(law) == "lawgo:law:001823"
    assert law_version_label(law) == "mst:999001"


@pytest.mark.parametrize(
    ("law_type", "expected"),
    [
        ("법률", "statute"),
        ("대통령령", "regulation"),
        ("국토교통부령", "rule"),
        ("서울특별시 조례", "ordinance"),
    ],
)
def test_document_type_mapping(law_type, expected):
    assert classify_document_type(law_type) == expected


def test_missing_stable_law_id_is_rejected():
    with pytest.raises(CanonicalizationError):
        law_source_key(item(law_id=""))
