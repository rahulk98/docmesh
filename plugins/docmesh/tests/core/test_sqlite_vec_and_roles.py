from __future__ import annotations

from pathlib import Path

import pytest

from docmesh.embeddings import DeterministicEmbedder
from docmesh.index import Indexer, SQLiteIndex
from docmesh.models import Manifest, SourceConfig


def test_document_role_is_updated_on_replace_conflict(tmp_path: Path) -> None:
    source = tmp_path / "guide.md"
    source.write_text("# Guide\nRole can change.", encoding="utf-8")
    manifest = Manifest(str(tmp_path), sources=[SourceConfig(str(source), "editable")])
    indexer = Indexer(
        tmp_path,
        manifest=manifest,
        index=SQLiteIndex(":memory:"),
        embedder=DeterministicEmbedder(8),
    )
    indexer.index()
    assert indexer.store.document(str(source))["role"] == "editable"

    manifest.sources[0].role = "reference"
    indexer.index()
    assert indexer.store.document(str(source))["role"] == "reference"


def test_sqlite_vec_path_is_used_when_extension_is_available(tmp_path: Path) -> None:
    source = tmp_path / "guide.md"
    source.write_text("# Guide\nVector retrieval.", encoding="utf-8")
    store = SQLiteIndex(":memory:")
    indexer = Indexer(tmp_path, index=store, embedder=DeterministicEmbedder(8))
    indexer.index()

    # The test double represents a loaded sqlite-vec extension.  It records
    # the vector query while leaving the public result semantics unchanged.
    calls: list[tuple[tuple[float, ...], int]] = []

    def _vec_search(vector: list[float], limit: int):
        calls.append((tuple(vector), limit))
        return store._vector_search_portable(vector, limit)

    store.sqlite_vec_available = True
    store._vec_table_name = "docmesh_vec"
    store._vector_search_sqlite_vec = _vec_search
    rows = store.vector_search([1.0] + [0.0] * 7, 4)
    assert rows
    assert calls and calls[0][1] == 4

    # Replacement/removal must keep the extension mirror in sync.
    store._vec_delete_calls = []
    indexer.index(force=True)
    assert len(store.chunks()) == len(rows)
    store.remove_document(str(source))
    assert store.chunks() == []
    store.close()


def test_sqlite_vec_reopen_reuses_synchronized_rows(tmp_path: Path) -> None:
    source = tmp_path / "guide.md"
    source.write_text("# Guide\nVector persistence.", encoding="utf-8")
    database = tmp_path / "index.sqlite3"
    first_store = SQLiteIndex(str(database))
    if not first_store.sqlite_vec_available:
        first_store.close()
        pytest.skip("sqlite-vec extension is not available")
    Indexer(tmp_path, index=first_store, embedder=DeterministicEmbedder(8)).index()
    first_store.close()

    reopened = SQLiteIndex(str(database))
    calls: list[int] = []
    original_insert = reopened._insert_vec

    def record_insert(chunk_id: int, vector: list[float]) -> None:
        calls.append(chunk_id)
        original_insert(chunk_id, vector)

    reopened._insert_vec = record_insert
    Indexer(tmp_path, index=reopened, embedder=DeterministicEmbedder(8))

    assert calls == []
    reopened.close()
