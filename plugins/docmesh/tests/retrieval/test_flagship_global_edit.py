from __future__ import annotations

from pathlib import Path

from docmesh.impact import ImpactEngine
from docmesh.index import Indexer, SQLiteIndex
from docmesh.models import ImpactQueryBundle


class _SemanticFixtureEmbedder:
    model = "fixture/semantic"
    dimensions = 2
    tokenizer_name = "fixture-tokenizer"
    passage_method = "passage_embed"
    query_method = "query_embed"

    def count_tokens(self, value: str) -> int:
        return len(value.split())

    def embed_passages(self, values: list[str]):
        return [[1.0, 0.0] for _ in values]

    def embed_queries(self, values: list[str]):
        return [[1.0, 0.0] for _ in values]


def test_flagship_global_edit_discovers_twelve_paraphrases_and_verifies(
    tmp_path: Path,
) -> None:
    paraphrases = [
        "the service keeps a local record",
        "a local copy is retained by the service",
        "the tool stores an on-device record",
        "records remain available on this machine",
        "the application preserves a local snapshot",
        "an offline record is maintained by the app",
        "the client keeps its own machine-local history",
        "this workflow retains local evidence",
        "the service caches a copy on the workstation",
        "a local archive is kept for later use",
        "the system preserves an offline trace",
        "the utility records state on the local device",
    ]
    for index, phrase in enumerate(paraphrases):
        (tmp_path / f"note-{index:02d}.md").write_text(
            f"# Note {index}\n{phrase}.\n", encoding="utf-8"
        )

    indexer = Indexer(
        tmp_path, index=SQLiteIndex(":memory:"), embedder=_SemanticFixtureEmbedder()
    )
    engine = ImpactEngine(indexer)
    bundle = ImpactQueryBundle(
        "local record",
        exact_terms=["local record"],
        semantic_queries=["where the system keeps evidence"],
        implication_queries=["what remains available without a network"],
    )
    discovery = engine.impact_start(query_bundle=bundle, page_size=5)
    assert len({candidate.location.path for candidate in discovery.candidates}) == 12

    cursor = None
    while True:
        page = engine.impact_page(discovery.run_id, cursor)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    engine.impact_classify(
        discovery.run_id,
        {candidate.candidate_id: "needs_edit" for candidate in discovery.candidates},
    )
    baseline = engine.impact_finish(discovery.run_id)
    assert len(baseline.edit_inventory) == 12

    for path in sorted(tmp_path.glob("note-*.md")):
        path.write_text(
            path.read_text(encoding="utf-8") + "\nUpdated by the global edit.\n",
            encoding="utf-8",
        )
    indexer.index()

    verification = engine.impact_start(
        phase="verify", baseline_run_id=baseline.baseline_run_id, page_size=5
    )
    cursor = None
    while True:
        page = engine.impact_page(verification.run_id, cursor)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    engine.impact_classify(
        verification.run_id,
        {candidate.candidate_id: "consistent" for candidate in verification.candidates},
    )
    result = engine.impact_finish(verification.run_id)
    assert result.status == "verified"
    assert result.scope_drift.changed_files == sorted(
        str(path.resolve()) for path in tmp_path.glob("note-*.md")
    )
