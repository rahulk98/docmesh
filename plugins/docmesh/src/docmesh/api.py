"""Stateless public operation adapters used by CLI, hooks, and MCP."""

from __future__ import annotations

import importlib.util
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .config import (
    DEFAULT_MODEL,
    discover_corpus,
    initialize_project,
    is_model_ready,
    model_cache_dir,
)
from .embeddings import DeterministicEmbedder, FastEmbedBackend
from .impact import ImpactEngine
from .index import Indexer
from .models import ImpactQueryBundle, SearchMetrics
from .retrieval import RetrievalService


def _indexer(
    project_root: str | Path = ".",
    *,
    db_path: str | None = None,
    embedder: object | None = None,
    load_model: bool = True,
    local_files_only: bool = True,
    cache_dir: str | Path | None = None,
) -> Indexer:
    return Indexer(
        project_root,
        db_path=db_path,
        embedder=embedder,  # type: ignore[arg-type]
        load_model=load_model,
        local_files_only=local_files_only,
        cache_dir=cache_dir,
    )


def _embedder_from_arguments(arguments: Mapping[str, Any]) -> object | None:
    # Deterministic embeddings are a test-only opt-in.  A normal operation
    # leaves this unset so Indexer constructs production FastEmbed locally.
    if arguments.get("deterministic") or arguments.get("test_double"):
        return DeterministicEmbedder(int(arguments.get("dimensions", 384)))
    if arguments.get("use_fastembed"):
        return FastEmbedBackend(
            str(arguments.get("model", DEFAULT_MODEL)),
            cache_dir=arguments.get("cache_dir"),
            local_files_only=True,
        )
    return None


def setup(
    project_root: str | Path = ".",
    *,
    approve: bool = False,
    dry_run: bool = False,
    summary: bool = True,
    included_samples: int = 500,
    excluded_samples: int = 200,
    **kwargs: Any,
) -> Mapping[str, Any]:
    if dry_run:
        report = discover_corpus(project_root)
    else:
        report = initialize_project(
            project_root,
            approve=approve,
            model=str(kwargs.get("model", DEFAULT_MODEL)),
            cache_dir=kwargs.get("cache_dir"),
            download_model=kwargs.get("download_model"),
        )
    if summary:
        return report.summary_dict(
            included_samples=included_samples, excluded_samples=excluded_samples
        )
    return report.to_dict()


def init(
    project_root: str | Path = ".",
    *,
    approve: bool = False,
    dry_run: bool = False,
    summary: bool = True,
    **kwargs: Any,
) -> Mapping[str, Any]:
    return setup(project_root, approve=approve, dry_run=dry_run, summary=summary, **kwargs)


def index(
    project_root: str | Path = ".",
    *,
    paths: Sequence[str | Path] | None = None,
    changed_paths: Sequence[str | Path] | None = None,
    db_path: str | None = None,
    force: bool = False,
    **kwargs: Any,
) -> Mapping[str, Any]:
    arguments = dict(kwargs)
    arguments.update(
        {
            "deterministic": kwargs.get("deterministic", False),
            "dimensions": kwargs.get("dimensions", 384),
        }
    )
    worker = _indexer(
        project_root, db_path=db_path, embedder=_embedder_from_arguments(arguments)
    )
    try:
        values = paths if paths is not None else changed_paths
        return worker.index(values, force=force).to_dict()
    finally:
        worker.store.close()


def reindex(project_root: str | Path = ".", **kwargs: Any) -> Mapping[str, Any]:
    return index(project_root, **kwargs)


def status(
    project_root: str | Path = ".", *, db_path: str | None = None, **kwargs: Any
) -> Mapping[str, Any]:
    # Status must never construct FastEmbed: it is safe before setup and with
    # network access disabled.
    worker = _indexer(project_root, db_path=db_path, load_model=False)
    try:
        value = worker.status().to_dict()
        value["manifest"] = worker.manifest.to_dict()
        return value
    finally:
        worker.store.close()


def doctor(project_root: str | Path = ".", **kwargs: Any) -> Mapping[str, Any]:
    # Doctor only probes installed modules and the project-local readiness
    # marker; it must not import/construct a model or trigger a download.
    worker = _indexer(project_root, db_path=kwargs.get("db_path"), load_model=False)
    try:
        fts5 = False
        try:
            worker.store.conn.execute(
                "CREATE VIRTUAL TABLE temp.docmesh_fts_probe USING fts5(value)"
            )
            worker.store.conn.execute("DROP TABLE temp.docmesh_fts_probe")
            fts5 = True
        except sqlite3.DatabaseError:
            pass
        return {
            "project_root": str(worker.root),
            "python": ".".join(
                str(item) for item in __import__("sys").version_info[:3]
            ),
            "fastembed_installed": importlib.util.find_spec("fastembed") is not None,
            "pypdf_installed": importlib.util.find_spec("pypdf") is not None,
            "sqlite_fts5": fts5,
            "sqlite_vec": worker.sqlite_vec_available,
            "network_required_for_index": False,
            "model": worker.manifest.model,
            "model_ready": is_model_ready(worker.root, worker.manifest.model),
            "model_cache_dir": str(model_cache_dir(worker.root)),
        }
    finally:
        worker.store.close()


def probe_hooks(project_root: str | Path = ".", **kwargs: Any) -> Mapping[str, Any]:
    # Core probing is deliberately conservative: only capabilities proven by
    # an external harness should be reported as strict-capable.
    return {
        "project_root": str(Path(project_root).expanduser().resolve()),
        "proven": False,
        "mode": "advisory",
        "reason": "runtime hook proof is owned by the harness",
    }


