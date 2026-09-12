from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from docmesh.embeddings import DeterministicEmbedder
from docmesh.index import PROJECT_ROOT_METADATA_KEY, Indexer, SQLiteIndex
from docmesh.models import ValidationError
from docmesh.config import canonical_path

PROSE = (
    "# Method\nThis section explains the method in real prose with enough"
    " distinct words to be a normal chunk. It walks through the experimental"
    " setup, the datasets used for evaluation, and the baseline comparisons"
    " reported in the results table.\n"
)


def _indexer(root: Path, db: str) -> Indexer:
    return Indexer(root, index=SQLiteIndex(db), embedder=DeterministicEmbedder(8))


def test_moved_project_rebases_indexed_paths_without_reindexing(
    tmp_path: Path,
) -> None:
    old_root = tmp_path / "old"
    old_root.mkdir()
    (old_root / "paper.md").write_text(PROSE, encoding="utf-8")
    db = str(tmp_path / "index.db")

    indexer = _indexer(old_root, db)
    indexer.index()
    chunk_count = len(indexer.store.chunks(str(old_root / "paper.md")))
    assert chunk_count > 0
    indexer.store.conn.close()

    new_root = tmp_path / "new"
    shutil.move(str(old_root), str(new_root))

    moved = _indexer(new_root, db)
    assert moved.relocated_project is True
    paths = moved.store.document_paths()
    assert paths == {canonical_path(new_root / "paper.md")}
    # Chunks follow their document rather than being dropped and re-embedded.
    assert len(moved.store.chunks(str(new_root / "paper.md"))) == chunk_count
    assert moved.store.get_metadata(PROJECT_ROOT_METADATA_KEY) == canonical_path(
        new_root
    )

    status = moved.index()
    assert status.documents == 1
    assert status.stale_documents == []


def test_unmoved_project_is_not_treated_as_relocated(tmp_path: Path) -> None:
    (tmp_path / "paper.md").write_text(PROSE, encoding="utf-8")
    db = str(tmp_path / "index.db")

    first = _indexer(tmp_path, db)
    first.index()
    first.store.conn.close()

    again = _indexer(tmp_path, db)
    assert again.relocated_project is False
    assert again.legacy_root_unknown is False


def test_legacy_database_without_root_marker_repairs_on_full_scan(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "paper.md").write_text(PROSE, encoding="utf-8")
    db = str(tmp_path / "index.db")

    seeded = _indexer(root, db)
    seeded.index()
    # Simulate a database written before the project-root marker existed.
    seeded.store.conn.execute(
        "DELETE FROM metadata WHERE key=?", (PROJECT_ROOT_METADATA_KEY,)
    )
    seeded.store.conn.commit()
    seeded.store.conn.close()

    legacy = _indexer(root, db)
    assert legacy.legacy_root_unknown is True
    # A selective index cannot be trusted until the full scan re-establishes
    # which checkout the stored paths belong to.
    with pytest.raises(ValidationError):
        legacy.index(paths=[str(root / "paper.md")])

    legacy.index()
    assert legacy.store.get_metadata(PROJECT_ROOT_METADATA_KEY) == canonical_path(root)
