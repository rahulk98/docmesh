"""Precision search, exhaustive find, source reads, and location validation."""

from __future__ import annotations

import bisect
import hashlib
import os
import re
import sqlite3
import time
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
    ValidationError,
)
from .parsing import parse_file, span_text

RRF_K = 60
DEFAULT_RESULT_LIMIT = 8
_LAST_RECONCILE_KEY = "retrieval_last_reconcile_attempt"
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


class _LineIndex:
    """Amortizes line/column/span lookups over many matches in one document.

    Built once per document (O(n) in file size); every lookup afterward is
    O(log lines), replacing what used to be a fresh ``text.count``/
    ``splitlines()``/``rfind`` scan of the whole document per match.
    """

    __slots__ = ("_newline_at", "_line_starts", "_lines")

    def __init__(self, text: str) -> None:
        newline_at = [index for index, char in enumerate(text) if char == "\n"]
        self._newline_at = newline_at
        self._line_starts = [0] + [index + 1 for index in newline_at]
        self._lines = text.splitlines()

    def line_for_offset(self, offset: int) -> int:
        return bisect.bisect_left(self._newline_at, max(0, offset)) + 1

    def line_start_offset(self, line: int) -> int:
        index = line - 1
        starts = self._line_starts
        if 0 <= index < len(starts):
            return starts[index]
        return starts[-1]

    def span(self, start_line: int, end_line: int) -> str:
        return "\n".join(self._lines[start_line - 1 : end_line])


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
        # A query is a synchronization boundary, but the naive form of that
        # (a full ``index()`` scan) reads and sha256-hashes every source's
        # full bytes on every single query, and re-parses any PDF whose hash
        # changed. For a large corpus that dwarfs the actual find/search
        # cost (F-find-1). ``indexed_at`` is already stored per document, so
        # a cheap mtime comparison tells us which paths could possibly have
        # changed; only those go through the real (hash + reindex) path.
        # Newly added files are still caught via discovery, which is a
        # filesystem walk, not a content read.
        stale: set[str] = set()
        rows = self.store.conn.execute(
            "SELECT path, indexed_at FROM documents WHERE active=1"
        ).fetchall()
        for path, indexed_at in rows:
            try:
                mtime = os.stat(path).st_mtime
            except OSError:
                stale.add(path)  # removed since indexing; let index() retire it
                continue
            if mtime > float(indexed_at):
                stale.add(path)
        try:
            discovered = self.indexer._current_discovery()
        except (OSError, FileNotFoundError, NotADirectoryError):
            discovered = []
        known = self.store.document_paths()
        # A discovered path with no document row is either genuinely new or
        # a source the indexer already tried and skipped (corrupt/encrypted
        # PDF, binary file, ...). A skipped file never gets a document row,
        # so re-parsing it every call was the bug: it looked "new" forever.
        # Only retry it if it changed since the last time we looked -- the
        # watermark below is either the last reconcile attempt or the newest
        # successful index, whichever is later.
        watermark = self._last_reconcile_watermark()
        for item in discovered:
            path = canonical_path(item.path)
            if path in known:
                continue
            try:
                mtime = os.stat(path).st_mtime
            except OSError:
                continue
            if mtime > watermark:
                stale.add(path)
        if stale:
            self.indexer.index(paths=sorted(stale))
        self.store.conn.execute(
            "INSERT INTO metadata(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_LAST_RECONCILE_KEY, str(time.time())),
        )
        self.store.conn.commit()

    def _last_reconcile_watermark(self) -> float:
        row = self.store.conn.execute(
            "SELECT value FROM metadata WHERE key=?", (_LAST_RECONCILE_KEY,)
        ).fetchone()
        watermark = float(row[0]) if row else 0.0
        newest = self.store.conn.execute(
            "SELECT MAX(indexed_at) FROM documents WHERE active=1"
        ).fetchone()
        if newest and newest[0] is not None:
            watermark = max(watermark, float(newest[0]))
        return watermark

    def _location_for_row(self, row: RowLike) -> SourceLocation:
        values = _row_mapping(row)
        path = canonical_path(str(values["document_path"]))
        fmt = str(values["format"])
        role = str(values["role"])
        stored_file_hash = str(values["file_hash"])
        generated_from = values.get("generated_from")
        page = values.get("page")
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
            int(page) if page is not None else None,
            _span_hash(exact),
            stored_file_hash,
            _bounded(exact),
            role,
            fmt,
            str(generated_from) if generated_from else None,
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
            merged["generated_from"] = document["generated_from"]
        return self._location_for_row(merged)

    def _breadcrumb_index(
        self, path: str
    ) -> tuple[list[int], list[tuple[int, str, int | None]]]:
        """One DB read per document: (sorted start_lines, [(end_line, breadcrumb, page)])."""
        entries = sorted(
            (
                (
                    int(row["start_line"]),
                    int(row["end_line"]),
                    str(row["breadcrumb"]),
                    row["page"],
                )
                for row in self.store.chunks(path)
            )
        )
        starts = [entry[0] for entry in entries]
        rest = [(entry[1], entry[2], entry[3]) for entry in entries]
        return starts, rest

    @staticmethod
    def _breadcrumb_for_line(
        index: tuple[list[int], list[tuple[int, str, int | None]]], line: int
    ) -> tuple[str, int | None]:
        starts, rest = index
        pos = bisect.bisect_right(starts, line) - 1
        if pos < 0:
            return "", None
        end_line, breadcrumb, page = rest[pos]
        if line <= end_line:
            return breadcrumb, (int(page) if page is not None else None)
        return "", None

    def validate_location(self, location: SourceLocation) -> SourceLocation:
        """Validate revision, line/page range, and exact span content."""

        path = Path(canonical_path(location.path))
        if not path.exists() or not path.is_file():
            raise StaleSourceError(f"source no longer exists: {path}")
        # PDFs are indexed only via their generated Markdown mirror, which is
        # an ordinary text file on disk; the general path below (fresh read,
        # fresh hash, fresh span) is correct for it without ever touching
        # pypdf again.
        data = path.read_bytes()
        current_hash = hashlib.sha256(data).hexdigest()
        if location.file_hash and current_hash != location.file_hash:
            raise StaleSourceError(f"source revision changed: {path}")
        document = self.store.document(str(path))
        generated_from = None
        if document is not None:
            current_role = str(document["role"])
            current_format = str(document["format"])
            generated_from = document["generated_from"]
        else:
            current_role, _, _ = infer_role(
                path, source_format(path), root=self.indexer.root
            )
            current_format = source_format(path) or location.format
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
            location.page,
            exact_hash,
            current_hash,
            _bounded(exact),
            current_role,
            current_format,
            str(generated_from) if generated_from else None,
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
        scope: str | None = None,
    ) -> list[FindResult]:
        self._reconcile_freshness()
        if mode not in ("literal", "regex"):
            raise ValueError("find mode must be literal or regex")
        if pattern == "":
            raise ValueError("find pattern must not be empty")
        try:
            expression = re.compile(
                re.escape(pattern) if mode == "literal" else pattern, re.MULTILINE
            )
        except re.error as exc:
            raise ValidationError(f"invalid find pattern: {exc}") from exc
        role_filter = set(source_roles or roles or ())
        scope_prefix = (
            canonical_path(scope, base=self.indexer.root) if scope else None
        )
        results: list[FindResult] = []
        for row in self.store.documents():
            if role_filter and row["role"] not in role_filter:
                continue
            path = row["path"]
            if scope_prefix and path != scope_prefix and not path.startswith(
                scope_prefix + "/"
            ):
                continue
            if row["format"] == "pdf":
                # A PDF's own placeholder row carries no content -- only its
                # generated mirror is chunked and searchable.
                continue
            text = row["content"]
            generated_from = row["generated_from"]
            breadcrumb_index: (
                tuple[list[int], list[tuple[int, str, int | None]]] | None
            ) = None
            line_index: _LineIndex | None = None
            for match in expression.finditer(text):
                if line_index is None:
                    # Built once per document: turns each match's line/column
                    # lookup and exact-span slice into O(log lines) instead of
                    # rescanning the whole document per match (the old
                    # ``text.count``/``splitlines()``/``rfind`` calls here made
                    # a large-match-count find() quadratic in file size).
                    line_index = _LineIndex(text)
                start_line = line_index.line_for_offset(match.start())
                end_line = line_index.line_for_offset(
                    max(match.start(), match.end() - 1)
                )
                exact = line_index.span(start_line, end_line)
                if breadcrumb_index is None:
                    breadcrumb_index = self._breadcrumb_index(path)
                breadcrumb, page = self._breadcrumb_for_line(
                    breadcrumb_index, start_line
                )
                results.append(
                    FindResult(
                        SourceLocation(
                            path,
                            breadcrumb,
                            start_line,
                            end_line,
                            page,
                            _span_hash(exact),
                            row["file_hash"],
                            _bounded(exact),
                            row["role"],
                            row["format"],
                            str(generated_from) if generated_from else None,
                        ),
                        match.group(0),
                        exact,
                        match.start() - line_index.line_start_offset(start_line),
                        match.end() - line_index.line_start_offset(
                            line_index.line_for_offset(match.end())
                        ),
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
        if fmt == "pdf":
            # A PDF has no content of its own: it is indexed only via its
            # generated Markdown mirror.  Redirect the read to the mirror and
            # report both paths (mirror path, plus generated_from == the PDF).
            mirror_row = self.store.mirror_for(canonical)
            if mirror_row is None:
                raise ValidationError(f"PDF has no generated mirror: {canonical}")
            if page is None and (start_line is not None or end_line is not None):
                return self.read(str(mirror_row["path"]), start_line, end_line, page)
            return self._read_mirror(str(mirror_row["path"]), page, canonical)
        if row["role"] == "mirror" and page is not None:
            return self._read_mirror(canonical, page, str(row["generated_from"] or ""))
        if page is not None:
            raise ValueError("page is only valid for PDF/mirror sources")
        parsed = parse_file(canonical)
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

    def _read_mirror(
        self, mirror_path: str, page: int | None, generated_from: str
    ) -> ReadResult:
        """Read a mirror by page (its ``## Page N`` span) or in full."""

        parsed = parse_file(mirror_path)
        if page is None:
            return ReadResult(
                mirror_path,
                parsed.text,
                file_hash=parsed.file_hash,
                role="mirror",
                format=parsed.format,
                generated_from=generated_from or None,
            )
        target = f"Page {page}"
        section = next(
            (
                item
                for item in parsed.sections
                if item.breadcrumb and item.breadcrumb[-1] == target
            ),
            None,
        )
        if section is None:
            raise ValueError("PDF page is outside the document")
        return ReadResult(
            mirror_path,
            span_text(parsed.text, section.start_line, section.end_line),
            section.start_line,
            section.end_line,
            page=page,
            file_hash=parsed.file_hash,
            role="mirror",
            format=parsed.format,
            generated_from=generated_from or None,
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
