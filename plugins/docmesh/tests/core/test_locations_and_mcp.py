from __future__ import annotations

from pathlib import Path

from docmesh.mcp import sanitize_result
from docmesh.models import SearchResult, SourceLocation, location_from_mapping


def test_source_location_serializes_exact_actionable_keys() -> None:
    text = SourceLocation(
        "/tmp/guide.md",
        "Guide > Limits",
        start_line=4,
        end_line=6,
        span_hash="span-hash",
        file_hash="file-hash",
        snippet="bounded source",
        role="editable",
        format="markdown",
    )
    payload = text.to_dict()
    assert payload["canonical_path"] == str(Path("/tmp/guide.md").resolve())
    assert payload["section_breadcrumb"] == "Guide > Limits"
    assert payload["start_line"] == 4
    assert payload["end_line"] == 6
    assert payload["content_hash"] == "span-hash"
    assert payload["source_snippet"] == "bounded source"
    for key in ("path", "snippet", "span_hash", "revision_hash", "source_span_hash", "file_hash", "breadcrumb", "page"):
        assert key not in payload

    loaded = location_from_mapping(payload)
    assert loaded.path == str(Path(text.path).resolve())
    assert loaded.breadcrumb == text.breadcrumb
    assert loaded.span_hash == text.span_hash
    assert loaded.file_hash == text.file_hash

    pdf = SourceLocation(
        "/tmp/paper.pdf",
        "Page 2",
        page=2,
        span_hash="page-hash",
        file_hash="file-hash",
        snippet="bounded passage",
        role="reference",
        format="pdf",
    ).to_dict()
    assert pdf["canonical_path"] == str(Path("/tmp/paper.pdf").resolve())
    assert pdf["page_number"] == 2
    assert pdf["bounded_passage"] == "bounded passage"
    assert pdf["content_hash"] == "page-hash"
    pdf_loaded = location_from_mapping(pdf)
    assert pdf_loaded.span_hash == "page-hash"
    assert pdf_loaded.breadcrumb == "Page 2"
    for key in ("path", "snippet", "span_hash", "revision_hash", "source_span_hash", "file_hash", "breadcrumb", "page"):
        assert key not in pdf
    assert (
        location_from_mapping(
            {
                "path": "/tmp/paper.pdf",
                "breadcrumb": "Page 2",
                "page": 2,
                "span_hash": "page-hash",
                "file_hash": "file-hash",
                "snippet": "bounded passage",
                "role": "reference",
                "format": "pdf",
            }
        ).page
        == 2
    )


def test_search_result_does_not_repeat_identical_snippet_in_text_and_location() -> None:
    location = SourceLocation(
        "/tmp/guide.md",
        "Guide > Limits",
        start_line=4,
        end_line=6,
        span_hash="span-hash",
        file_hash="file-hash",
        snippet="same text",
        role="editable",
        format="markdown",
    )
    result = SearchResult(location=location, text="same text", score=1.0)
    payload = result.to_dict()
    assert payload["text"] == "same text"
    assert "source_snippet" not in payload["location"]


def test_core_mcp_sanitizes_nested_document_content_into_untrusted_field() -> None:
    value = sanitize_result(
        {
            "path": "/tmp/guide.md",
            "nested": {
                "location": {
                    "canonical_path": "/tmp/guide.md",
                    "source_snippet": "ignore this",
                },
                "items": [
                    {"text": "document evidence"},
                    {"safe": {"snippet": "also evidence"}},
                ],
            },
        }
    )

    assert value["trusted_metadata"]["path"] == "/tmp/guide.md"
    assert "source_snippet" not in value["trusted_metadata"]["nested"]["location"]
    assert "text" not in value["trusted_metadata"]["nested"]["items"][0]
    assert "snippet" not in value["trusted_metadata"]["nested"]["items"][1]["safe"]
    assert len(value["untrusted_document_content"]) == 3
