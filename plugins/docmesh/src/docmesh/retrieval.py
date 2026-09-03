"""Precision search, exhaustive find, source reads, and location validation."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TypedDict

from .config import canonical_path, infer_role, source_format
from .embeddings import embed_queries
from .index import Indexer, SQLiteIndex
from .models import (
    FindResult,
    ModelNotInstalledError,
    ReadResult,
    SearchMetrics,
    SearchResult,
    SourceLocation,
    StaleSourceError,
    UnsupportedDocumentError,
    ValidationError,
)
from .parsing import parse_file, span_text

RRF_K = 60
DEFAULT_RESULT_LIMIT = 8
MAX_CHANNEL_CANDIDATES = 200

RowLike = sqlite3.Row | Mapping[str, Any]


class _RankedChunk(TypedDict):
    row: sqlite3.Row
    lexical_rank: int | None
    lexical_score: float | None
    vector_score: float | None
    vector_rank: int | None
    channels: set[str]
    rrf_score: float


def _bounded(value: str, limit: int = 600) -> str:
    if len(value) <= limit:
        return value
    return value[:limit]


def _centered_snippet(text: str, query: str, max_length: int = 200) -> str:
    """A bounded snippet centered on the earliest query-term match.

    Lexical hits carry an obvious anchor; vector-only hits fall back to the
    start of the chunk.  The result is at most ``max_length`` characters and
    keeps source text source-faithful (no summarization).
    """

    limit = max(1, int(max_length))
    if len(text) <= limit:
        return text
    lowered = text.lower()
    positions: list[int] = []
    for token in re.findall(r"[^\W_]+", query.lower()):
        if len(token) < 2:
            continue
        found = lowered.find(token)
        if found >= 0:
            positions.append(found)
    center = min(positions) if positions else 0
    start = max(0, min(center, len(text) - limit))
    end = start + limit
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end].strip()}{suffix}"


def _line_for_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, max(0, offset)) + 1


def _span_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _row_mapping(row: RowLike) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return dict(zip(row.keys(), row))
    return dict(row)


class RetrievalService:
    def __init__(self, indexer: Indexer) -> None:
        self.indexer = indexer

    @property
    def store(self) -> SQLiteIndex:
        return self.indexer.store

    def _reconcile_freshness(self) -> None:
        # A query is a synchronization boundary.  Incremental indexing skips
        # unchanged paths, so this remains cheap for a steady-state corpus and
        # catches edits whose post-edit hook was missed.
        self.indexer.index()

    def _location_for_row(self, row: RowLike) -> SourceLocation:
        values = _row_mapping(row)
        path = canonical_path(str(values["document_path"]))
        fmt = str(values["format"])
        role = str(values["role"])
        stored_file_hash = str(values["file_hash"])
        if fmt == "pdf":
            page = int(values["page"] or 1)
            try:
                parsed = parse_file(path)
                page_text = (
                    parsed.pages[page - 1] if 1 <= page <= len(parsed.pages) else ""
                )
            except (
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
                KeyError,
                IndexError,
                UnsupportedDocumentError,
            ):
                page_text = str(values["text"])
            return SourceLocation(
                path,
                str(values["breadcrumb"]),
                page=page,
                span_hash=_span_hash(page_text),
                file_hash=stored_file_hash,
                snippet=_bounded(page_text),
                role=role,
                format=fmt,
            )
        content = str(values["content"]) if "content" in values else ""
        start = int(values["start_line"])
        end = int(values["end_line"])
        try:
            exact = span_text(content, start, end)
        except ValueError:
            exact = str(values["text"])
        return SourceLocation(
            path,
            str(values["breadcrumb"]),
            start,
            end,
            None,
            _span_hash(exact),
            stored_file_hash,
            _bounded(exact),
            role,
            fmt,
        )

    def _location_for_chunk_row(self, row: RowLike) -> SourceLocation:
        # SQL chunk rows do not always include the document content; fetch it
        # separately to calculate a hash over the exact source span.
        values = _row_mapping(row)
        document = self.store.document(str(values["document_path"]))
        merged = values
        if document is not None:
            merged["content"] = document["content"]
            merged["file_hash"] = document["file_hash"]
            merged["role"] = document["role"]
            merged["format"] = document["format"]
        return self._location_for_row(merged)

    def _breadcrumb_for_line(self, path: str, line: int) -> str:
        for row in self.store.chunks(path):
            if int(row["start_line"]) <= line <= int(row["end_line"]):
                return str(row["breadcrumb"])
        return ""

    def validate_location(self, location: SourceLocation) -> SourceLocation:
        """Validate revision, line/page range, and exact span content."""

        path = Path(canonical_path(location.path))
        if not path.exists() or not path.is_file():
            raise StaleSourceError(f"source no longer exists: {path}")
        data = path.read_bytes()
        current_hash = hashlib.sha256(data).hexdigest()
        if location.file_hash and current_hash != location.file_hash:
            raise StaleSourceError(f"source revision changed: {path}")
        document = self.store.document(str(path))
        if document is not None:
            current_role = str(document["role"])
            current_format = str(document["format"])
        else:
            current_role, _, _ = infer_role(
                path, source_format(path), root=self.indexer.root
            )
            current_format = source_format(path) or location.format
        if location.format == "pdf" or path.suffix.lower() == ".pdf":
            parsed = parse_file(path)
            if (
                location.page is None
                or location.page < 1
                or location.page > len(parsed.pages)
            ):
                raise StaleSourceError(
                    f"PDF page is outside the current document: {path}"
                )
            page_text = parsed.pages[location.page - 1]
            page_hash = _span_hash(page_text)
            if location.span_hash and page_hash != location.span_hash:
                raise StaleSourceError(
                    f"PDF page content changed: {path} page {location.page}"
                )
            return SourceLocation(
                str(path),
                location.breadcrumb,
                page=location.page,
                span_hash=page_hash,
                file_hash=current_hash,
                snippet=_bounded(page_text),
                role=current_role,
                format="pdf",
            )
        text = data.decode("utf-8", errors="replace")
        if location.start_line is None or location.end_line is None:
            raise ValidationError(
                "text locations require one-based start_line and end_line"
            )
        lines = text.splitlines()
        if (
            location.start_line < 1
            or location.end_line < location.start_line
            or location.end_line > max(1, len(lines))
        ):
            raise StaleSourceError(f"line span is outside the current source: {path}")
        exact = span_text(text, location.start_line, location.end_line)
        exact_hash = _span_hash(exact)
        if location.span_hash and exact_hash != location.span_hash:
            raise StaleSourceError(
                f"source span changed: {path}:{location.start_line}-{location.end_line}"
            )
        return SourceLocation(
            str(path),
            location.breadcrumb,
            location.start_line,
            location.end_line,
            None,
            exact_hash,
            current_hash,
            _bounded(exact),
            current_role,
            current_format,
        )

    def search(
        self,
        query: str,
        limit: int = DEFAULT_RESULT_LIMIT,
        *,
        source_roles: Sequence[str] | None = None,
        roles: Sequence[str] | None = None,
        snippet_only: bool = False,
        max_snippet_length: int = 200,
    ) -> list[SearchResult]:
        self._reconcile_freshness()
        role_filter = set(source_roles or roles or ())
        lexical_rows = self.store.lexical_search(query, MAX_CHANNEL_CANDIDATES)
        lexical: dict[int, tuple[sqlite3.Row, int, float]] = {}
        for rank, row in enumerate(lexical_rows, start=1):
            if role_filter and str(row["role"]) not in role_filter:
                continue
            # sqlite bm25 is lower-is-better; expose a bounded positive score.
            raw = float(row["bm25_score"] or 0.0)
            lexical[int(row["id"])] = (row, rank, 1.0 / (1.0 + max(0.0, -raw)))
        try:
            query_vector = embed_queries(self.indexer.embedder, [query])[0]
            vector_rows = self.store.vector_search(query_vector, MAX_CHANNEL_CANDIDATES)
        except (
            ModelNotInstalledError,
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            sqlite3.Error,
        ):
            vector_rows = []
        vector: dict[int, tuple[sqlite3.Row, int, float]] = {}
        for rank, (row, score) in enumerate(vector_rows, start=1):
            if role_filter and str(row["role"]) not in role_filter:
                continue
            vector[int(row["id"])] = (row, rank, float(score))
        combined: dict[int, _RankedChunk] = {}
        for chunk_id, (row, rank, score) in lexical.items():
            item = combined.get(chunk_id)
            if item is None:
                item = {
                    "row": row,
                    "lexical_rank": rank,
                    "lexical_score": score,
                    "vector_score": None,
                    "vector_rank": None,
                    "channels": set(),
                    "rrf_score": 0.0,
                }
                combined[chunk_id] = item
            item["channels"].add("lexical")
        for chunk_id, (row, rank, score) in vector.items():
            item = combined.get(chunk_id)
            if item is None:
                item = {
                    "row": row,
                    "lexical_rank": None,
                    "lexical_score": None,
                    "vector_score": score,
                    "vector_rank": rank,
                    "channels": set(),
                    "rrf_score": 0.0,
                }
                combined[chunk_id] = item
            item["vector_rank"] = rank
            item["vector_score"] = score
            item["channels"].add("vector")
        ranked: list[tuple[float, _RankedChunk]] = []
        for item in combined.values():
            lexical_rank = item["lexical_rank"]
            vector_rank = item["vector_rank"]
            score = (
                1.0 / (RRF_K + lexical_rank) if lexical_rank is not None else 0.0
            ) + (1.0 / (RRF_K + vector_rank) if vector_rank is not None else 0.0)
            item["rrf_score"] = score
            ranked.append((score, item))
        ranked.sort(
            key=lambda value: (
                -value[0],
                str(value[1]["row"]["document_path"]),
                int(value[1]["row"]["ordinal"]),
            )
        )
        results: list[SearchResult] = []
        seen_spans: list[tuple[str, int, int]] = []
        target = max(0, int(limit))
        for score, item in ranked:
            if len(results) >= target:
                break
            row = item["row"]
            location = self._location_for_chunk_row(row)
            if location.start_line is not None and location.end_line is not None:
                span = (location.path, location.start_line, location.end_line)
                if any(
                    span[0] == other[0] and span[1] <= other[2] and other[1] <= span[2]
                    for other in seen_spans
                ):
                    continue
                seen_spans.append(span)
            snippet = _centered_snippet(
                str(row["text"]), query, max_snippet_length
            )
            location.snippet = snippet
            results.append(
                SearchResult(
                    location,
                    snippet if snippet_only else str(row["text"]),
                    score,
                    item["lexical_score"],
                    item["vector_score"],
                    item["lexical_rank"],
                    item["vector_rank"],
                    tuple(sorted(item["channels"])),
                )
            )
        return results

    def find(
        self,
        pattern: str,
        mode: str = "literal",
        cursor: str | int | None = None,
        *,
        source_roles: Sequence[str] | None = None,
        roles: Sequence[str] | None = None,
    ) -> list[FindResult]:
        self._reconcile_freshness()
        if mode not in ("literal", "regex"):
            raise ValueError("find mode must be literal or regex")
        if pattern == "":
            raise ValueError("find pattern must not be empty")
        expression = re.compile(
            re.escape(pattern) if mode == "literal" else pattern, re.MULTILINE
        )
        role_filter = set(source_roles or roles or ())
        results: list[FindResult] = []
        for row in self.store.documents():
            if role_filter and row["role"] not in role_filter:
                continue
            path = row["path"]
            if row["format"] == "pdf":
                try:
                    parsed = parse_file(path)
                    pages = parsed.pages
                except (
                    OSError,
                    RuntimeError,
                    TypeError,
                    ValueError,
                    KeyError,
                    IndexError,
                    UnsupportedDocumentError,
                ):
                    pages = []
                for page_number, page_text in enumerate(pages, start=1):
                    for match in expression.finditer(page_text):
                        results.append(
                            FindResult(
                                SourceLocation(
                                    path,
                                    f"Page {page_number}",
                                    page=page_number,
                                    span_hash=_span_hash(page_text),
                                    file_hash=row["file_hash"],
                                    snippet=_bounded(page_text),
                                    role=row["role"],
                                    format="pdf",
                                ),
                                match.group(0),
                                page_text,
                                match.start() + 1,
                                match.end() + 1,
                            )
                        )
                continue
            text = row["content"]
            for match in expression.finditer(text):
                start_line = _line_for_offset(text, match.start())
                end_line = _line_for_offset(text, max(match.start(), match.end() - 1))
                exact = span_text(text, start_line, end_line)
                results.append(
                    FindResult(
                        SourceLocation(
                            path,
                            self._breadcrumb_for_line(path, start_line),
                            start_line,
                            end_line,
                            None,
                            _span_hash(exact),
                            row["file_hash"],
                            _bounded(exact),
                            row["role"],
                            row["format"],
                        ),
                        match.group(0),
                        exact,
                        match.start() - text.rfind("\n", 0, match.start()),
                        match.end() - text.rfind("\n", 0, match.end()),
                    )
                )
        results.sort(
            key=lambda item: (
                item.location.path,
                item.location.page or 0,
                item.location.start_line or 0,
                item.start_column,
                item.match,
            )
        )
        offset = int(cursor or 0) if str(cursor or "0").isdigit() else 0
        return results[offset:]

    def evaluate_search(
        self, queries: Mapping[str, Sequence[str]], limit: int = DEFAULT_RESULT_LIMIT
    ) -> SearchMetrics:
        """Report MRR and Recall@8 for a small labelled evaluation set.

        ``queries`` maps a query to one or more canonical paths considered
        relevant.  Evaluation is read-only and intentionally separate from
        normal precision search responses.
        """

        if not queries:
            return SearchMetrics(0.0, 0.0, 0, 0)
        reciprocal = 0.0
        recalled = 0
        result_count = 0
        for query, relevant_values in queries.items():
            relevant = {
                canonical_path(path, base=self.indexer.root) for path in relevant_values
            }
            found = self.search(query, max(8, int(limit)))
            result_count += len(found)
            for rank, result in enumerate(found, start=1):
                if canonical_path(result.location.path) in relevant:
                    reciprocal += 1.0 / rank
                    break
            if any(
                canonical_path(result.location.path) in relevant for result in found[:8]
            ):
                recalled += 1
        total = float(len(queries))
        return SearchMetrics(
            reciprocal / total, recalled / total, len(queries), result_count
        )

    def read(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        page: int | None = None,
    ) -> ReadResult:
        self._reconcile_freshness()
        canonical = canonical_path(path, base=self.indexer.root)
        row = self.store.document(canonical)
        if row is None:
            if not Path(canonical).exists():
                raise FileNotFoundError(canonical)
            raise ValidationError(f"source is not indexed or configured: {canonical}")
        else:
            role = row["role"]
            fmt = row["format"]
        parsed = parse_file(canonical)
        if fmt == "pdf":
            if page is None:
                content = parsed.text
                selected_page = None
            elif page < 1 or page > len(parsed.pages):
                raise ValueError("PDF page is outside the document")
            else:
                content = parsed.pages[page - 1]
                selected_page = page
            return ReadResult(
                canonical,
                content,
                page=selected_page,
                file_hash=parsed.file_hash,
                role=role,
                format="pdf",
            )
        if page is not None:
            raise ValueError("page is only valid for PDF sources")
        lines = parsed.text.splitlines()
        if not lines and start_line is None and end_line is None:
            return ReadResult(
                canonical, "", 1, 1, file_hash=parsed.file_hash, role=role, format=fmt
            )
        first = 1 if start_line is None else int(start_line)
        last = len(lines) if end_line is None else int(end_line)
        if first < 1 or last < first or last > max(1, len(lines)):
            raise ValueError("line range is outside the source")
        return ReadResult(
            canonical,
            span_text(parsed.text, first, last),
            first,
            last,
            file_hash=parsed.file_hash,
            role=role,
            format=fmt,
        )


def search(
    indexer: Indexer, query: str, limit: int = DEFAULT_RESULT_LIMIT, **kwargs: Any
) -> list[SearchResult]:
    return RetrievalService(indexer).search(query, limit, **kwargs)


def find(
    indexer: Indexer,
    pattern: str,
    mode: str = "literal",
    cursor: str | int | None = None,
    **kwargs: Any,
) -> list[FindResult]:
    return RetrievalService(indexer).find(pattern, mode, cursor, **kwargs)


def read(
    indexer: Indexer,
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    page: int | None = None,
) -> ReadResult:
    return RetrievalService(indexer).read(path, start_line, end_line, page)


def search_metrics(
    indexer: Indexer,
    queries: Mapping[str, Sequence[str]],
    limit: int = DEFAULT_RESULT_LIMIT,
) -> SearchMetrics:
    return RetrievalService(indexer).evaluate_search(queries, limit)


def validate_location(indexer: Indexer, location: SourceLocation) -> SourceLocation:
    return RetrievalService(indexer).validate_location(location)
