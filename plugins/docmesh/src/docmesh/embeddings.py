"""Embedding backends with an offline deterministic test double."""

from __future__ import annotations

import hashlib
import math
import re
import struct
from collections.abc import Iterable, Sequence
from typing import Any

from .chunking import ApproximateTokenizer
from .models import ModelNotInstalledError


class EmbeddingBackend:
    model = ""
    tokenizer_name = ""
    passage_method = "passage_embed"
    query_method = "query_embed"
    _dimensions: int | None = 0

    @property
    def dimensions(self) -> int:
        return 0 if self._dimensions is None else self._dimensions

    @dimensions.setter
    def dimensions(self, value: int) -> None:
        self._dimensions = int(value)

    def embed_passages(self, passages: Sequence[str]) -> list[list[float]]:
        raise NotImplementedError

    def embed_queries(self, queries: Sequence[str]) -> list[list[float]]:
        raise NotImplementedError

    # FastEmbed's API names are part of the contract and are useful to callers
    # that do not want to know which backend implementation is active.
    def passage_embed(self, passages: Sequence[str]) -> list[list[float]]:
        return self.embed_passages(passages)

    def query_embed(self, queries: Sequence[str]) -> list[list[float]]:
        return self.embed_queries(queries)

    def count_tokens(self, text: str) -> int:
        return ApproximateTokenizer().count(text)


class DeterministicEmbedder(EmbeddingBackend):
    """A stable local bag-of-subtokens vector for tests and offline operation.

    It is intentionally not presented as the production model.  Its role is
    to make all indexing/state tests deterministic while preserving the exact
    passage/query method split of FastEmbed.
    """

    model = "test/deterministic"
    tokenizer_name = "unicode-word-punctuation-v1"
    passage_method = "passage_embed"
    query_method = "query_embed"

    def __init__(
        self,
        dimensions: int = 384,
        *,
        model: str = "test/deterministic",
        tokenizer_name: str = "unicode-word-punctuation-v1",
    ) -> None:
        if dimensions <= 0:
            raise ValueError("embedding dimensions must be positive")
        self.dimensions = dimensions
        self.model = model
        self.tokenizer_name = tokenizer_name
        self._tokenizer = ApproximateTokenizer()

    def _embed(self, text: str, *, query: bool) -> list[float]:
        values = [0.0] * self.dimensions
        tokens = re.findall(r"[\w]+", text.lower(), re.UNICODE)
        if query:
            tokens = ["query:" + token for token in tokens]
        if not tokens:
            return values
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:8], "big") % self.dimensions
            sign = -1.0 if digest[8] & 1 else 1.0
            values[index] += sign
        norm = math.sqrt(sum(value * value for value in values))
        if norm:
            values = [value / norm for value in values]
        return values

    def embed_passages(self, passages: Sequence[str]) -> list[list[float]]:
        return [self._embed(value, query=False) for value in passages]

    def embed_queries(self, queries: Sequence[str]) -> list[list[float]]:
        # Query vectors share the same vocabulary space.  The query marker is
        # hashed as a prefix only to avoid accidental equality with passages;
        # cosine retrieval remains fully deterministic.
        return [self._embed(value, query=False) for value in queries]

    def count_tokens(self, text: str) -> int:
        return self._tokenizer.count(text)


