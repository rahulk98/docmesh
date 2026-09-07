from __future__ import annotations

from pathlib import Path

from docmesh.embeddings import DeterministicEmbedder
from docmesh.index import Indexer, SQLiteIndex
from docmesh.models import Manifest, SourceConfig


def test_low_signal_chunk_is_excluded_from_vector_table_but_fts_searchable(
    tmp_path: Path,
) -> None:
    source = tmp_path / "figure.md"
    source.write_text(
        "# Figure\n0.1 0.2 0.3 0.4 0.5\n1e-3 1e-2 1e-1 axis\n\n"
        "## Discussion\nThis section explains the background of the method"
        " in real prose with enough distinct words to be a normal chunk."
        " It also walks through the experimental setup, the datasets used"
        " for evaluation, and the baseline comparisons reported in the"
        " results table so the document reads as substantive prose overall.\n",
        encoding="utf-8",
    )
    store = SQLiteIndex(":memory:")
    indexer = Indexer(tmp_path, index=store, embedder=DeterministicEmbedder(8))
    indexer.index()

    chunks = store.chunks(str(source))
    assert len(chunks) >= 2
    vector_chunk_ids = {
        int(row[0]) for row in store.conn.execute("SELECT chunk_id FROM vectors")
    }
    degenerate = [c for c in chunks if c["breadcrumb"] == "Figure"]
    normal = [c for c in chunks if c["breadcrumb"] == "Figure > Discussion"]
    assert degenerate and normal
    assert degenerate[0]["id"] not in vector_chunk_ids
    assert normal[0]["id"] in vector_chunk_ids

    # Still lexically searchable (find/grep parity).
    hits = store.lexical_search("axis")
    assert any(row["id"] == degenerate[0]["id"] for row in hits)


def test_invalid_utf8_document_is_indexed_with_a_warning(tmp_path: Path) -> None:
    source = tmp_path / "latin1.txt"
    source.write_bytes("café \xe9\xe8".encode("latin1"))
    manifest = Manifest(str(tmp_path), sources=[SourceConfig(str(source), "editable")])
    indexer = Indexer(
        tmp_path,
        manifest=manifest,
        index=SQLiteIndex(":memory:"),
        embedder=DeterministicEmbedder(8),
    )
    status = indexer.index()
    assert status.documents == 1
    assert any(
        w["path"] == str(source) and "invalid UTF-8" in w["reason"]
        for w in status.warnings
    )


def test_binary_document_is_skipped(tmp_path: Path) -> None:
    source = tmp_path / "binary.md"
    source.write_bytes(bytes(range(256)) * 32)
    manifest = Manifest(str(tmp_path), sources=[SourceConfig(str(source), "editable")])
    indexer = Indexer(
        tmp_path,
        manifest=manifest,
        index=SQLiteIndex(":memory:"),
        embedder=DeterministicEmbedder(8),
    )
    status = indexer.index()
    assert status.documents == 0
    assert any(item["path"] == str(source) for item in status.skipped_documents)


def test_stale_strategy_marker_still_triggers_a_rebuild_on_next_index(
    tmp_path: Path,
) -> None:
    # Regression for the case where a strategy change (e.g. adding the
    # low-signal vector filter) was detected by an Indexer construction that
    # never called index() (a status/search/bench call): that must not stamp
    # the new marker early, or the real index() call later sees
    # previous == computed and skips the rebuild forever.
    source = tmp_path / "figure.md"
    source.write_text(
        "# Figure\n0.1 0.2 0.3 0.4 0.5\n1e-3 1e-2 1e-1 axis\n\n"
        "## Discussion\nThis section explains the background of the method"
        " in real prose with enough distinct words to be a normal chunk.\n",
        encoding="utf-8",
    )
    manifest = Manifest(str(tmp_path), sources=[SourceConfig(str(source), "editable")])
    db_path = str(tmp_path / "index.sqlite3")
    Indexer(
        tmp_path, manifest=manifest, db_path=db_path, embedder=DeterministicEmbedder(8)
    ).index()

    store = SQLiteIndex(db_path)
    store.set_metadata("embedding_strategy_id", "v1-stale-pre-filter-marker")
    store.conn.execute("DELETE FROM vectors")
    store.conn.execute(
        "INSERT INTO vectors(chunk_id,dimensions,vector) "
        "SELECT id, 8, zeroblob(8*4) FROM chunks"
    )
    store.conn.commit()

    # A construction that never calls index() (simulating status/search/bench).
    Indexer(tmp_path, manifest=manifest, index=store, embedder=DeterministicEmbedder(8))
    assert store.get_metadata("embedding_strategy_id") == "v1-stale-pre-filter-marker"

    indexer = Indexer(
        tmp_path, manifest=manifest, index=store, embedder=DeterministicEmbedder(8)
    )
    assert indexer.strategy_changed is True
    indexer.index()

    chunks = store.chunks(str(source))
    vector_chunk_ids = {
        int(row[0]) for row in store.conn.execute("SELECT chunk_id FROM vectors")
    }
    degenerate = [c for c in chunks if c["breadcrumb"] == "Figure"]
    assert degenerate and degenerate[0]["id"] not in vector_chunk_ids
    assert store.get_metadata("embedding_strategy_id") == indexer.strategy_id
