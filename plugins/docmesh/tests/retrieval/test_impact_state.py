from pathlib import Path

import pytest

from docmesh.embeddings import DeterministicEmbedder
from docmesh.impact import ImpactEngine
from docmesh.index import Indexer, SQLiteIndex
from docmesh.models import (
    CorpusMutationError,
    ImpactQueryBundle,
    ImpactStateError,
    SearchResult,
    SourceLocation,
    ValidationError,
)
from docmesh.retrieval import RetrievalService


def _engine(tmp_path: Path) -> ImpactEngine:
    (tmp_path / "a.md").write_text(
        "# A\nThe cache policy is documented here.", encoding="utf-8"
    )
    (tmp_path / "b.md").write_text(
        "# B\nThe cache policy is documented there.", encoding="utf-8"
    )
    return ImpactEngine(
        Indexer(
            tmp_path, index=SQLiteIndex(":memory:"), embedder=DeterministicEmbedder(32)
        )
    )


def test_discovery_paginates_classifies_and_seals_immutable_baseline(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    run = engine.impact_start(
        query_bundle=ImpactQueryBundle("cache policy", exact_terms=["cache policy"]),
        page_size=1,
    )
    cursor = None
    while True:
        page = engine.impact_page(run.run_id, cursor)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    decisions = {candidate.candidate_id: "needs_edit" for candidate in run.candidates}
    engine.impact_classify(run.run_id, decisions)
    baseline = engine.impact_finish(run.run_id)
    assert baseline.baseline_run_id == run.run_id
    assert baseline.edit_inventory
    with pytest.raises(ImpactStateError):
        engine.impact_classify(run.run_id, decisions)


def test_finish_rejects_corpus_mutation_after_snapshot(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    run = engine.impact_start(
        query_bundle=ImpactQueryBundle("cache policy", exact_terms=["cache policy"]),
        page_size=20,
    )
    engine.impact_page(run.run_id)
    for candidate in run.candidates:
        engine.impact_classify(run.run_id, {candidate.candidate_id: "consistent"})
    (tmp_path / "a.md").write_text(
        "# A\nThe changed policy is different.", encoding="utf-8"
    )
    with pytest.raises(CorpusMutationError):
        engine.impact_finish(run.run_id)


def test_invalid_impact_location_gets_one_targeted_reindex_before_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "a.md"
    source.write_text("# A\nActual evidence.", encoding="utf-8")
    indexer = Indexer(
        tmp_path, index=SQLiteIndex(":memory:"), embedder=DeterministicEmbedder(16)
    )
    indexer.index()
    service = RetrievalService(indexer)
    row = indexer.store.chunks(str(source))[0]
    document = indexer.store.document(str(source))
    assert document is not None
    invalid = SourceLocation(
        str(source),
        str(row["breadcrumb"]),
        start_line=None,
        end_line=None,
        file_hash=str(document["file_hash"]),
        role="editable",
        format="markdown",
    )
    monkeypatch.setattr(service, "find", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        service,
        "search",
        lambda *args, **kwargs: [SearchResult(invalid, str(row["text"]), 1.0)],
    )
    original_validate = service.validate_location
    validation_calls: list[SourceLocation] = []

    def _flaky_validate(location: SourceLocation) -> SourceLocation:
        validation_calls.append(location)
        if len(validation_calls) == 1:
            raise ValidationError("line metadata was invalid")
        return original_validate(location)

    monkeypatch.setattr(service, "validate_location", _flaky_validate)
    reindex_calls: list[str] = []
    original_reindex = indexer.reindex_path

    def _record_reindex(path: str) -> object:
        reindex_calls.append(path)
        return original_reindex(path)

    monkeypatch.setattr(indexer, "reindex_path", _record_reindex)
    engine = ImpactEngine(indexer, service)

    run = engine.impact_start(query_bundle=ImpactQueryBundle("needle"), page_size=10)
    assert run.candidates and run.candidates[0].resolved
    assert len(validation_calls) == 2
    assert reindex_calls == [str(source.resolve())]