class FastEmbedBackend(EmbeddingBackend):
    """Thin adapter around FastEmbed's ``passage_embed``/``query_embed``."""

    model = "BAAI/bge-small-en-v1.5"
    tokenizer_name = "fastembed"
    passage_method = "passage_embed"
    query_method = "query_embed"

    def __init__(
        self,
        model: str = "BAAI/bge-small-en-v1.5",
        *,
        cache_dir: str | None = None,
        local_files_only: bool = True,
        text_embedding: object | None = None,
    ) -> None:
        if text_embedding is None:
            try:
                from fastembed import TextEmbedding  # type: ignore
            except ImportError as exc:
                raise ModelNotInstalledError(
                    "FastEmbed is not installed; run the explicit DocMesh setup step"
                ) from exc
            kwargs: dict[str, Any] = {
                "model_name": model,
                "local_files_only": bool(local_files_only),
            }
            if cache_dir:
                kwargs["cache_dir"] = cache_dir
            try:
                text_embedding = TextEmbedding(**kwargs)
            except Exception as exc:
                raise ModelNotInstalledError(
                    f"FastEmbed model {model} is not installed or could not be loaded: {exc}"
                ) from exc
        self._model = text_embedding
        self.model = model
        self.cache_dir = cache_dir
        self.local_files_only = bool(local_files_only)
        self._dimensions: int | None = None

    @property
    def dimensions(self) -> int:
        if self._dimensions is None:
            probe = self.embed_passages(["docmesh dimension probe"])
            self._dimensions = len(probe[0]) if probe else 0
        return self._dimensions

    @dimensions.setter
    def dimensions(self, value: int) -> None:
        self._dimensions = int(value)

    def _convert(self, values: Iterable[object]) -> list[list[float]]:
        result: list[list[float]] = []
        for value in values:
            tolist = getattr(value, "tolist", None)
            converted = tolist() if callable(tolist) else value
            if isinstance(converted, (str, bytes, bytearray)) or not isinstance(
                converted, Iterable
            ):
                raise TypeError("embedding output must be an iterable of numbers")
            result.append([float(item) for item in converted])
        if result and self._dimensions is None:
            self._dimensions = len(result[0])
        return result

    def embed_passages(self, passages: Sequence[str]) -> list[list[float]]:
        method = getattr(self._model, "passage_embed", None)
        if not callable(method):
            raise ModelNotInstalledError(
                "FastEmbed backend has no passage_embed method"
            )
        return self._convert(method(list(passages)))

    def embed_queries(self, queries: Sequence[str]) -> list[list[float]]:
        method = getattr(self._model, "query_embed", None)
        if not callable(method):
            raise ModelNotInstalledError("FastEmbed backend has no query_embed method")
        return self._convert(method(list(queries)))

    def count_tokens(self, text: str) -> int:
        method = getattr(self._model, "token_count", None)
        if not callable(method):
            # Older FastEmbed releases did not expose token_count.  A UTF-8
            # byte count is intentionally conservative for BPE tokenizers: it
            # may over-split, but it can never silently accept an input based
            # on an optimistic word-count estimate.
            return len(text.encode("utf-8"))
        return int(method(text))


def vector_to_blob(vector: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *[float(value) for value in vector])


def blob_to_vector(blob: bytes, dimensions: int | None = None) -> list[float]:
    if dimensions is None:
        dimensions = len(blob) // 4
    if dimensions <= 0 or len(blob) < dimensions * 4:
        return []
    return list(struct.unpack(f"<{dimensions}f", blob[: dimensions * 4]))


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    size = min(len(left), len(right))
    if not size:
        return 0.0
    dot = sum(float(left[index]) * float(right[index]) for index in range(size))
    left_norm = math.sqrt(sum(float(left[index]) ** 2 for index in range(size)))
    right_norm = math.sqrt(sum(float(right[index]) ** 2 for index in range(size)))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def embed_passages(backend: object, passages: Sequence[str]) -> list[list[float]]:
    """Call either the backend convenience method or FastEmbed's name."""

    method = getattr(backend, "embed_passages", None) or getattr(
        backend, "passage_embed", None
    )
    if not callable(method):
        raise ModelNotInstalledError("embedding backend has no passage_embed method")
    values = method(passages)
    return [
        list(value.tolist() if hasattr(value, "tolist") else value) for value in values
    ]


def embed_queries(backend: object, queries: Sequence[str]) -> list[list[float]]:
    """Call either the backend convenience method or FastEmbed's name."""

    method = getattr(backend, "embed_queries", None) or getattr(
        backend, "query_embed", None
    )
    if not callable(method):
        raise ModelNotInstalledError("embedding backend has no query_embed method")
    values = method(queries)
    return [
        list(value.tolist() if hasattr(value, "tolist") else value) for value in values
    ]


HashEmbedder = DeterministicEmbedder
