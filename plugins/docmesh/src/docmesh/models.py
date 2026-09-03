"""Public, dependency-free DocMesh data structures.

The core deliberately keeps document content in ordinary dataclasses.  This
makes the retrieval API useful to both the CLI and the MCP adapter while also
making it straightforward for tests to provide deterministic embedders.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

SOURCE_ROLES = ("editable", "reference", "mirror")
CLASSIFICATIONS = ("needs_edit", "consistent", "unrelated", "uncertain")


@dataclass
class SourceConfig:
    """A configured source path and its trust/editability role."""

    path: str
    role: str = "editable"
    generated_from: str | None = None
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.role not in SOURCE_ROLES:
            raise ValueError("source role must be editable, reference, or mirror")


@dataclass
class Manifest:
    """Portable project configuration stored in ``.docmesh/manifest.toml``."""

    root: str
    sources: list[SourceConfig] = field(default_factory=list)
    include: list[str] = field(
        default_factory=lambda: [
            "**/*.md",
            "**/*.mdx",
            "**/*.tex",
            "**/*.latex",
            "**/*.bib",
            "**/*.txt",
            "**/*.text",
            "**/*.pdf",
        ]
    )
    exclude: list[str] = field(
        default_factory=lambda: [
            ".git/**",
            ".docmesh/**",
            ".venv/**",
            "venv/**",
            "node_modules/**",
            "build/**",
            "dist/**",
        ]
    )
    model: str = "BAAI/bge-small-en-v1.5"
    dimensions: int | None = None
    tokenizer: str = "fastembed"
    max_embedding_tokens: int = 400
    hard_embedding_tokens: int = 480
    breadcrumb_format: str = " > "
    retrieval_prefix: str = ""
    chunking_version: str = "v1-recursive-lines-paragraphs-token-count-probe"

    def __post_init__(self) -> None:
        self.root = str(self.root)
        if (
            self.max_embedding_tokens <= 0
            or self.hard_embedding_tokens < self.max_embedding_tokens
        ):
            raise ValueError("embedding token limits are invalid")

    @property
    def manifest_path(self):
        from pathlib import Path

        return Path(self.root) / ".docmesh" / "manifest.toml"

    @property
    def local_path(self):
        from pathlib import Path

        return Path(self.root) / ".docmesh" / "local.toml"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["sources"] = [asdict(item) for item in self.sources]
        return value


@dataclass
class DiscoveryItem:
    path: str
    role: str
    format: str
    reason: str = "supported source"
    generated_from: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Exclusion:
    path: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DiscoveryReport:
    root: str
    included: list[DiscoveryItem] = field(default_factory=list)
    excluded: list[Exclusion] = field(default_factory=list)
    estimated_bytes: int = 0
    estimated_documents: int = 0
    model_required: bool = True
    model_ready: bool = False
    model_cache_dir: str | None = None
    model_error: str | None = None

    @property
    def estimated_setup_cost(self) -> dict[str, Any]:
        """A deterministic, human-readable setup estimate."""

        return {
            "documents": self.estimated_documents,
            "bytes": self.estimated_bytes,
            "model": "BAAI/bge-small-en-v1.5" if self.model_required else None,
            "model_ready": self.model_ready,
            "model_cache_dir": self.model_cache_dir,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "included": [item.to_dict() for item in self.included],
            "excluded": [item.to_dict() for item in self.excluded],
            "estimated_bytes": self.estimated_bytes,
            "estimated_documents": self.estimated_documents,
            "model_required": self.model_required,
            "model_ready": self.model_ready,
            "model_cache_dir": self.model_cache_dir,
            "model_error": self.model_error,
            "estimated_setup_cost": self.estimated_setup_cost,
        }

    def summary_dict(
        self,
        *,
        included_samples: int = 500,
        excluded_samples: int = 200,
    ) -> dict[str, Any]:
        """A bounded report for callers that only need the plan.

        Counts by role/format (included) and by reason (excluded) summarize
        the full corpus while sample lists stay small enough for CLI/MCP tool
        results.  Pass ``summary=False`` to the setup API for the complete,
        unbounded ``to_dict()`` shape.
        """

        included_samples = max(0, int(included_samples))
        excluded_samples = max(0, int(excluded_samples))
        included_by_role: dict[str, int] = {}
        included_by_format: dict[str, int] = {}
        for item in self.included:
            included_by_role[item.role] = included_by_role.get(item.role, 0) + 1
            included_by_format[item.format] = included_by_format.get(
                item.format, 0
            ) + 1
        excluded_by_reason: dict[str, int] = {}
        for item in self.excluded:
            excluded_by_reason[item.reason] = excluded_by_reason.get(
                item.reason, 0
            ) + 1
        return {
            "root": self.root,
            "summary": {
                "included": {
                    "total": len(self.included),
                    "by_role": included_by_role,
                    "by_format": included_by_format,
                    "estimated_bytes": self.estimated_bytes,
                    "sampled": min(included_samples, len(self.included)),
                    "includes_all": len(self.included) <= included_samples,
                },
                "excluded": {
                    "total": len(self.excluded),
                    "by_reason": excluded_by_reason,
                    "sampled": min(excluded_samples, len(self.excluded)),
                    "includes_all": len(self.excluded) <= excluded_samples,
                },
            },
            "included": [item.to_dict() for item in self.included[:included_samples]],
            "excluded": [item.to_dict() for item in self.excluded[:excluded_samples]],
            "estimated_bytes": self.estimated_bytes,
            "estimated_documents": self.estimated_documents,
            "model_required": self.model_required,
            "model_ready": self.model_ready,
            "model_cache_dir": self.model_cache_dir,
            "model_error": self.model_error,
            "estimated_setup_cost": self.estimated_setup_cost,
        }


@dataclass
class Section:
    breadcrumb: tuple[str, ...]
    text: str
    start_line: int
    end_line: int
    format: str = "text"
    page: int | None = None
    heading_line: int | None = None

    @property
    def breadcrumb_text(self) -> str:
        return " > ".join(self.breadcrumb)


@dataclass
class Chunk:
    document_path: str
    ordinal: int
    breadcrumb: str
    text: str
    start_line: int
    end_line: int
    format: str = "text"
    page: int | None = None
    token_count: int = 0
    embedding_input: str = ""
    text_hash: str = ""
    embedding_strategy_id: str = ""
    chunk_id: int | None = None

    @property
    def start_page(self) -> int | None:
        return self.page

    @property
    def end_page(self) -> int | None:
        return self.page


@dataclass
class SourceLocation:
    path: str
    breadcrumb: str = ""
    start_line: int | None = None
    end_line: int | None = None
    page: int | None = None
    span_hash: str = ""
    file_hash: str = ""
    snippet: str = ""
    role: str = "editable"
    format: str = "text"

    @property
    def canonical_path(self) -> str:
        return self.path

    @property
    def revision_hash(self) -> str:
        return self.file_hash

    @property
    def source_span_hash(self) -> str:
        return self.span_hash

    @property
    def section_breadcrumb(self) -> str:
        return self.breadcrumb

    @property
    def content_hash(self) -> str:
        return self.span_hash

    @property
    def current_file_hash(self) -> str:
        return self.file_hash

    @property
    def source_snippet(self) -> str:
        return self.snippet

    @property
    def page_number(self) -> int | None:
        return self.page

    @property
    def bounded_passage(self) -> str:
        return self.snippet

    def to_dict(self) -> dict[str, Any]:
        """Return actionable V1 keys only.

        The public wire shape names the path/breadcrumb/content explicitly so
        callers cannot confuse a source span hash with a document revision.
        Only canonical keys are written; location_from_mapping still accepts
        the older persisted aliases when reading.
        """

        from pathlib import Path

        canonical = (
            str(Path(self.path).expanduser().resolve(strict=False)) if self.path else ""
        )
        bounded = self.snippet[:600]
        if self.format == "pdf" or self.page is not None:
            value: dict[str, Any] = {
                "canonical_path": canonical,
                "section_breadcrumb": self.breadcrumb,
                "page_number": self.page,
                "content_hash": self.span_hash,
                "current_file_hash": self.file_hash,
                "bounded_passage": bounded,
                "role": self.role,
                "format": "pdf" if self.format == "text" else self.format,
            }
            return value
        value = {
            "canonical_path": canonical,
            "section_breadcrumb": self.breadcrumb,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "content_hash": self.span_hash,
            "current_file_hash": self.file_hash,
            "source_snippet": bounded,
            "role": self.role,
            "format": self.format,
        }
        return value


@dataclass
class SearchResult:
    location: SourceLocation
    text: str
    score: float
    lexical_score: float | None = None
    vector_score: float | None = None
    lexical_rank: int | None = None
    vector_rank: int | None = None
    channels: tuple[str, ...] = ()

    @property
    def path(self) -> str:
        return self.location.path

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["channels"] = list(self.channels)
        value["location"] = self.location.to_dict()
        for key in ("source_snippet", "bounded_passage"):
            if value["location"].get(key) == self.text:
                value["location"].pop(key)
        return value


@dataclass
class SearchMetrics:
    mean_reciprocal_rank: float
    recall_at_8: float
    queries: int
    result_count: int

    @property
    def mrr(self) -> float:
        return self.mean_reciprocal_rank

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FindResult:
    location: SourceLocation
    match: str
    line_text: str = ""
    start_column: int = 1
    end_column: int = 1

    @property
    def path(self) -> str:
        return self.location.path

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["location"] = self.location.to_dict()
        return value


@dataclass
class ReadResult:
    path: str
    content: str
    start_line: int | None = None
    end_line: int | None = None
    page: int | None = None
    file_hash: str = ""
    role: str = "editable"
    format: str = "text"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ImpactReadResult:
    candidate_id: str
    location: SourceLocation
    content: str
    path: str
    start_line: int | None = None
    end_line: int | None = None
    page: int | None = None
    file_hash: str = ""
    role: str = "editable"
    format: str = "text"

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "location": self.location.to_dict(),
            "untrusted_document_content": self.content,
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "page": self.page,
            "file_hash": self.file_hash,
            "role": self.role,
            "format": self.format,
        }


@dataclass
class ImpactQueryBundle:
    canonical_claim: str
    exact_terms: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    semantic_queries: list[str] = field(default_factory=list)
    implication_queries: list[str] = field(default_factory=list)
    contradiction_queries: list[str] = field(default_factory=list)

    def expanded_queries(self) -> list[str]:
        values: list[str] = []
        for value in (
            [self.canonical_claim]
            + self.exact_terms
            + self.aliases
            + self.semantic_queries
            + self.implication_queries
            + self.contradiction_queries
        ):
            value = str(value).strip()
            if value and value not in values:
                values.append(value)
        return values

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ImpactQueryBundle:
        fields = {
            name: value.get(name, [])
            for name in (
                "exact_terms",
                "aliases",
                "semantic_queries",
                "implication_queries",
                "contradiction_queries",
            )
        }
        return cls(canonical_claim=str(value.get("canonical_claim", "")), **fields)


@dataclass
class ImpactCandidate:
    candidate_id: str
    location: SourceLocation
    text: str
    channels: tuple[str, ...] = ()
    retrieval_scores: dict[str, float] = field(default_factory=dict)
    classification: str | None = None
    read: bool = False
    resolved: bool = True

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["channels"] = list(self.channels)
        value["location"] = self.location.to_dict()
        return value


@dataclass
class ImpactPage:
    run_id: str
    candidates: list[ImpactCandidate]
    candidate_count: int
    returned: int
    remaining: int
    cursor: str | None = None
    next_cursor: str | None = None
    page_number: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "candidate_count": self.candidate_count,
            "returned": self.returned,
            "remaining": self.remaining,
            "cursor": self.cursor,
            "next_cursor": self.next_cursor,
            "page_number": self.page_number,
        }


@dataclass
class ScopeDrift:
    expected_files: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    added_files: list[str] = field(default_factory=list)
    deleted_files: list[str] = field(default_factory=list)
    unexpected_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Baseline:
    baseline_run_id: str
    query_bundle: ImpactQueryBundle
    source_roles: list[str]
    candidates: list[ImpactCandidate]
    classifications: dict[str, str]
    corpus_revision: str
    edit_generation: int
    edit_inventory: list[str]
    file_hashes: dict[str, str]
    sealed_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_run_id": self.baseline_run_id,
            "query_bundle": self.query_bundle.to_dict(),
            "source_roles": list(self.source_roles),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "classifications": dict(self.classifications),
            "corpus_revision": self.corpus_revision,
            "edit_generation": self.edit_generation,
            "edit_inventory": list(self.edit_inventory),
            "file_hashes": dict(self.file_hashes),
            "sealed_at": self.sealed_at,
        }


@dataclass
class ImpactRun:
    run_id: str
    phase: str
    query_bundle: ImpactQueryBundle
    source_roles: list[str]
    page_size: int
    corpus_revision: str
    edit_generation: int
    candidates: list[ImpactCandidate] = field(default_factory=list)
    baseline_run_id: str | None = None
    status: str = "open"
    seen_candidates: list[str] = field(default_factory=list)
    read_candidates: list[str] = field(default_factory=list)
    consumed_pages: list[int] = field(default_factory=list)
    scope_drift: ScopeDrift = field(default_factory=ScopeDrift)
    metrics: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    finished_at: str | None = None

    def candidate_map(self) -> dict[str, ImpactCandidate]:
        return {candidate.candidate_id: candidate for candidate in self.candidates}

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "phase": self.phase,
            "query_bundle": self.query_bundle.to_dict(),
            "source_roles": list(self.source_roles),
            "page_size": self.page_size,
            "corpus_revision": self.corpus_revision,
            "edit_generation": self.edit_generation,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "baseline_run_id": self.baseline_run_id,
            "status": self.status,
            "seen_candidates": list(self.seen_candidates),
            "read_candidates": list(self.read_candidates),
            "consumed_pages": list(self.consumed_pages),
            "scope_drift": self.scope_drift.to_dict(),
            "metrics": dict(self.metrics),
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


@dataclass
class IndexStatus:
    documents: int
    chunks: int
    corpus_revision: str
    edit_generation: int
    embedding_strategy_id: str
    sqlite_vec_available: bool = False
    stale_documents: list[str] = field(default_factory=list)
    model: str = ""
    model_ready: bool = False
    model_cache_dir: str | None = None
    skipped_documents: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DocMeshError(RuntimeError):
    """Base class for errors intended to be shown by CLI/MCP callers."""


class ApprovalRequired(DocMeshError):
    pass


class ModelNotInstalledError(DocMeshError):
    pass


class UnsupportedDocumentError(DocMeshError):
    pass


class StaleSourceError(DocMeshError):
    pass


class ImpactStateError(DocMeshError):
    pass


class CorpusMutationError(ImpactStateError):
    pass


class ValidationError(DocMeshError):
    pass


def location_from_mapping(value: Mapping[str, Any]) -> SourceLocation:
    """Load both actionable public locations and older persisted aliases."""

    from pathlib import Path

    def _first(*names: str, default: Any = None) -> Any:
        for name in names:
            if name in value and value[name] is not None:
                return value[name]
        return default

    raw_path = str(_first("canonical_path", "path", default=""))
    path = str(Path(raw_path).expanduser().resolve(strict=False)) if raw_path else ""
    raw_format = str(
        _first(
            "format",
            default="pdf" if _first("page_number", "page") is not None else "text",
        )
    )

    def _integer(item: Any) -> int | None:
        if item is None or item == "":
            return None
        return int(item)

    return SourceLocation(
        path=path,
        breadcrumb=str(_first("section_breadcrumb", "breadcrumb", default="")),
        start_line=_integer(_first("start_line")),
        end_line=_integer(_first("end_line")),
        page=_integer(_first("page_number", "page")),
        span_hash=str(
            _first("content_hash", "source_span_hash", "span_hash", default="")
        ),
        file_hash=str(
            _first("current_file_hash", "revision_hash", "file_hash", default="")
        ),
        snippet=str(_first("source_snippet", "bounded_passage", "snippet", default="")),
        role=str(_first("role", default="editable")),
        format=raw_format,
    )
