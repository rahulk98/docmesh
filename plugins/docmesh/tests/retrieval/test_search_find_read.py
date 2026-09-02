from pathlib import Path

from docmesh.embeddings import DeterministicEmbedder
from docmesh.index import Indexer, SQLiteIndex
from docmesh.models import Manifest, SourceConfig, ValidationError
from docmesh.retrieval import RetrievalService


def test_index_search_find_and_read_use_exact_current_locations(tmp_path: Path) -> None:
    (tmp_path / "guide.md").write_text(
        "# Guide\nThe canonical policy applies.\nA second policy mention.",
        encoding="utf-8",
    )
    indexer = Indexer(
        tmp_path, index=SQLiteIndex(":memory:"), embedder=DeterministicEmbedder(32)
    )
    status = indexer.index()
    assert status.documents == 1
    service = RetrievalService(indexer)
    results = service.search("canonical policy", limit=2)
    assert results and results[0].location.path.endswith("guide.md")
    matches = service.find("policy", mode="literal")
    assert len(matches) == 2
    assert [match.location.start_line for match in matches] == [2, 3]
    read = service.read("guide.md", start_line=2, end_line=2)
    assert read.content == "The canonical policy applies."


def test_read_allows_configured_external_source_and_rejects_unconfigured_file(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    external = tmp_path / "shared" / "reference.md"
    unconfigured = tmp_path / "outside.md"
    project.mkdir()
    external.parent.mkdir()
    external.write_text("# Shared\nConfigured external evidence.", encoding="utf-8")
    unconfigured.write_text(
        "# Outside\nThis file is not in the corpus.", encoding="utf-8"
    )
    manifest = Manifest(
        str(project), sources=[SourceConfig(str(external), "reference")]
    )
    indexer = Indexer(
        project,
        manifest=manifest,
        index=SQLiteIndex(":memory:"),
        embedder=DeterministicEmbedder(16),
    )
    indexer.index()
    service = RetrievalService(indexer)

    configured_read = service.read(str(external))
    assert configured_read.content == "# Shared\nConfigured external evidence."
    assert configured_read.role == "reference"
    try:
        service.read(str(unconfigured))
    except ValidationError as exc:
        assert "indexed or configured" in str(exc)
    else:
        raise AssertionError("read must reject an existing but unconfigured source")
