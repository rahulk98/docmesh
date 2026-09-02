"""DocMesh V1 local document retrieval and consistency engine."""

# The package-level operations use the configured project in the current
# working directory.  RetrievalService remains available for callers that
# manage an Indexer explicitly.
from .api import doctor, index, init, probe_hooks, reindex, setup, status
from .api import find as find_project
from .api import read as read_project
from .api import search as search_project
from .chunking import (
    ApproximateTokenizer,
    Chunker,
    TokenBudgetError,
    compute_embedding_strategy_id,
    token_count,
)
from .config import (
    ApprovalRequired,
    DiscoveryReport,
    Manifest,
    canonical_path,
    discover_corpus,
    initialize_project,
    load_manifest,
)
from .embeddings import (
    DeterministicEmbedder,
    FastEmbedBackend,
    HashEmbedder,
    ModelNotInstalledError,
)
from .impact import (
    ImpactEngine,
    impact_classify,
    impact_finish,
    impact_page,
    impact_read,
    impact_start,
)
from .index import Indexer, SQLiteIndex
from .models import (
    Baseline,
    Chunk,
    CorpusMutationError,
    FindResult,
    ImpactCandidate,
    ImpactPage,
    ImpactQueryBundle,
    ImpactReadResult,
    ImpactRun,
    ImpactStateError,
    IndexStatus,
    ReadResult,
    ScopeDrift,
    SearchMetrics,
    SearchResult,
    SourceLocation,
    StaleSourceError,
    ValidationError,
)
from .parsing import (
    DocumentParser,
    ParsedDocument,
    parse_document,
    parse_file,
    parse_text,
)
from .retrieval import RetrievalService, search_metrics, validate_location


def search(query: str = "", limit: int = 8, *, project_root: str = ".", **kwargs):
    return search_project(project_root, query=query, limit=limit, **kwargs)


def find(
    pattern: str = "",
    mode: str = "literal",
    cursor=None,
    *,
    project_root: str = ".",
    **kwargs,
):
    return find_project(
        project_root, pattern=pattern, mode=mode, cursor=cursor, **kwargs
    )


def read(
    path: str = "",
    start_line=None,
    end_line=None,
    page=None,
    *,
    project_root: str = ".",
    **kwargs,
):
    return read_project(
        project_root,
        path=path,
        start_line=start_line,
        end_line=end_line,
        page=page,
        **kwargs,
    )


__all__ = [
    "ApprovalRequired",
    "ApproximateTokenizer",
    "Baseline",
    "Chunk",
    "Chunker",
    "CorpusMutationError",
    "DeterministicEmbedder",
    "DiscoveryReport",
    "DocumentParser",
    "FastEmbedBackend",
    "FindResult",
    "HashEmbedder",
    "ImpactCandidate",
    "ImpactEngine",
    "ImpactPage",
    "ImpactQueryBundle",
    "ImpactReadResult",
    "ImpactRun",
    "ImpactStateError",
    "IndexStatus",
    "Indexer",
    "Manifest",
    "ModelNotInstalledError",
    "ParsedDocument",
    "ReadResult",
    "RetrievalService",
    "SQLiteIndex",
    "ScopeDrift",
    "SearchMetrics",
    "SearchResult",
    "SourceLocation",
    "StaleSourceError",
    "TokenBudgetError",
    "ValidationError",
    "canonical_path",
    "compute_embedding_strategy_id",
    "discover_corpus",
    "doctor",
    "find",
    "find_project",
    "impact_classify",
    "impact_finish",
    "impact_page",
    "impact_read",
    "impact_start",
    "index",
    "init",
    "initialize_project",
    "load_manifest",
    "parse_document",
    "parse_file",
    "parse_text",
    "probe_hooks",
    "read",
    "read_project",
    "reindex",
    "search",
    "search_metrics",
    "search_project",
    "setup",
    "status",
    "token_count",
    "validate_location",
]
