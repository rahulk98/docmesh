import re
from pathlib import Path

from docmesh import api
from docmesh.embeddings import DeterministicEmbedder
from docmesh.index import Indexer, SQLiteIndex
from docmesh.models import Manifest, SourceConfig, ValidationError
from docmesh.retrieval import RetrievalService, _pdf_match_fields


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


def test_pdf_match_fields_return_only_the_matched_line_and_a_bounded_snippet() -> None:
    filler = "filler text " * 200
    page_text = f"{filler}\nneedle phrase on its own line\n{filler}"
    match = next(re.finditer("needle phrase", page_text))

    line_text, snippet, start_col, end_col = _pdf_match_fields(page_text, match)

    assert line_text == "needle phrase on its own line"
    assert len(snippet) <= 810
    assert "needle phrase" in snippet
    assert start_col == 1
    assert end_col == 1 + len("needle phrase")


def test_find_scope_restricts_to_a_subtree_and_respects_path_boundaries(
    tmp_path: Path,
) -> None:
    (tmp_path / "paper").mkdir()
    (tmp_path / "paper.md").write_text("# Paper\nneedle here.", encoding="utf-8")
    (tmp_path / "paper" / "draft.md").write_text(
        "# Draft\nneedle here too.", encoding="utf-8"
    )
    indexer = Indexer(
        tmp_path, index=SQLiteIndex(":memory:"), embedder=DeterministicEmbedder(16)
    )
    indexer.index()
    service = RetrievalService(indexer)

    scoped = service.find("needle", scope="paper")
    assert len(scoped) == 1
    assert scoped[0].location.path.endswith("paper/draft.md")

    unscoped = service.find("needle")
    assert len(unscoped) == 2


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


def test_search_collapses_overlapping_chunks_for_the_same_path(
    tmp_path: Path,
) -> None:
    (tmp_path / "guide.md").write_text(
        "# Guide\n"
        + "\n".join(f"policy statement number {i}" for i in range(1, 5)),
        encoding="utf-8",
    )
    indexer = Indexer(
        tmp_path, index=SQLiteIndex(":memory:"), embedder=DeterministicEmbedder(32)
    )
    indexer.index()
    store = indexer.store
    row = store.conn.execute("SELECT * FROM chunks LIMIT 1").fetchone()
    # Simulate a second chunk that overlaps the same source lines (e.g. from a
    # re-chunk boundary shift); the ranker must not surface both.
    cursor = store.conn.execute(
        "INSERT INTO chunks(document_path,ordinal,breadcrumb,text,start_line,end_line,"
        "page,token_count,embedding_input,text_hash,embedding_strategy_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            row["document_path"],
            row["ordinal"] + 1000,
            row["breadcrumb"],
            row["text"],
            row["start_line"],
            row["end_line"],
            row["page"],
            row["token_count"],
            row["embedding_input"],
            row["text_hash"],
            row["embedding_strategy_id"],
        ),
    )
    new_id = cursor.lastrowid
    store.conn.execute(
        "INSERT INTO chunks_fts(rowid,breadcrumb,text) VALUES(?,?,?)",
        (new_id, row["breadcrumb"], row["text"]),
    )
    vector_row = store.conn.execute(
        "SELECT vector, dimensions FROM vectors WHERE chunk_id=?", (row["id"],)
    ).fetchone()
    store.conn.execute(
        "INSERT INTO vectors(chunk_id,dimensions,vector) VALUES(?,?,?)",
        (new_id, vector_row["dimensions"], vector_row["vector"]),
    )
    store.conn.commit()

    service = RetrievalService(indexer)
    results = service.search("policy statement", limit=8)
    spans = [
        (result.location.path, result.location.start_line, result.location.end_line)
        for result in results
    ]
    assert len(spans) == len(set(spans))


def test_bench_reports_mean_reciprocal_rank_and_recall_at_8(tmp_path: Path) -> None:
    (tmp_path / "guide.md").write_text(
        "# Guide\nThe canonical policy applies.\n", encoding="utf-8"
    )
    (tmp_path / "other.md").write_text(
        "# Other\nUnrelated shipping schedule notes.\n", encoding="utf-8"
    )
    queries = [
        {"query": "canonical policy", "expect_path_contains": "guide.md"},
        {"query": "shipping schedule", "expect_path_contains": "other.md"},
        {"query": "nonexistent topic entirely", "expect_path_contains": "missing.md"},
    ]
    result = api.bench(
        project_root=tmp_path,
        queries=queries,
        deterministic=True,
        dimensions=32,
        db_path=str(tmp_path / "index.sqlite3"),
    )
    assert result["queries"] == 3
    assert result["recall_at_8"] == 2 / 3
    assert result["mean_reciprocal_rank"] > 0.0
    hit_ranks = [item["hit_rank"] for item in result["per_query"]]
    assert hit_ranks[0] == 1
    assert hit_ranks[1] == 1
    assert hit_ranks[2] is None


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
