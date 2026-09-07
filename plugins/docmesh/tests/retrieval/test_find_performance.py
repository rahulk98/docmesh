"""F-find-1/F-find-2: find() must not re-extract PDFs at query time, must
report structured errors for invalid regex, must stay fast on large match
counts, and must stay correct after a source is edited post-index."""

from __future__ import annotations

import io
import time
from pathlib import Path

import pytest

from docmesh.embeddings import DeterministicEmbedder
from docmesh.index import Indexer, SQLiteIndex
from docmesh.models import StaleSourceError, ValidationError
from docmesh.retrieval import RetrievalService


def _make_pdf(text: str) -> bytes:
    """A minimal, real, extractable single-page PDF (no external deps)."""
    objs = [
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj",
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj",
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
        b"/Resources<</Font<</F1 5 0 R>>>>/Contents 4 0 R>>endobj",
    ]
    content = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET".encode("latin1")
    objs.append(
        b"4 0 obj<</Length "
        + str(len(content)).encode()
        + b">>stream\n"
        + content
        + b"\nendstream endobj"
    )
    objs.append(b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for obj in objs:
        offsets.append(out.tell())
        out.write(obj + b"\n")
    xref_start = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(
        f"trailer<</Size {len(objs) + 1}/Root 1 0 R>>\nstartxref\n{xref_start}\n%%EOF".encode()
    )
    return out.getvalue()


def _service(tmp_path: Path) -> RetrievalService:
    indexer = Indexer(
        tmp_path, index=SQLiteIndex(":memory:"), embedder=DeterministicEmbedder(8)
    )
    indexer.index()
    return RetrievalService(indexer)


def test_find_does_not_re_extract_an_unchanged_pdf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "note.md").write_text("plain text file\n", encoding="utf-8")
    (tmp_path / "paper.pdf").write_bytes(_make_pdf("NEEDLE_PDF hello world"))
    service = _service(tmp_path)

    calls = {"count": 0}
    import pypdf

    original = pypdf.PdfReader.pages.fget

    def spy(self):  # pragma: no cover - trivial counter
        calls["count"] += 1
        return original(self)

    monkeypatch.setattr(pypdf.PdfReader, "pages", property(spy))

    results = service.find("NEEDLE_PDF", mode="literal")
    assert len(results) == 1
    assert results[0].location.format == "markdown"
    assert results[0].location.generated_from == str((tmp_path / "paper.pdf").resolve())
    assert calls["count"] == 0, "find() must not re-parse an unchanged PDF"


def test_find_rejects_invalid_regex_with_a_structured_error(tmp_path: Path) -> None:
    (tmp_path / "note.md").write_text("hello\n", encoding="utf-8")
    service = _service(tmp_path)
    with pytest.raises(ValidationError):
        service.find("[unclosed", mode="regex")


def test_find_stays_fast_with_thousands_of_matches(tmp_path: Path) -> None:
    (tmp_path / "huge.md").write_text("NEEDLE line\n" * 5000, encoding="utf-8")
    service = _service(tmp_path)
    start = time.monotonic()
    results = service.find("NEEDLE", mode="literal")
    elapsed = time.monotonic() - start
    assert len(results) == 5000
    assert elapsed < 2.0, f"find() took {elapsed:.2f}s for 5000 matches"


def test_find_does_not_reparse_a_skipped_document_on_repeat_queries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "note.md").write_text("hello\n", encoding="utf-8")
    (tmp_path / "broken.pdf").write_bytes(b"%PDF-1.4 not a real pdf")
    service = _service(tmp_path)  # initial index already tried + skipped it
    assert service.store.document(str(tmp_path / "broken.pdf")) is None

    import importlib

    index_module = importlib.import_module("docmesh.index")
    calls = {"count": 0}
    original = index_module.parse_file

    def spy(*args, **kwargs):
        calls["count"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(index_module, "parse_file", spy)

    service.find("hello", mode="literal")
    service.find("hello", mode="literal")

    assert calls["count"] == 0, "an unchanged skipped document must not be reparsed"


def test_validate_location_never_reparses_an_unchanged_pdf_mirror_but_flags_content_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(_make_pdf("NEEDLE_PDF hello world"))
    service = _service(tmp_path)
    results = service.search("NEEDLE_PDF", limit=1)
    assert results
    location = results[0].location
    assert location.format == "markdown"

    import pypdf

    calls = {"count": 0}
    original_pages = pypdf.PdfReader.pages.fget

    def spy(self):  # pragma: no cover - trivial counter
        calls["count"] += 1
        return original_pages(self)

    monkeypatch.setattr(pypdf.PdfReader, "pages", property(spy))

    # Unchanged: a mirror is an ordinary text file; validating it never
    # touches pypdf again.
    validated = service.validate_location(location)
    assert validated.span_hash == location.span_hash
    assert calls["count"] == 0

    # The PDF changes; the mirror is only refreshed on the next index() (a
    # query alone does not re-extract PDFs), so the stored location goes
    # stale once that reindex updates the mirror's content.
    pdf_path.write_bytes(_make_pdf("DIFFERENT_TEXT now"))
    service.indexer.index()
    with pytest.raises(StaleSourceError):
        service.validate_location(location)


def test_find_reflects_edits_made_after_indexing(tmp_path: Path) -> None:
    source = tmp_path / "doc.md"
    source.write_text("original content here\n", encoding="utf-8")
    service = _service(tmp_path)
    assert service.find("original", mode="literal")

    time.sleep(0.05)
    source.write_text("updated content here\n", encoding="utf-8")

    assert service.find("original", mode="literal") == []
    updated = service.find("updated", mode="literal")
    assert len(updated) == 1
    assert updated[0].location.snippet.startswith("updated content")
