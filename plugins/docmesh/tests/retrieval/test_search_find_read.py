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


def test_indexer_skips_corrupt_pdf_and_keeps_indexing_the_rest(tmp_path: Path) -> None:
    (tmp_path / "guide.md").write_text(
        "# Guide\nThe canonical policy applies.", encoding="utf-8"
    )
    (tmp_path / "corrupt.pdf").write_bytes(b"<!doc truncated")
    indexer = Indexer(
        tmp_path, index=SQLiteIndex(":memory:"), embedder=DeterministicEmbedder(32)
    )
    status = indexer.index()
    assert status.documents == 1
    assert len(status.skipped_documents) == 1
    assert status.skipped_documents[0]["path"].endswith("corrupt.pdf")
    assert status.skipped_documents[0]["reason"]
    assert not indexer.store.document(str((tmp_path / "corrupt.pdf").resolve()))
    results = RetrievalService(indexer).search("canonical policy", limit=2)
    assert results and results[0].location.path.endswith("guide.md")

    status = indexer.index(force=True)
    assert status.documents == 1
    assert len(status.skipped_documents) == 1


def test_search_snippets_are_concise_and_centered_on_the_match(
    tmp_path: Path,
) -> None:
    (tmp_path / "guide.md").write_text(
        "# Guide\n" + ("filler text " * 120) + "needle phrase\n"
        + ("mop up " * 120),
        encoding="utf-8",
    )
    indexer = Indexer(
        tmp_path, index=SQLiteIndex(":memory:"), embedder=DeterministicEmbedder(32)
    )
    indexer.index()
    service = RetrievalService(indexer)
    full = service.search("needle phrase", limit=2)
    assert full
    full_text = full[0].text
    assert len(full_text) > 300
    assert "needle phrase" in full_text

    concise = service.search(
        "needle phrase", limit=2, snippet_only=True, max_snippet_length=100
    )
    assert concise
    result = concise[0]
    assert result.text == result.location.snippet
    assert len(result.text) <= 100 + 2
    lowered = result.text.lower()
    assert "needle" in lowered or "needle phrase" in lowered


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
