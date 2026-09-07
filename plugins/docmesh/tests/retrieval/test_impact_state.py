from pathlib import Path

import pytest

from docmesh.embeddings import DeterministicEmbedder
from docmesh.impact import ImpactEngine
from docmesh.index import Indexer, SQLiteIndex
from docmesh.models import (
    CorpusMutationError,
    ImpactQueryBundle,
    ImpactStateError,
    Manifest,
    SearchResult,
    SourceConfig,
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


def test_candidates_are_tagged_with_match_kind_and_ordered_exact_first(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    run = engine.impact_start(
        query_bundle=ImpactQueryBundle(
            "cache policy",
            exact_terms=["cache policy"],
            semantic_queries=["storage rules"],
        ),
        page_size=20,
    )
    assert run.candidates
    kinds = [candidate.match_kind for candidate in run.candidates]
    assert all(kind in ("exact_term", "alias", "semantic_only") for kind in kinds)
    # exact_term candidates must sort before any semantic_only candidate.
    priority = {"exact_term": 2, "alias": 1, "semantic_only": 0}
    ranks = [priority[kind] for kind in kinds]
    assert ranks == sorted(ranks, reverse=True)
    exact = [c for c in run.candidates if c.match_kind == "exact_term"]
    assert exact and all(c.matched_terms == ["cache policy"] for c in exact)
    assert run.metrics["by_match_kind"]
    assert run.metrics["semantic_limit"] == 50
    assert run.metrics["semantic_only_dropped"] == 0


def test_reloaded_page_preserves_exact_term_match_kind_and_matches_aggregate(
    tmp_path: Path,
) -> None:
    # Regression: _candidate_from_mapping (the persisted-run reload path used
    # by impact_page) must round-trip match_kind/matched_terms, not silently
    # default every reloaded candidate back to semantic_only.
    engine = _engine(tmp_path)
    run = engine.impact_start(
        query_bundle=ImpactQueryBundle(
            "cache policy",
            exact_terms=["cache policy"],
            semantic_queries=["storage rules"],
        ),
        page_size=20,
    )
    page = engine.impact_page(run.run_id)
    exact = [c for c in page.candidates if c.match_kind == "exact_term"]
    assert exact
    assert "cache policy" in exact[0].matched_terms
    tally = {}
    for candidate in page.candidates:
        tally[candidate.match_kind] = tally.get(candidate.match_kind, 0) + 1
    assert tally == run.metrics["by_match_kind"]


def test_semantic_limit_caps_semantic_only_candidates_and_reports_the_cap(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    run = engine.impact_start(
        query_bundle=ImpactQueryBundle(
            "cache policy",
            exact_terms=["cache policy"],
            semantic_queries=["storage rules", "retention plan"],
        ),
        page_size=20,
        semantic_limit=0,
    )
    assert all(c.match_kind != "semantic_only" for c in run.candidates)
    assert run.metrics["semantic_limit"] == 0
    # exact hits are never capped
    assert any(c.match_kind == "exact_term" for c in run.candidates)


def test_api_impact_start_returns_only_run_id_counts_and_first_page(
    tmp_path: Path,
) -> None:
    from docmesh import api

    (tmp_path / "a.md").write_text(
        "# A\nThe cache policy is documented here.", encoding="utf-8"
    )
    (tmp_path / "b.md").write_text(
        "# B\nThe cache policy is documented there.", encoding="utf-8"
    )
    api.setup(project_root=tmp_path, deterministic=True, approve=True)
    result = api.impact_start(
        project_root=tmp_path,
        query_bundle={"canonical_claim": "cache policy", "exact_terms": ["cache policy"]},
        page_size=1,
        deterministic=True,
    )
    payload = result.to_dict()
    assert set(payload) == {
        "run_id",
        "phase",
        "status",
        "source_roles",
        "page_size",
        "semantic_limit",
        "counts",
        "first_page",
        "metrics",
    }
    assert payload["counts"]["total"] >= 1
    # the first page must never carry the full candidate set.
    assert len(payload["first_page"]["candidates"]) <= payload["page_size"]
    if payload["counts"]["total"] > payload["page_size"]:
        assert payload["first_page"]["next_cursor"] is not None


def test_default_discovery_roles_exclude_reference_and_widen_on_request(
    tmp_path: Path,
) -> None:
    editable = tmp_path / "editable.md"
    editable.write_text("# E\nThe safe band term appears here.", encoding="utf-8")
    reference = tmp_path / "reference.md"
    reference.write_text("# R\nThe safe band term appears here too.", encoding="utf-8")
    manifest = Manifest(
        str(tmp_path),
        sources=[SourceConfig(str(editable), "editable"), SourceConfig(str(reference), "reference")],
    )
    engine = ImpactEngine(
        Indexer(
            tmp_path,
            manifest=manifest,
            index=SQLiteIndex(":memory:"),
            embedder=DeterministicEmbedder(32),
        )
    )
    run = engine.impact_start(
        query_bundle=ImpactQueryBundle("safe band", exact_terms=["safe band"]),
        page_size=20,
    )
    assert run.source_roles == ["editable"]
    assert all(c.location.role == "editable" for c in run.candidates)

    run_both = engine.impact_start(
        query_bundle=ImpactQueryBundle("safe band", exact_terms=["safe band"]),
        source_roles=["editable", "reference"],
        page_size=20,
    )
    assert set(run_both.source_roles) == {"editable", "reference"}
    assert any(c.location.role == "reference" for c in run_both.candidates)


def test_impact_page_snippet_only_omits_full_text_and_respects_page_size(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    run = engine.impact_start(
        query_bundle=ImpactQueryBundle("cache policy", exact_terms=["cache policy"]),
        page_size=20,
    )
    page = engine.impact_page(run.run_id, page_size=1, snippet_only=True)
    assert page.snippet_only is True
    assert len(page.candidates) <= 1
    payload = page.to_dict()
    for candidate in payload["candidates"]:
        assert "text" not in candidate
        assert "retrieval_scores" not in candidate
        assert {"candidate_id", "path", "match_kind", "matched_terms", "snippet"} <= set(
            candidate
        )

    full_page = engine.impact_page(run.run_id, snippet_only=False)
    full_payload = full_page.to_dict()
    assert any("text" in candidate for candidate in full_payload["candidates"])


def test_impact_classify_selector_bulk_and_explicit_override(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    run = engine.impact_start(
        query_bundle=ImpactQueryBundle("cache policy", exact_terms=["cache policy"]),
        page_size=20,
    )
    cursor = None
    while True:
        page = engine.impact_page(run.run_id, cursor)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert len(run.candidates) >= 2
    target_id = run.candidates[0].candidate_id

    updated = engine.impact_classify(
        run.run_id,
        [
            {
                "selector": {"match_kind": ["exact_term"]},
                "classification": "unrelated",
                "reason": "peer-paper boilerplate",
            },
            {"candidate_id": target_id, "classification": "needs_edit"},
        ],
    )
    by_id = {c.candidate_id: c for c in updated.candidates}
    # explicit decision always wins over the bulk selector
    assert by_id[target_id].classification == "needs_edit"
    assert all(c.classification == "unrelated" for c in updated.candidates if c.candidate_id != target_id)
    assert len(updated.bulk_selectors) == 1
    non_target = [c for c in updated.candidates if c.candidate_id != target_id]
    assert updated.bulk_selectors[0]["classified_count"] == len(non_target) + 1


def test_impact_finish_reports_counts_by_classification_and_match_kind(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    run = engine.impact_start(
        query_bundle=ImpactQueryBundle("cache policy", exact_terms=["cache policy"]),
        page_size=20,
    )
    cursor = None
    while True:
        page = engine.impact_page(run.run_id, cursor)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    engine.impact_classify(
        run.run_id, {c.candidate_id: "needs_edit" for c in run.candidates}
    )
    baseline = engine.impact_finish(run.run_id)
    metrics = engine.indexer.store.load_run(run.run_id)["metrics"]
    assert metrics["by_classification"] == {"needs_edit": len(baseline.candidates)}
    assert set(metrics["by_match_kind_classified"]) <= {"exact_term", "alias", "semantic_only"}


def test_api_impact_classify_and_finish_return_thin_summaries_not_candidates(
    tmp_path: Path,
) -> None:
    from docmesh import api

    (tmp_path / "a.md").write_text(
        "# A\nThe cache policy is documented here.", encoding="utf-8"
    )
    api.setup(project_root=tmp_path, deterministic=True, approve=True)
    start = api.impact_start(
        project_root=tmp_path,
        query_bundle={"canonical_claim": "cache policy", "exact_terms": ["cache policy"]},
        page_size=20,
        deterministic=True,
    )
    run_id = start.run_id
    ids = [c.candidate_id for c in start.first_page.candidates]
    classify = api.impact_classify(
        project_root=tmp_path,
        run_id=run_id,
        decisions={cid: "needs_edit" for cid in ids},
        deterministic=True,
    )
    payload = classify.to_dict()
    assert "candidates" not in payload
    assert payload["remaining_unclassified"] == 0
    assert payload["by_classification"] == {"needs_edit": len(ids)}

    finish = api.impact_finish(project_root=tmp_path, run_id=run_id, deterministic=True)
    finish_payload = finish.to_dict()
    assert "candidates" not in finish_payload
    assert finish_payload["edit_inventory"]["count"] >= 1
    assert "by_classification" in finish_payload["metrics"]
