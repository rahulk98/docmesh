"""Small, source-faithful parsers for the V1 document formats."""

from __future__ import annotations

import gc
import hashlib
import io
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
    warning: str | None = None
    generated_from: str | None = None

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


def _parse_pdf(path: Path, data: bytes | None = None) -> ParsedDocument:
    try:
        from pypdf import PdfReader
    except (
        ImportError
    ) as exc:  # pragma: no cover - dependency is optional at import time
        raise UnsupportedDocumentError(
            "PDF support requires the pypdf dependency"
        ) from exc
    try:
        stream = io.BytesIO(data) if data is not None else str(path)
        reader = PdfReader(stream)
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


_MIRROR_FORMAT_VERSION = 2

_MIRROR_HEADER = re.compile(
    r"^<!-- docmesh mirror of (?P<rel>.*) sha256=(?P<sha>[0-9a-f]{64}) "
    r"pages=(?P<pages>\d+)(?: format=(?P<format>\d+))? -->$"
)
_HYPHEN_JOIN = re.compile(r"(\w+)-$")
_BLANK_RUN = re.compile(r"\n{3,}")
# Control characters pypdf can leak into extracted text: NUL and other C0
# controls (kept: \t, \n) are stripped outright; FF/VT are folded to a
# newline instead of vanishing so page breaks don't glue words together.
_CONTROL_STRIP = re.compile(r"[\x00-\x08\x0e-\x1f\x7f]")
_CONTROL_TO_NEWLINE = re.compile(r"[\x0b\x0c]")


def _strip_control_chars(text: str) -> str:
    return _CONTROL_STRIP.sub("", _CONTROL_TO_NEWLINE.sub("\n", text))


def mirror_header_hash(mirror_path: Path) -> str | None:
    """Return the sha256 recorded in an existing mirror's header, if any."""

    match = _read_mirror_header(mirror_path)
    return match.group("sha") if match else None


def _read_mirror_header(mirror_path: Path):
    try:
        with mirror_path.open("r", encoding="utf-8") as handle:
            first_line = handle.readline().rstrip("\n")
    except OSError:
        return None
    return _MIRROR_HEADER.match(first_line)


def _mirror_format_current(mirror_path: Path) -> bool:
    """True only if the mirror's recorded format version is current."""

    match = _read_mirror_header(mirror_path)
    if match is None:
        return False
    recorded = match.group("format")
    return recorded is not None and int(recorded) == _MIRROR_FORMAT_VERSION


def _dehyphenate_page(page_text: str) -> str:
    """Join a line-end hyphenated word with the start of the next line.

    ``compres-\\nsion`` becomes ``compression`` only when the joined word is
    lowercase alphabetic; anything else is left untouched.
    """

    lines = page_text.split("\n")
    out: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        match = _HYPHEN_JOIN.search(line)
        if match and index + 1 < len(lines):
            next_line = lines[index + 1]
            tail_match = re.match(r"(\w+)", next_line)
            if tail_match:
                joined = match.group(1) + tail_match.group(1)
                if joined.isalpha() and joined.islower():
                    merged = (
                        line[: match.start()]
                        + joined
                        + next_line[tail_match.end() :]
                    )
                    out.append(merged)
                    index += 2
                    continue
        out.append(line)
        index += 1
    return "\n".join(out)


def _clean_page_text(page_text: str) -> str:
    joined = _dehyphenate_page(page_text)
    return _BLANK_RUN.sub("\n\n", joined)


def write_pdf_mirror(
    pdf_path: Path, mirror_path: Path, data: bytes, *, rel_path: str | None = None
) -> bool:
    """Write (or refresh) the Markdown mirror for a PDF source.

    Returns ``True`` if the mirror was (re)written, ``False`` when the PDF's
    current sha256 matches the hash already recorded in the mirror's header
    (the mirror file is then left untouched, mtime included). Extraction and
    writing stay page-by-page: at most one page's text is held beyond what
    pypdf itself holds, and the mirror is written to a temporary file first
    so a mid-extraction failure never leaves a partial mirror in place.
    """

    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise UnsupportedDocumentError(
            "PDF support requires the pypdf dependency"
        ) from exc

    file_hash = hashlib.sha256(data).hexdigest()
    if (
        mirror_path.is_file()
        and mirror_header_hash(mirror_path) == file_hash
        and _mirror_format_current(mirror_path)
    ):
        return False

    rel = rel_path if rel_path is not None else pdf_path.name

    try:
        reader = PdfReader(io.BytesIO(data))
        page_count = len(reader.pages)
    except Exception as exc:
        raise UnsupportedDocumentError(
            f"could not extract text from PDF {pdf_path}: {exc}"
        ) from exc

    mirror_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = mirror_path.with_suffix(mirror_path.suffix + ".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(
                f"<!-- docmesh mirror of {rel} sha256={file_hash} "
                f"pages={page_count} format={_MIRROR_FORMAT_VERSION} -->\n"
            )
            for page_number in range(page_count):
                try:
                    page_text = reader.pages[page_number].extract_text() or ""
                except Exception as exc:
                    raise UnsupportedDocumentError(
                        f"could not extract text from PDF {pdf_path}: {exc}"
                    ) from exc
                handle.write(f"\n## Page {page_number + 1}\n\n")
                handle.write(_strip_control_chars(_clean_page_text(page_text)))
                handle.write("\n")
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    finally:
        # pypdf's parsed objects (pages, form XObjects, fonts) reference the
        # reader and each other, so refcounting alone can't reclaim them;
        # without an explicit collection here the cycles pile up across a
        # multi-PDF indexing run until Python's own gc thresholds catch up,
        # which is what drove multi-GB peak RSS on large corpora.
        del reader
        gc.collect()
    tmp_path.replace(mirror_path)
    return True


def parse_file(
    path: str | Path, *, data: bytes | None = None
) -> ParsedDocument:
    """Read and parse one supported file.

    ``data`` lets callers that already read the file (for hashing, say)
    avoid re-reading it; PDFs then parse from the in-memory copy too.
    """

    path_obj = Path(path).expanduser().resolve(strict=False)
    fmt = source_format(path_obj)
    if fmt is None:
        raise UnsupportedDocumentError(
            f"unsupported document format: {path_obj.suffix}"
        )
    if data is None:
        data = path_obj.read_bytes()
    file_hash = hashlib.sha256(data).hexdigest()
    if fmt != "pdf" and b"\x00" in data[:8192]:
        # A NUL byte in the first 8KB is grep's own binary heuristic; a binary
        # file decoded as text produces unusable garbage chunks.
        raise UnsupportedDocumentError(f"binary file, not indexed: {path_obj}")
    if fmt == "pdf":
        parsed = _parse_pdf(path_obj, data)
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="replace")
            parsed = parse_text(str(path_obj), text, fmt)
            parsed.warning = "invalid UTF-8, decoded with replacement"
            parsed.file_hash = file_hash
            return parsed
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
