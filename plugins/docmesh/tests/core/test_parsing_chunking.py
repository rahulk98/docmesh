from docmesh.chunking import (
    ApproximateTokenizer,
    Chunker,
    compute_embedding_strategy_id,
)
from docmesh.models import Manifest
from docmesh.parsing import parse_text


def test_markdown_breadcrumbs_and_token_budget_are_source_faithful() -> None:
    text = "# Contract\n\n## Limits\n" + "word " * 80
    parsed = parse_text("guide.md", text)
    assert [section.breadcrumb for section in parsed.sections] == [
        ("Contract",),
        ("Contract", "Limits"),
    ]
    strategy = compute_embedding_strategy_id(
        model="test", dimensions=8, tokenizer="fixture"
    )
    chunks = Chunker(
        max_tokens=12,
        hard_max_tokens=16,
        tokenizer=ApproximateTokenizer(),
        strategy_id=strategy,
    ).chunk_document(parsed.path, parsed.sections)
    assert chunks
    assert all(chunk.token_count <= 16 for chunk in chunks)
    assert all("passage:" not in chunk.embedding_input for chunk in chunks)
    assert "word" in "\n".join(chunk.text for chunk in chunks)


def test_chunker_probes_backend_token_count_before_approximate_fallback() -> None:
    class _InflatedTokenizer:
        def count_tokens(self, value: str) -> int:
            return ApproximateTokenizer().count(value) * 2

    text = "word " * 240
    parsed = parse_text("guide.md", "# Contract\n" + text)
    embedding_input = "Contract\n\n" + text
    assert ApproximateTokenizer().count(embedding_input) < 480
    tokenizer = _InflatedTokenizer()
    chunks = Chunker(
        max_tokens=400, hard_max_tokens=480, tokenizer=tokenizer
    ).chunk_document(parsed.path, parsed.sections)

    assert len(chunks) > 1
    assert all(tokenizer.count_tokens(chunk.embedding_input) <= 480 for chunk in chunks)


def test_corrected_token_count_dispatch_changes_strategy_id() -> None:
    old_strategy = compute_embedding_strategy_id(
        model="test",
        dimensions=8,
        tokenizer="fixture",
        chunking_version="v1-recursive-lines-paragraphs",
    )
    corrected_strategy = compute_embedding_strategy_id(
        model="test", dimensions=8, tokenizer="fixture"
    )

    assert Manifest("/tmp/project").chunking_version != "v1-recursive-lines-paragraphs"
    assert corrected_strategy != old_strategy
