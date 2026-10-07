import math
import os

import pytest

from archontos.projection.embedder import (
    COLUMN_DIM,
    EmbedderUnavailable,
    HashingEmbedder,
    build_embedder,
    fit_dimension,
    to_pgvector,
)


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_hashing_is_deterministic_and_normalised():
    embedder = HashingEmbedder()
    first, second = embedder.embed(["건축법 제61조 일조 등의 확보", "건축법 제61조 일조 등의 확보"])
    assert first == second
    assert math.isclose(sum(v * v for v in first), 1.0, rel_tol=1e-9)
    assert embedder.model_id == "hashing-v1/256"


def test_hashing_similarity_orders_related_text_higher():
    embedder = HashingEmbedder()
    base, near, far = embedder.embed(
        ["건축물의 높이 제한 규정", "건축물 높이 제한", "주차장 설치 기준 완화"]
    )
    assert _cos(base, near) > _cos(base, far)


def test_empty_text_gives_zero_vector():
    assert HashingEmbedder(dim=8).embed([""]) == [[0.0] * 8]


def test_fit_dimension_pads_and_rejects_oversize():
    padded = fit_dimension([1.0, 0.0])
    assert len(padded) == COLUMN_DIM
    assert padded[:2] == [1.0, 0.0]
    with pytest.raises(ValueError):
        fit_dimension([0.0] * (COLUMN_DIM + 1))


def test_pgvector_literal():
    assert to_pgvector([0.5, -1.0]) == "[0.5,-1]"


def test_build_embedder():
    assert build_embedder("none") is None
    assert isinstance(build_embedder("hashing"), HashingEmbedder)
    with pytest.raises(EmbedderUnavailable):
        build_embedder("word2vec")


@pytest.mark.skipif(
    not os.getenv("ARCHONTOS_TEST_FASTEMBED"),
    reason="set ARCHONTOS_TEST_FASTEMBED=1 to download and run the fastembed model",
)
def test_fastembed_multilingual_similarity():
    embedder = build_embedder("fastembed", cache_dir=os.getenv("ARCHONTOS_EMBEDDING_CACHE_DIR"))
    assert embedder is not None
    base, near, far = embedder.embed(
        ["건축물의 높이 제한", "building height limit", "주차장 설치 기준"]
    )
    assert _cos(base, near) > _cos(base, far)
