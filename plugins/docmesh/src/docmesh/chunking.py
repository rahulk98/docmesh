"""Token-budgeted, source-preserving section chunking."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

from .models import Chunk, Manifest, Section

_TOKEN_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)
_TOKEN_COUNT_DISPATCH_VERSION = "count_tokens-first-v1"
_DEFAULT_CHUNKING_VERSION = "v1-recursive-lines-paragraphs-token-count-probe"


class TokenBudgetError(ValueError):
    pass


class ApproximateTokenizer:
    """Deterministic fallback tokenizer used before FastEmbed is installed."""

    name = "unicode-word-punctuation-v1"

    def encode(self, text: str) -> list[str]:
        return _TOKEN_RE.findall(text)

    def count(self, text: str) -> int:
        return len(self.encode(text))


def token_count(text: str, tokenizer: object | None = None) -> int:
    if tokenizer is None:
        return ApproximateTokenizer().count(text)
    count_tokens = getattr(tokenizer, "count_tokens", None)
    if callable(count_tokens):
        return int(count_tokens(text))
    count = getattr(tokenizer, "count", None)
    if callable(count):
        return int(count(text))
    encode = getattr(tokenizer, "encode", None)
    if callable(encode):
        return len(encode(text))
    if callable(tokenizer):
        return int(tokenizer(text))
    return ApproximateTokenizer().count(text)


def compute_embedding_strategy_id(
    *,
    model: str,
    dimensions: int | None,
    tokenizer: str,
    passage_method: str = "passage_embed",
    query_method: str = "query_embed",
    max_tokens: int = 400,
    hard_max_tokens: int = 480,
    chunking_version: str = _DEFAULT_CHUNKING_VERSION,
    breadcrumb_format: str = " > ",
    retrieval_prefix: str = "",
) -> str:
    """Stable content address for every parameter affecting vectors."""

    payload = {
        "model": model,
        "dimensions": dimensions,
        "tokenizer": tokenizer,
        "passage_method": passage_method,
        "query_method": query_method,
        "max_tokens": max_tokens,
        "hard_max_tokens": hard_max_tokens,
        "chunking_version": chunking_version,
        # This dispatch order is part of the vector strategy.  Without it,
        # indexes built while FastEmbed was silently approximated would look
        # compatible after upgrading the tokenizer wiring.
        "token_count_dispatch": _TOKEN_COUNT_DISPATCH_VERSION,
        "breadcrumb_format": breadcrumb_format,
        "retrieval_prefix": retrieval_prefix,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "v1-" + hashlib.sha256(encoded).hexdigest()


@dataclass
class _Piece:
    text: str
    start_offset_line: int
    end_offset_line: int


class Chunker:
    """Recursively split sections until their final embedding input fits."""

    def __init__(
        self,
        max_tokens: int = 400,
        hard_max_tokens: int = 480,
        tokenizer: object | None = None,
        breadcrumb_format: str = " > ",
        strategy_id: str = "",
        retrieval_prefix: str = "",
    ) -> None:
        if max_tokens <= 0 or hard_max_tokens < max_tokens:
            raise ValueError("invalid chunk token limits")
        self.max_tokens = max_tokens
        self.hard_max_tokens = hard_max_tokens
        self.tokenizer = tokenizer or ApproximateTokenizer()
        self.breadcrumb_format = breadcrumb_format
        self.strategy_id = strategy_id
        self.retrieval_prefix = retrieval_prefix

    def embedding_input(self, breadcrumb: str, passage: str) -> str:
        # FastEmbed applies its own prefix inside passage_embed.  A caller may
        # provide an explicit library prefix so it participates in the budget;
        # no hand-written ``passage:`` marker is added by default.
        return self.retrieval_prefix + breadcrumb + "\n\n" + passage

    def _fits(self, breadcrumb: str, passage: str, limit: int) -> bool:
        return (
            token_count(self.embedding_input(breadcrumb, passage), self.tokenizer)
            <= limit
        )

    def _split_words(self, line: str, breadcrumb: str) -> list[str]:
        words = re.findall(r"\S+\s*", line)
        if not words:
            return [line]
        pieces: list[str] = []
        current = ""
        for word in words:
            candidate = current + word
            if current and not self._fits(breadcrumb, candidate, self.max_tokens):
                pieces.append(current)
                current = word
            else:
                current = candidate
        if current or not pieces:
            pieces.append(current)
        # A single unbreakable token can exceed the model limit.  Splitting it
        # by Unicode codepoint is lossless and explicit rather than truncating.
        output: list[str] = []
        for piece in pieces:
            if self._fits(breadcrumb, piece, self.hard_max_tokens):
                output.append(piece)
                continue
            chars = list(piece)
            current_chars = ""
            for char in chars:
                candidate = current_chars + char
                if current_chars and not self._fits(
                    breadcrumb, candidate, self.max_tokens
                ):
                    output.append(current_chars)
                    current_chars = char
                else:
                    current_chars = candidate
            if current_chars:
                output.append(current_chars)
        if any(
            not self._fits(breadcrumb, piece, self.hard_max_tokens) for piece in output
        ):
            raise TokenBudgetError(
                "breadcrumb alone exceeds the hard embedding token limit"
            )
        return output

    def _pieces(self, section: Section) -> list[_Piece]:
        breadcrumb = self.breadcrumb_format.join(section.breadcrumb)
        if not self._fits(breadcrumb, "", self.hard_max_tokens):
            raise TokenBudgetError(
                "section breadcrumb exceeds the hard embedding token limit"
            )
        lines = section.text.splitlines()
        if not lines:
            return [_Piece("", 0, 0)]
        pieces: list[_Piece] = []
        current_lines: list[str] = []
        current_start = 0
        for line_index, line in enumerate(lines):
            if self._fits(breadcrumb, line, self.max_tokens):
                candidate_lines = current_lines + [line]
                candidate = "\n".join(candidate_lines)
                if current_lines and not self._fits(
                    breadcrumb, candidate, self.max_tokens
                ):
                    pieces.append(
                        _Piece("\n".join(current_lines), current_start, line_index - 1)
                    )
                    current_lines = [line]
                    current_start = line_index
                else:
                    if not current_lines:
                        current_start = line_index
                    current_lines = candidate_lines
                continue
            if current_lines:
                pieces.append(
                    _Piece("\n".join(current_lines), current_start, line_index - 1)
                )
                current_lines = []
            words = self._split_words(line, breadcrumb)
            for word_index, word in enumerate(words):
                pieces.append(_Piece(word, line_index, line_index))
        if current_lines:
            pieces.append(
                _Piece("\n".join(current_lines), current_start, len(lines) - 1)
            )
        return pieces

    def chunk(
        self,
        document_path: str,
        section: Section,
        ordinal_start: int = 0,
        *,
        text_hash: str = "",
    ) -> list[Chunk]:
        result: list[Chunk] = []
        breadcrumb = self.breadcrumb_format.join(section.breadcrumb)
        for offset, piece in enumerate(self._pieces(section)):
            start_line = section.start_line + piece.start_offset_line
            end_line = section.start_line + piece.end_offset_line
            embedding = self.embedding_input(breadcrumb, piece.text)
            count = token_count(embedding, self.tokenizer)
            if count > self.hard_max_tokens:
                raise TokenBudgetError(
                    "chunking produced an embedding input above the hard token limit"
                )
            result.append(
                Chunk(
                    document_path=document_path,
                    ordinal=ordinal_start + offset,
                    breadcrumb=breadcrumb,
                    text=piece.text,
                    start_line=start_line,
                    end_line=end_line,
                    format=section.format,
                    page=section.page,
                    token_count=count,
                    embedding_input=embedding,
                    text_hash=text_hash,
                    embedding_strategy_id=self.strategy_id,
                )
            )
        return result

    def chunk_document(
        self, document_path: str, sections: Sequence[Section], *, text_hash: str = ""
    ) -> list[Chunk]:
        chunks: list[Chunk] = []
        for section in sections:
            chunks.extend(
                self.chunk(document_path, section, len(chunks), text_hash=text_hash)
            )
        return chunks


def build_chunker(
    manifest: Manifest, *, tokenizer: object | None = None, strategy_id: str = ""
) -> Chunker:
    strategy_id = strategy_id or compute_embedding_strategy_id(
        model=manifest.model,
        dimensions=manifest.dimensions,
        tokenizer=manifest.tokenizer,
        max_tokens=manifest.max_embedding_tokens,
        hard_max_tokens=manifest.hard_embedding_tokens,
        chunking_version=manifest.chunking_version,
        breadcrumb_format=manifest.breadcrumb_format,
        retrieval_prefix=manifest.retrieval_prefix,
    )
    return Chunker(
        manifest.max_embedding_tokens,
        manifest.hard_embedding_tokens,
        tokenizer,
        manifest.breadcrumb_format,
        strategy_id,
        manifest.retrieval_prefix,
    )
