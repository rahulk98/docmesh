from docmesh.chunking import (
    ApproximateTokenizer,
    Chunker,
    compute_embedding_strategy_id,
    is_low_signal_chunk,
)
from docmesh.models import Manifest
from docmesh.parsing import parse_file, parse_text
from docmesh.models import UnsupportedDocumentError


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


def test_approximate_tokenizer_never_undercounts_an_unbroken_run() -> None:
    # A 500KB line with no whitespace matches the \w+ regex as one token; the
    # estimator must fall back to a chars/4 floor so it is never treated as
    # cheap by the chunker's fit checks (F-parse-3).
    run = "x" * 500_000
    assert ApproximateTokenizer().count(run) >= len(run) // 4


def test_chunker_hard_splits_a_whitespace_free_run_under_the_hard_limit() -> None:
    text = "x" * 500_000
    parsed = parse_text("longline.txt", text, "text")
    chunks = Chunker(max_tokens=400, hard_max_tokens=480).chunk_document(
        parsed.path, parsed.sections
    )
    assert len(chunks) > 1
    assert all(chunk.token_count <= 480 for chunk in chunks)
    assert all(
        ApproximateTokenizer().count(chunk.embedding_input) <= 480 for chunk in chunks
    )
    # Line/column mapping stays intact even though the source line was split.
    assert all(chunk.start_line == 1 and chunk.end_line == 1 for chunk in chunks)
    assert "".join(chunk.text for chunk in chunks) == text


def test_is_low_signal_chunk_flags_bare_axis_labels() -> None:
    # A PDF figure chunk made of axis tick labels: few real words, mostly
    # digits/punctuation (F-search-2).
    assert is_low_signal_chunk("0.1 0.2 0.3 0.4 0.5\n1e-3 1e-2 1e-1")
    assert not is_low_signal_chunk(
        "This section explains the background of the method in detail."
    )


def test_parse_file_strips_utf8_bom(tmp_path) -> None:
    path = tmp_path / "bom.md"
    path.write_bytes("﻿# Title\nBody text.".encode("utf-8"))
    parsed = parse_file(path)
    assert parsed.sections[0].breadcrumb == ("Title",)
    assert not parsed.text.startswith("﻿")


def test_parse_file_warns_on_invalid_utf8(tmp_path) -> None:
    path = tmp_path / "latin1.txt"
    path.write_bytes("café \xe9\xe8".encode("latin1"))
    parsed = parse_file(path)
    assert parsed.warning == "invalid UTF-8, decoded with replacement"
    assert parsed.text  # still indexed


def test_parse_file_rejects_binary_content(tmp_path) -> None:
    path = tmp_path / "binary.md"
    path.write_bytes(bytes(range(256)) * 32)
    try:
        parse_file(path)
    except UnsupportedDocumentError:
        pass
    else:
        raise AssertionError("binary content should be rejected")
