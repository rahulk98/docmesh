"""PDFs are indexed only via a generated Markdown mirror (docs/design.md).

Covers: mirror header/page-heading/dehyphenation format, skip-when-unchanged
(mtime preserved), removal cascade, page-based reads mapping to mirror
lines, and that PDF-derived chunks carry the mirror path, generated_from,
page, and real line numbers.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from docmesh.embeddings import DeterministicEmbedder
from docmesh.index import Indexer, SQLiteIndex
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


class _FakePage:
    def __init__(self, text: str) -> None:
        self._text = text

    def extract_text(self) -> str:
        return self._text


class _FakeReader:
    def __init__(self, stream: object) -> None:
        self.pages = [_FakePage(page) for page in _FakeReader.PAGE_TEXTS]


def _patch_multipage_pdf(monkeypatch: pytest.MonkeyPatch, page_texts: list[str]) -> None:
    import pypdf

    _FakeReader.PAGE_TEXTS = page_texts
    monkeypatch.setattr(pypdf, "PdfReader", _FakeReader)


def _mirror_path(root: Path, rel: str) -> Path:
    return root / ".docmesh" / "mirrors" / (rel + ".md")


def _indexer(tmp_path: Path) -> Indexer:
    indexer = Indexer(
        tmp_path, index=SQLiteIndex(":memory:"), embedder=DeterministicEmbedder(16)
    )
    return indexer


def test_mirror_header_page_headings_and_dehyphenation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "paper.pdf").write_bytes(b"%PDF-1.4 stub")
    _patch_multipage_pdf(
        monkeypatch,
        [
            "This word is compres-\nsion in full.",
            "Second page text\n\n\n\nwith extra blank runs.",
        ],
    )
    indexer = _indexer(tmp_path)
    indexer.index()

    mirror = _mirror_path(tmp_path, "paper.pdf")
    assert mirror.is_file()
    lines = mirror.read_text(encoding="utf-8").splitlines()
    pdf_hash = __import__("hashlib").sha256(
        (tmp_path / "paper.pdf").read_bytes()
    ).hexdigest()
    assert lines[0] == (
        f"<!-- docmesh mirror of paper.pdf sha256={pdf_hash} pages=2 format=2 -->"
    )
    text = mirror.read_text(encoding="utf-8")
    assert "## Page 1" in text
    assert "## Page 2" in text
    assert "compression" in text
    assert "compres-\nsion" not in text
    assert "\n\n\n" not in text


def test_unchanged_pdf_does_not_rewrite_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "paper.pdf").write_bytes(b"%PDF-1.4 stub")
    _patch_multipage_pdf(monkeypatch, ["Only page text."])
    indexer = _indexer(tmp_path)
    indexer.index()
    mirror = _mirror_path(tmp_path, "paper.pdf")
    first_mtime = mirror.stat().st_mtime_ns

    indexer.index()

    assert mirror.stat().st_mtime_ns == first_mtime


def test_removing_pdf_removes_mirror_file_and_index_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 stub")
    _patch_multipage_pdf(monkeypatch, ["NEEDLE content here."])
    indexer = _indexer(tmp_path)
    indexer.index()
    mirror = _mirror_path(tmp_path, "paper.pdf")
    assert mirror.is_file()
    assert indexer.store.mirror_for(str(pdf_path)) is not None

    pdf_path.unlink()
    indexer.index()

    assert not mirror.exists()
    assert indexer.store.mirror_for(str(pdf_path)) is None
    assert indexer.store.document(str(pdf_path)) is None


def test_read_by_page_maps_to_mirror_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "paper.pdf").write_bytes(b"%PDF-1.4 stub")
    _patch_multipage_pdf(monkeypatch, ["First page body.", "Second page body."])
    indexer = _indexer(tmp_path)
    indexer.index()
    service = RetrievalService(indexer)

    result = service.read(str(tmp_path / "paper.pdf"), page=2)
    assert result.format == "markdown"
    assert result.generated_from == str((tmp_path / "paper.pdf").resolve())
    assert "Second page body." in result.content
    assert result.path.endswith("paper.pdf.md")

    mirror_lines = Path(result.path).read_text(encoding="utf-8").splitlines()
    assert mirror_lines[result.start_line - 1] == "## Page 2"


def test_control_chars_from_pypdf_are_stripped_from_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "paper.pdf").write_bytes(b"%PDF-1.4 stub")
    _patch_multipage_pdf(
        monkeypatch, ["Has\x00NUL and\x0bvtab and\x0cformfeed here."]
    )
    indexer = _indexer(tmp_path)
    indexer.index()

    mirror = _mirror_path(tmp_path, "paper.pdf")
    data = mirror.read_bytes()
    assert b"\x00" not in data
    text = mirror.read_text(encoding="utf-8")
    assert "HasNUL and\nvtab and\nformfeed here." in text


def test_missing_mirror_is_rebuilt_on_plain_reindex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "paper.pdf").write_bytes(b"%PDF-1.4 stub")
    _patch_multipage_pdf(monkeypatch, ["Only page text."])
    indexer = _indexer(tmp_path)
    indexer.index()
    mirror = _mirror_path(tmp_path, "paper.pdf")
    assert mirror.is_file()

    mirror.unlink()
    indexer.index()

    assert mirror.is_file()
    assert "Only page text." in mirror.read_text(encoding="utf-8")


def test_read_pdf_by_line_range_resolves_to_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "paper.pdf").write_bytes(b"%PDF-1.4 stub")
    _patch_multipage_pdf(monkeypatch, ["First page body."])
    indexer = _indexer(tmp_path)
    indexer.index()
    service = RetrievalService(indexer)
    mirror = _mirror_path(tmp_path, "paper.pdf")
    mirror_lines = mirror.read_text(encoding="utf-8").splitlines()

    result = service.read(str(tmp_path / "paper.pdf"), start_line=1, end_line=2)
    assert result.path.endswith("paper.pdf.md")
    assert result.content == "\n".join(mirror_lines[0:2])
    assert result.start_line == 1
    assert result.end_line == 2

    direct = service.read(str(mirror), start_line=1, end_line=2)
    assert direct.content == result.content


def test_pdf_chunks_carry_mirror_path_generated_from_page_and_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "paper.pdf").write_bytes(b"%PDF-1.4 stub")
    _patch_multipage_pdf(monkeypatch, ["NEEDLE_TOKEN appears here."])
    indexer = _indexer(tmp_path)
    indexer.index()
    service = RetrievalService(indexer)

    results = service.search("NEEDLE_TOKEN", limit=5)
    assert results
    location = results[0].location
    assert location.path.endswith("paper.pdf.md")
    assert location.generated_from == str((tmp_path / "paper.pdf").resolve())
    assert location.page == 1
    assert location.start_line is not None and location.end_line is not None

    matches = service.find("NEEDLE_TOKEN", mode="literal")
    assert len(matches) == 1
    assert matches[0].location.page == 1
    assert matches[0].location.generated_from == str(
        (tmp_path / "paper.pdf").resolve()
    )

    # The PDF itself carries no chunks; only the mirror does.
    assert indexer.store.chunks(str(tmp_path / "paper.pdf")) == []
    mirror_row = indexer.store.mirror_for(str(tmp_path / "paper.pdf"))
    assert mirror_row is not None
    assert indexer.store.chunks(str(mirror_row["path"]))