def search(
    project_root: str | Path = ".", query: str = "", limit: int = 8, **kwargs: Any
) -> Any:
    worker = _indexer(
        project_root,
        db_path=kwargs.get("db_path"),
        embedder=_embedder_from_arguments(kwargs),
    )
    try:
        max_snippet_length = kwargs.get("max_snippet_length")
        return RetrievalService(worker).search(
            query,
            int(limit),
            source_roles=kwargs.get("source_roles") or kwargs.get("roles"),
            snippet_only=bool(kwargs.get("snippet_only", False)),
            max_snippet_length=(
                int(max_snippet_length) if max_snippet_length is not None else 200
            ),
        )
    finally:
        worker.store.close()


def bench(
    project_root: str | Path = ".",
    queries: Sequence[Mapping[str, Any]] = (),
    **kwargs: Any,
) -> Mapping[str, Any]:
    worker = _indexer(
        project_root,
        db_path=kwargs.get("db_path"),
        embedder=_embedder_from_arguments(kwargs),
    )
    try:
        service = RetrievalService(worker)
        reciprocal = 0.0
        recalled = 0
        result_count = 0
        per_query: list[dict[str, Any]] = []
        for item in queries:
            query = str(item.get("query", ""))
            expect_path = item.get("expect_path_contains")
            expect_text = item.get("expect_text_contains")
            found = service.search(query, 8)
            result_count += len(found)
            hit_rank: int | None = None
            for rank, result in enumerate(found, start=1):
                if expect_path and expect_path not in result.location.path:
                    continue
                if expect_text and expect_text not in result.text:
                    continue
                hit_rank = rank
                break
            if hit_rank is not None:
                reciprocal += 1.0 / hit_rank
                recalled += 1
            per_query.append({"query": query, "hit_rank": hit_rank})
        total = float(len(queries)) if queries else 1.0
        metrics = SearchMetrics(
            reciprocal / total,
            recalled / total,
            len(queries),
            result_count,
        )
        payload = metrics.to_dict()
        payload["per_query"] = per_query
        return payload
    finally:
        worker.store.close()


def find(
    project_root: str | Path = ".",
    pattern: str = "",
    mode: str = "literal",
    cursor: Any = None,
    **kwargs: Any,
) -> Any:
    worker = _indexer(
        project_root,
        db_path=kwargs.get("db_path"),
        embedder=_embedder_from_arguments(kwargs),
    )
    try:
        return RetrievalService(worker).find(
            pattern,
            mode,
            cursor,
            source_roles=kwargs.get("source_roles") or kwargs.get("roles"),
            scope=kwargs.get("scope"),
        )
    finally:
        worker.store.close()


def read(
    project_root: str | Path = ".",
    path: str = "",
    start_line: int | None = None,
    end_line: int | None = None,
    page: int | None = None,
    **kwargs: Any,
) -> Any:
    worker = _indexer(
        project_root,
        db_path=kwargs.get("db_path"),
        embedder=_embedder_from_arguments(kwargs),
    )
    try:
        return RetrievalService(worker).read(path, start_line, end_line, page)
    finally:
        worker.store.close()


def _impact_engine(
    project_root: str | Path, arguments: Mapping[str, Any]
) -> ImpactEngine:
    return ImpactEngine(
        _indexer(
            project_root,
            db_path=arguments.get("db_path"),
            embedder=_embedder_from_arguments(arguments),
        )
    )


def impact_start(
    project_root: str | Path = ".",
    phase: str = "discover",
    query_bundle: Any = None,
    source_roles: Sequence[str] | None = None,
    page_size: int = 20,
    baseline_run_id: str | None = None,
    **kwargs: Any,
) -> Any:
    engine = _impact_engine(project_root, kwargs)
    try:
        bundle = query_bundle
        if isinstance(bundle, Mapping):
            bundle = ImpactQueryBundle.from_mapping(bundle)
        return engine.impact_start(
            phase, bundle, source_roles, int(page_size), baseline_run_id
        )
    finally:
        engine.indexer.store.close()


def impact_page(
    project_root: str | Path = ".", run_id: str = "", cursor: Any = None, **kwargs: Any
) -> Any:
    engine = _impact_engine(project_root, kwargs)
    try:
        return engine.impact_page(run_id, cursor)
    finally:
        engine.indexer.store.close()


def impact_read(
    project_root: str | Path = ".",
    run_id: str = "",
    candidate_id: str = "",
    context_lines: int = 20,
    **kwargs: Any,
) -> Any:
    engine = _impact_engine(project_root, kwargs)
    try:
        return engine.impact_read(run_id, candidate_id, int(context_lines))
    finally:
        engine.indexer.store.close()


def impact_classify(
    project_root: str | Path = ".",
    run_id: str = "",
    decisions: Any = None,
    **kwargs: Any,
) -> Any:
    engine = _impact_engine(project_root, kwargs)
    try:
        return engine.impact_classify(run_id, decisions or {})
    finally:
        engine.indexer.store.close()


def impact_finish(
    project_root: str | Path = ".", run_id: str = "", **kwargs: Any
) -> Any:
    engine = _impact_engine(project_root, kwargs)
    try:
        return engine.impact_finish(run_id)
    finally:
        engine.indexer.store.close()
