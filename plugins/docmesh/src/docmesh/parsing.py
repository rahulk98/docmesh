"""Small, source-faithful parsers for the V1 document formats."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import canonical_path, source_format
from .models import Section, UnsupportedDocumentError


@dataclass
class ParsedDocument:
    path: str
    format: str
    text: str
    sections: list[Section] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)
    file_hash: str = ""
    role: str = "editable"

    @property
    def revision_hash(self) -> str:
        return self.file_hash

    @property
    def is_pdf(self) -> bool:
        return self.format == "pdf"


_MARKDOWN_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_SETEXT = re.compile(r"^[ \t]*(=+|-+)[ \t]*$")
_LATEX_HEADING = re.compile(
    r"^[ \t]*\\(part|chapter|section|subsection|subsubsection|paragraph|subparagraph)\*?\s*\{([^{}]*)\}"
)
_BIB_ENTRY = re.compile(
    r"^[ \t]*@([A-Za-z][A-Za-z0-9_-]*)\s*[\[{]\s*([^,\s}\]]+)", re.MULTILINE
)


def _markdown_sections(text: str, fmt: str) -> list[Section]:
    lines = text.splitlines()
    if not lines:
        return [Section((), "", 1, 1, fmt)]
    headings: list[tuple[int, int, str]] = []
    fenced = False
    index = 0
    while index < len(lines):
        line = lines[index]
        if re.match(r"^[ \t]*(```|~~~)", line):
            fenced = not fenced
            index += 1
            continue
        if not fenced:
            match = _MARKDOWN_HEADING.match(line)
            if match:
                headings.append((index, len(match.group(1)), match.group(2).strip()))
            elif (
                index + 1 < len(lines)
                and lines[index].strip()
                and _SETEXT.match(lines[index + 1])
                and (index == 0 or not lines[index - 1].strip())
            ):
                level = 1 if lines[index + 1].lstrip().startswith("=") else 2
                headings.append((index, level, lines[index].strip()))
                # The underline is part of the section source span, so it is
                # intentionally not skipped here.
        index += 1
    if not headings:
        return [Section((), text, 1, len(lines), fmt)]

    sections: list[Section] = []
    first_heading = headings[0][0]
    if first_heading > 0 and any(line.strip() for line in lines[:first_heading]):
        sections.append(
            Section((), "\n".join(lines[:first_heading]), 1, first_heading, fmt)
        )
    stack: list[tuple[int, str]] = []
    for pos, (start, level, title) in enumerate(headings):
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        end = headings[pos + 1][0] if pos + 1 < len(headings) else len(lines)
        sections.append(
            Section(
                tuple(item[1] for item in stack),
                "\n".join(lines[start:end]),
                start + 1,
                end,
                fmt,
                heading_line=start + 1,
            )
        )
    return sections


def _structured_sections(
    text: str, fmt: str, pattern: re.Pattern[str]
) -> list[Section]:
    lines = text.splitlines()
    matches = list(pattern.finditer(text))
    if not matches:
        return [Section((), text, 1, max(1, len(lines)), fmt)]
    starts: list[int] = []
    for match in matches:
        starts.append(text.count("\n", 0, match.start()) + 1)
    sections: list[Section] = []
    if starts[0] > 1:
        sections.append(
            Section((), "\n".join(lines[: starts[0] - 1]), 1, starts[0] - 1, fmt)
        )
    # LaTeX commands have a natural hierarchy. Preserve it in breadcrumbs so
    # subsection locations are actionable in the same way as Markdown.
    ranks = {
        "part": 0,
        "chapter": 1,
        "section": 2,
        "subsection": 3,
        "subsubsection": 4,
        "paragraph": 5,
        "subparagraph": 6,
    }
    stack: list[tuple[int, str]] = []
    for index, match in enumerate(matches):
        start_line = starts[index]
        end_line = (
            starts[index + 1] - 1
            if index + 1 < len(starts)
            else max(start_line, len(lines))
        )
        title = (
            match.group(2).strip()
            if match.lastindex and match.lastindex >= 2
            else match.group(0).strip()
        )
        level = ranks.get(match.group(1).lower(), len(stack))
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        sections.append(
            Section(
                tuple(item[1] for item in stack),
                "\n".join(lines[start_line - 1 : end_line]),
                start_line,
                end_line,
                fmt,
                heading_line=start_line,
            )
        )
    return sections


def _bibtex_sections(text: str) -> list[Section]:
    lines = text.splitlines()
    matches = list(_BIB_ENTRY.finditer(text))
    if not matches:
        return [Section((), text, 1, max(1, len(lines)), "bibtex")]
    starts = [text.count("\n", 0, match.start()) + 1 for match in matches]
    sections: list[Section] = []
    if starts[0] > 1:
        sections.append(
            Section((), "\n".join(lines[: starts[0] - 1]), 1, starts[0] - 1, "bibtex")
        )
    for index, match in enumerate(matches):
        start = starts[index]
        end = (
            starts[index + 1] - 1 if index + 1 < len(starts) else max(start, len(lines))
        )
        title = f"{match.group(1)}: {match.group(2)}"
        sections.append(
            Section(
                (title,),
                "\n".join(lines[start - 1 : end]),
                start,
                end,
                "bibtex",
                heading_line=start,
            )
        )
    return sections


def parse_text(path: str, text: str, fmt: str | None = None) -> ParsedDocument:
    """Parse text while preserving line-oriented source spans."""

    fmt = fmt or source_format(path) or "text"
    if fmt in ("markdown", "mdx"):
        sections = _markdown_sections(text, fmt)
    elif fmt == "latex":
        sections = _structured_sections(text, fmt, _LATEX_HEADING)
    elif fmt == "bibtex":
        sections = _bibtex_sections(text)
    else:
        lines = text.splitlines()
        sections = [Section((), text, 1, max(1, len(lines)), fmt)]
    return ParsedDocument(canonical_path(path), fmt, text, sections)


def _parse_pdf(path: Path) -> ParsedDocument:
    try:
        from pypdf import PdfReader
    except (
        ImportError
    ) as exc:  # pragma: no cover - dependency is optional at import time
        raise UnsupportedDocumentError(
            "PDF support requires the pypdf dependency"
        ) from exc
    try:
        reader = PdfReader(str(path))
        pages = [(page.extract_text() or "") for page in reader.pages]
    except Exception as exc:
        raise UnsupportedDocumentError(
            f"could not extract text from PDF {path}: {exc}"
        ) from exc
    sections: list[Section] = []
    for page_number, page_text in enumerate(pages, start=1):
        page_lines = page_text.splitlines()
        sections.append(
            Section(
                (f"Page {page_number}",),
                page_text,
                1,
                max(1, len(page_lines)),
                "pdf",
                page=page_number,
            )
        )
    return ParsedDocument(
        canonical_path(path), "pdf", "\n\n".join(pages), sections, pages
    )


def parse_file(path: str | Path) -> ParsedDocument:
    """Read and parse one supported file."""

    path_obj = Path(path).expanduser().resolve(strict=False)
    fmt = source_format(path_obj)
    if fmt is None:
        raise UnsupportedDocumentError(
            f"unsupported document format: {path_obj.suffix}"
        )
    data = path_obj.read_bytes()
    file_hash = hashlib.sha256(data).hexdigest()
    if fmt == "pdf":
        parsed = _parse_pdf(path_obj)
    else:
        text = data.decode("utf-8", errors="replace")
        parsed = parse_text(str(path_obj), text, fmt)
    parsed.file_hash = file_hash
    return parsed


def span_text(text: str, start_line: int, end_line: int) -> str:
    lines = text.splitlines()
    if start_line < 1 or end_line < start_line or end_line > max(1, len(lines)):
        raise ValueError("invalid one-based line span")
    return "\n".join(lines[start_line - 1 : end_line])


def line_at(text: str, line_number: int) -> str:
    lines = text.splitlines()
    if line_number < 1 or line_number > max(1, len(lines)):
        raise ValueError("line is outside source")
    return lines[line_number - 1] if lines else ""


class DocumentParser:
    """Reusable parser facade for callers that manage source bytes themselves."""

    def parse(self, path: str | Path, text: str | None = None) -> ParsedDocument:
        if text is None:
            return parse_file(path)
        return parse_text(str(path), text)


Parser = DocumentParser


# Public aliases for callers that prefer parser terminology.
parse_document = parse_file
