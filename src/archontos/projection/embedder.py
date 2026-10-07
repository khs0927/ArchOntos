"""Pluggable text embedders for ``embedding_projection``.

``embedding_projection.embedding`` is ``vector(1536)``. Every embedder returns
L2-normalised vectors; shorter model outputs are zero-padded to the column width,
which leaves cosine similarity and inner product between vectors of the *same*
model unchanged. ``embedding_model`` is stored next to the vector so vectors from
different models are never compared by accident.

Backends (``ARCHONTOS_EMBEDDER``):

* ``none`` (default): no vector; ``embedding`` stays NULL, as before.
* ``hashing``: deterministic signed feature hashing over character n-grams.
  Dependency-free and stable across processes; used in tests and as a cheap
  lexical fallback. It is not a semantic model.
* ``fastembed``: ONNX sentence embeddings via `fastembed` (Apache-2.0), installed
  with ``pip install 'archontos[embeddings]'``. The default model is multilingual
  because the canonical sources are Korean law.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

COLUMN_DIM = 1536
DEFAULT_FASTEMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


class EmbedderUnavailable(RuntimeError):
    """The configured embedder cannot be constructed (missing extra, bad model)."""


@runtime_checkable
class Embedder(Protocol):
    @property
    def model_id(self) -> str: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def _normalise(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return [0.0] * len(vector)
    return [value / norm for value in vector]


def fit_dimension(vector: Sequence[float], dim: int = COLUMN_DIM) -> list[float]:
    if len(vector) > dim:
        raise ValueError(f"embedding has {len(vector)} dims; column holds {dim}")
    return list(vector) + [0.0] * (dim - len(vector))


def to_pgvector(vector: Sequence[float]) -> str:
    """Text literal accepted by ``CAST(:v AS vector)``."""
    return "[" + ",".join(f"{value:.7g}" for value in vector) + "]"


_TOKEN = re.compile(r"\w+", re.UNICODE)


class HashingEmbedder:
    """Deterministic signed feature hashing over word tokens and character trigrams."""

    def __init__(self, dim: int = 256) -> None:
        if not 1 <= dim <= COLUMN_DIM:
            raise ValueError(f"dim must be in [1, {COLUMN_DIM}]")
        self.dim = dim

    @property
    def model_id(self) -> str:
        return f"hashing-v1/{self.dim}"

    def _features(self, text: str) -> list[str]:
        lowered = text.lower()
        tokens = _TOKEN.findall(lowered)
        grams = [lowered[i : i + 3] for i in range(max(len(lowered) - 2, 0))]
        return [f"w:{token}" for token in tokens] + [f"c:{gram}" for gram in grams]

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dim
            for feature in self._features(text):
                digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
                bucket = int.from_bytes(digest[:4], "little") % self.dim
                vector[bucket] += 1.0 if digest[4] & 1 else -1.0
            out.append(_normalise(vector))
        return out


class FastEmbedEmbedder:
    """Sentence embeddings via fastembed (ONNX Runtime, no torch)."""

    def __init__(self, model_name: str = DEFAULT_FASTEMBED_MODEL, cache_dir: str | None = None):
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise EmbedderUnavailable(
                "ARCHONTOS_EMBEDDER=fastembed needs `pip install 'archontos[embeddings]'`"
            ) from exc
        try:
            self._model = TextEmbedding(model_name=model_name, cache_dir=cache_dir)
        except ValueError as exc:
            raise EmbedderUnavailable(str(exc)) from exc
        self.model_name = model_name

    @property
    def model_id(self) -> str:
        return f"fastembed/{self.model_name}"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [_normalise([float(x) for x in vector]) for vector in self._model.embed(list(texts))]


def build_embedder(
    kind: str, model_name: str | None = None, cache_dir: str | None = None
) -> Embedder | None:
    if kind == "none":
        return None
    if kind == "hashing":
        return HashingEmbedder()
    if kind == "fastembed":
        return FastEmbedEmbedder(model_name or DEFAULT_FASTEMBED_MODEL, cache_dir=cache_dir)
    raise EmbedderUnavailable(f"unknown embedder {kind!r}")
