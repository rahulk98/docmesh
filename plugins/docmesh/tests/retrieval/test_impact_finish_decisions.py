"""Tests for the explicit-decisions impact_finish convenience path."""

from __future__ import annotations

import pytest

from docmesh import api
from docmesh.models import ImpactStateError, ValidationError


def _setup_project(tmp_path, *documents: str) -> None:
    for index, content in enumerate(documents):
        (tmp_path / f"note-{index}.md").write_text(content, encoding="utf-8")
    api.setup(
        project_root=tmp_path,
        deterministic=True,
        approve=True,
        download_model=False,
    )


def _start(tmp_path, *, page_size: int = 20, phase: str = "discover", baseline_run_id=None):
    return api.impact_start(
        project_root=tmp_path,
        phase=phase,
        query_bundle={
            "canonical_claim": "cache policy",
            "exact_terms": ["cache policy"],
        }
        if phase == "discover"
        else None,
        page_size=page_size,
        baseline_run_id=baseline_run_id,
        deterministic=True,
    )


def _candidate_ids(tmp_path, start, *, consume_pages: bool = True) -> list[str]:
    ids = [candidate.candidate_id for candidate in start.first_page.candidates]
    if not consume_pages:
        return ids
    cursor = start.first_page.next_cursor
    while cursor is not None:
        page = api.impact_page(
            project_root=tmp_path,
            run_id=start.run_id,
            cursor=cursor,
            page_size=start.page_size,
            deterministic=True,
        )
        ids.extend(candidate.candidate_id for candidate in page.candidates)
        cursor = page.next_cursor
    return ids


def test_finish_decisions_seals_discovery_after_all_pages_are_consumed(tmp_path) -> None:
    _setup_project(tmp_path, "# Note\nThe cache policy is documented here.\n")
    start = _start(tmp_path)
    ids = _candidate_ids(tmp_path, start)
    assert ids

    result = api.impact_finish(
        project_root=tmp_path,
        run_id=start.run_id,
        decisions={candidate_id: "needs_edit" for candidate_id in ids},
        deterministic=True,
    )

    assert result.phase == "discover"
    assert result.status == "sealed"
    assert result.edit_inventory["count"] == 1


def test_finish_decisions_can_verify_a_baseline(tmp_path) -> None:
    _setup_project(tmp_path, "# Note\nThe cache policy is documented here.\n")
    discovery = _start(tmp_path)
    discovery_ids = _candidate_ids(tmp_path, discovery)
    baseline = api.impact_finish(
        project_root=tmp_path,
        run_id=discovery.run_id,
        decisions={candidate_id: "needs_edit" for candidate_id in discovery_ids},
        deterministic=True,
    )

    source = tmp_path / "note-0.md"
    source.write_text(
        "# Note\nThe cache policy is now documented and consistent.\n",
        encoding="utf-8",
    )
    verification = _start(
        tmp_path,
        phase="verify",
        baseline_run_id=baseline.baseline_run_id,
    )
    verification_ids = _candidate_ids(tmp_path, verification)
    assert verification_ids

    result = api.impact_finish(
        project_root=tmp_path,
        run_id=verification.run_id,
        decisions={candidate_id: "consistent" for candidate_id in verification_ids},
        deterministic=True,
    )

    assert result.phase == "verify"
    assert result.status == "verified"


def test_finish_decisions_does_not_bypass_unread_pages(tmp_path) -> None:
    _setup_project(
        tmp_path,
        "# One\nThe cache policy is documented here.\n",
        "# Two\nThe cache policy is documented there.\n",
    )
    start = _start(tmp_path, page_size=1)
    ids = _candidate_ids(tmp_path, start, consume_pages=False)
    assert ids
    with pytest.raises(ImpactStateError, match="all impact pages"):
        api.impact_finish(
            project_root=tmp_path,
            run_id=start.run_id,
            decisions={ids[0]: "needs_edit"},
            deterministic=True,
        )


def test_empty_decisions_do_not_auto_classify_candidates(tmp_path) -> None:
    _setup_project(tmp_path, "# Note\nThe cache policy is documented here.\n")
    start = _start(tmp_path)
    _candidate_ids(tmp_path, start)

    with pytest.raises(ImpactStateError, match="every impact candidate"):
        api.impact_finish(
            project_root=tmp_path,
            run_id=start.run_id,
            decisions={},
            deterministic=True,
        )


def test_finish_decisions_preserves_uncertain_candidate_gate(tmp_path) -> None:
    _setup_project(tmp_path, "# Note\nThe cache policy is documented here.\n")
    start = _start(tmp_path)
    ids = _candidate_ids(tmp_path, start)
    assert ids
    decisions = {candidate_id: "consistent" for candidate_id in ids}
    decisions[ids[0]] = "uncertain"

    with pytest.raises(ImpactStateError, match="uncertain"):
        api.impact_finish(
            project_root=tmp_path,
            run_id=start.run_id,
            decisions=decisions,
            deterministic=True,
        )

    api.impact_read(
        project_root=tmp_path,
        run_id=start.run_id,
        candidate_id=ids[0],
        deterministic=True,
    )
    result = api.impact_finish(
        project_root=tmp_path,
        run_id=start.run_id,
        decisions={ids[0]: "consistent"},
        deterministic=True,
    )
    assert result.status == "sealed"


def test_finish_decisions_preserves_needs_edit_verification_gate(tmp_path) -> None:
    _setup_project(tmp_path, "# Note\nThe cache policy is documented here.\n")
    discovery = _start(tmp_path)
    discovery_ids = _candidate_ids(tmp_path, discovery)
    baseline = api.impact_finish(
        project_root=tmp_path,
        run_id=discovery.run_id,
        decisions={candidate_id: "needs_edit" for candidate_id in discovery_ids},
        deterministic=True,
    )

    source = tmp_path / "note-0.md"
    source.write_text(
        "# Note\nThe cache policy is still documented here.\n",
        encoding="utf-8",
    )
    verification = _start(
        tmp_path,
        phase="verify",
        baseline_run_id=baseline.baseline_run_id,
    )
    verification_ids = _candidate_ids(tmp_path, verification)
    assert verification_ids

    with pytest.raises(ImpactStateError, match="needs_edit candidates remain"):
        api.impact_finish(
            project_root=tmp_path,
            run_id=verification.run_id,
            decisions={candidate_id: "needs_edit" for candidate_id in verification_ids},
            deterministic=True,
        )


def test_finish_decisions_rejects_unknown_candidate_id(tmp_path) -> None:
    _setup_project(tmp_path, "# Note\nThe cache policy is documented here.\n")
    start = _start(tmp_path)
    _candidate_ids(tmp_path, start)

    with pytest.raises(ValidationError, match="unknown impact candidate"):
        api.impact_finish(
            project_root=tmp_path,
            run_id=start.run_id,
            decisions={"missing-candidate": "consistent"},
            deterministic=True,
        )


def test_finish_without_decisions_keeps_existing_finish_only_behavior(tmp_path) -> None:
    _setup_project(tmp_path, "# Note\nThe cache policy is documented here.\n")
    start = _start(tmp_path)
    ids = _candidate_ids(tmp_path, start)
    api.impact_classify(
        project_root=tmp_path,
        run_id=start.run_id,
        decisions={candidate_id: "consistent" for candidate_id in ids},
        deterministic=True,
    )

    result = api.impact_finish(
        project_root=tmp_path,
        run_id=start.run_id,
        deterministic=True,
    )
    assert result.status == "sealed"
