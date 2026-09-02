"""Recall-first impact discovery and immutable verification state machine."""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from .index import Indexer
from .models import (
    Baseline,
    CorpusMutationError,
    ImpactCandidate,
    ImpactPage,
    ImpactQueryBundle,
    ImpactReadResult,
    ImpactRun,
    ImpactStateError,
    ScopeDrift,
    SourceLocation,
    StaleSourceError,
    ValidationError,
    location_from_mapping,
)
from .retrieval import RetrievalService


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _candidate_id(location: SourceLocation) -> str:
    value = "\0".join(
        [
            location.path,
            str(location.page or ""),
            str(location.start_line or ""),
            str(location.end_line or ""),
            location.span_hash,
        ]
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _candidate_from_mapping(value: Mapping[str, Any]) -> ImpactCandidate:
    return ImpactCandidate(
        candidate_id=str(value["candidate_id"]),
        location=location_from_mapping(value.get("location", {})),
        text=str(value.get("text", "")),
        channels=tuple(value.get("channels", ()) or ()),
        retrieval_scores=dict(value.get("retrieval_scores", {}) or {}),
        classification=value.get("classification"),
        read=bool(value.get("read", False)),
        resolved=bool(value.get("resolved", True)),
    )


def _bundle(value: ImpactQueryBundle | Mapping[str, Any]) -> ImpactQueryBundle:
    return (
        value
        if isinstance(value, ImpactQueryBundle)
        else ImpactQueryBundle.from_mapping(value)
    )


class ImpactEngine:
    def __init__(
        self, indexer: Indexer, retrieval: RetrievalService | None = None
    ) -> None:
        self.indexer = indexer
        self.retrieval = retrieval or RetrievalService(indexer)

    def _persist(self, run: ImpactRun) -> None:
        self.indexer.store.save_run(run.run_id, run.to_dict())

    def _load_run(self, run_id: str) -> ImpactRun:
        payload = self.indexer.store.load_run(run_id)
        if payload is None:
            raise ImpactStateError(f"unknown impact run: {run_id}")
        return ImpactRun(
            run_id=str(payload["run_id"]),
            phase=str(payload["phase"]),
            query_bundle=_bundle(payload["query_bundle"]),
            source_roles=list(payload.get("source_roles", [])),
            page_size=int(payload.get("page_size", 20)),
            corpus_revision=str(payload.get("corpus_revision", "")),
            edit_generation=int(payload.get("edit_generation", 0)),
            candidates=[
                _candidate_from_mapping(item) for item in payload.get("candidates", [])
            ],
            baseline_run_id=payload.get("baseline_run_id"),
            status=str(payload.get("status", "open")),
            seen_candidates=list(payload.get("seen_candidates", [])),
            read_candidates=list(payload.get("read_candidates", [])),
            consumed_pages=[int(item) for item in payload.get("consumed_pages", [])],
            scope_drift=ScopeDrift(**dict(payload.get("scope_drift", {}) or {})),
            metrics=dict(payload.get("metrics", {}) or {}),
            created_at=str(payload.get("created_at", "")),
            finished_at=payload.get("finished_at"),
        )

    def _load_baseline(self, run_id: str) -> Baseline:
        payload = self.indexer.store.load_baseline(run_id)
        if payload is None:
            raise ImpactStateError(f"unknown or unsealed baseline: {run_id}")
        return Baseline(
            baseline_run_id=str(payload["baseline_run_id"]),
            query_bundle=_bundle(payload["query_bundle"]),
            source_roles=list(payload.get("source_roles", [])),
            candidates=[
                _candidate_from_mapping(item) for item in payload.get("candidates", [])
            ],
            classifications=dict(payload.get("classifications", {}) or {}),
            corpus_revision=str(payload.get("corpus_revision", "")),
            edit_generation=int(payload.get("edit_generation", 0)),
            edit_inventory=list(payload.get("edit_inventory", [])),
            file_hashes=dict(payload.get("file_hashes", {}) or {}),
            sealed_at=str(payload.get("sealed_at", "")),
        )

    @staticmethod
    def _validate_roles(source_roles: Sequence[str]) -> list[str]:
        roles = list(dict.fromkeys(str(role) for role in source_roles))
        if not roles:
            roles = ["editable"]
        invalid = [
            role for role in roles if role not in ("editable", "reference", "mirror")
        ]
        if invalid:
            raise ValidationError(
                "invalid source role(s): {}".format(", ".join(invalid))
            )
        return roles

    def _candidate_from_location(
        self,
        location: SourceLocation,
        text: str,
        channels: Iterable[str],
        scores: Mapping[str, float] | None = None,
    ) -> ImpactCandidate:
        return ImpactCandidate(
            _candidate_id(location),
            location,
            text,
            tuple(sorted(set(channels))),
            dict(scores or {}),
        )

    def _merge_candidate(
        self, candidates: dict[str, ImpactCandidate], candidate: ImpactCandidate
    ) -> None:
        old = candidates.get(candidate.candidate_id)
        if old is None:
            candidates[candidate.candidate_id] = candidate
            return
        old.channels = tuple(sorted(set(old.channels) | set(candidate.channels)))
        old.retrieval_scores.update(candidate.retrieval_scores)

    def _resolve_and_validate(
        self, candidate: ImpactCandidate, *, query: str = ""
    ) -> ImpactCandidate:
        try:
            candidate.location = self.retrieval.validate_location(candidate.location)
            candidate.resolved = True
            return candidate
        except (StaleSourceError, ValidationError) as first_error:
            # A dirty hook may race candidate generation.  Reindex exactly the
            # affected source, then resolve once against fresh chunks.  A
            # second failure is actionable and aborts freezing the run.
            self.indexer.reindex_path(candidate.location.path)
            fresh: ImpactCandidate | None = None
            for row in self.indexer.store.chunks(candidate.location.path):
                if str(row["text"]) == candidate.text or (
                    query and query.lower() in str(row["text"]).lower()
                ):
                    location = self.retrieval._location_for_chunk_row(row)
                    fresh = self._candidate_from_location(
                        location,
                        str(row["text"]),
                        candidate.channels,
                        candidate.retrieval_scores,
                    )
                    break
            if fresh is None:
                raise StaleSourceError(
                    f"stale impact location could not be resolved after targeted reindex: {candidate.location.path}"
                ) from first_error
            try:
                fresh.location = self.retrieval.validate_location(fresh.location)
            except (StaleSourceError, ValidationError) as second_error:
                raise StaleSourceError(
                    f"stale impact location could not be resolved after one retry: {candidate.location.path}"
                ) from second_error
            return fresh

    def _generate_candidates(
        self, bundle: ImpactQueryBundle, source_roles: Sequence[str]
    ) -> list[ImpactCandidate]:
        candidates: dict[str, ImpactCandidate] = {}
        exact_queries = list(
            dict.fromkeys(list(bundle.exact_terms) + list(bundle.aliases))
        )
        if not exact_queries:
            exact_queries = [bundle.canonical_claim]
        for term in exact_queries:
            if not str(term).strip():
                continue
            for result in self.retrieval.find(
                term, mode="literal", source_roles=source_roles
            ):
                candidate = self._candidate_from_location(
                    result.location,
                    result.line_text or result.match,
                    ("exact",),
                    {"exact": 1.0},
                )
                self._merge_candidate(candidates, candidate)
        expanded = bundle.expanded_queries()
        for query in expanded:
            if not query.strip():
                continue
            # RetrievalService returns the RRF union of lexical and vector
            # channels.  Each channel is independently capped at 200 there,
            # satisfying the recall-first candidate budget.
            for search_result in self.retrieval.search(
                query, limit=400, source_roles=source_roles
            ):
                candidate = self._candidate_from_location(
                    search_result.location,
                    search_result.text,
                    search_result.channels,
                    {"rrf": search_result.score},
                )
                self._merge_candidate(candidates, candidate)
        ordered = sorted(
            candidates.values(),
            key=lambda item: (
                item.location.path,
                item.location.page or 0,
                item.location.start_line or 0,
                item.candidate_id,
            ),
        )
        validated: list[ImpactCandidate] = []
        for candidate in ordered:
            # Search candidates carry chunk passages as useful context; exact
            # candidates carry the complete source line/page.  Validation is
            # always against the source location, never against the passage.
            resolved = self._resolve_and_validate(
                candidate, query=bundle.canonical_claim
            )
            # A source may change role between retrieval and validation.  Do
            # not leak a now-reference/mirror location into an editable run.
            if resolved.location.role in source_roles:
                validated.append(resolved)
        return validated

    def impact_start(
        self,
        phase: str = "discover",
        query_bundle: ImpactQueryBundle | Mapping[str, Any] | None = None,
        source_roles: Sequence[str] | None = None,
        page_size: int = 20,
        baseline_run_id: str | None = None,
    ) -> ImpactRun:
        if phase not in ("discover", "verify"):
            raise ValidationError("impact phase must be discover or verify")
        if page_size <= 0:
            raise ValidationError("impact page_size must be positive")
        # Ensure the frozen candidate snapshot starts from an indexed corpus.
        self.indexer.index()
        baseline: Baseline | None = None
        if phase == "verify":
            if not baseline_run_id:
                raise ImpactStateError("verification requires baseline_run_id")
            baseline = self._load_baseline(baseline_run_id)
            requested = (
                _bundle(query_bundle)
                if query_bundle is not None
                else baseline.query_bundle
            )
            if requested.to_dict() != baseline.query_bundle.to_dict():
                raise ImpactStateError(
                    "verification must use the immutable discovery query bundle"
                )
            roles = self._validate_roles(source_roles or baseline.source_roles)
            if roles != baseline.source_roles:
                raise ImpactStateError(
                    "verification must use the immutable discovery source roles"
                )
            changes = self.indexer.changed_files_since(baseline.file_hashes)
            expected = set(baseline.edit_inventory)
            changed = set(changes["changed"])
            added = set(changes["added"])
            unexpected = sorted((changed | added) - expected)
            drift = ScopeDrift(
                sorted(expected),
                sorted(changed),
                sorted(added),
                sorted(changes["deleted"]),
                unexpected,
            )
            bundle = baseline.query_bundle
        else:
            if query_bundle is None:
                raise ValidationError("discovery requires query_bundle")
            bundle = _bundle(query_bundle)
            roles = self._validate_roles(source_roles or ["editable"])
            drift = ScopeDrift()
        if not bundle.canonical_claim.strip():
            raise ValidationError("canonical_claim must not be empty")
        started = time.monotonic()
        candidates = self._generate_candidates(bundle, roles)
        run_id = str(uuid.uuid4())
        run = ImpactRun(
            run_id,
            phase,
            bundle,
            roles,
            int(page_size),
            self.indexer.current_corpus_revision(),
            self.indexer.edit_generation,
            candidates,
            baseline_run_id,
            "open",
            scope_drift=drift,
            created_at=_now(),
        )
        run.metrics = {
            "candidate_burden": len(candidates),
            "candidate_count": len(candidates),
            "relevant_count": None,
            "candidate_relevant_ratio": None,
            "p50_candidates": len(candidates),
            "p95_candidates": len(candidates),
            "classification_token_estimate": sum(
                len(candidate.text.split()) for candidate in candidates
            ),
            "latency_ms": round((time.monotonic() - started) * 1000.0, 3),
        }
        self._persist(run)
        return run

    def impact_page(self, run_id: str, cursor: str | int | None = None) -> ImpactPage:
        run = self._load_run(run_id)
        if run.status not in ("open", "sealed", "verified"):
            raise ImpactStateError(f"impact run is not readable: {run.status}")
        try:
            offset = int(cursor) if cursor is not None else 0
        except (TypeError, ValueError) as exc:
            raise ValidationError("invalid impact cursor") from exc
        if offset < 0 or offset > len(run.candidates):
            raise ValidationError("impact cursor is outside candidate snapshot")
        page_number = offset // run.page_size
        page_candidates = run.candidates[offset : offset + run.page_size]
        for candidate in page_candidates:
            if candidate.candidate_id not in run.seen_candidates:
                run.seen_candidates.append(candidate.candidate_id)
        if page_number not in run.consumed_pages:
            run.consumed_pages.append(page_number)
        next_cursor = (
            str(offset + len(page_candidates))
            if offset + len(page_candidates) < len(run.candidates)
            else None
        )
        self._persist(run)
        return ImpactPage(
            run.run_id,
            page_candidates,
            len(run.candidates),
            len(page_candidates),
            max(0, len(run.candidates) - offset - len(page_candidates)),
            None if cursor is None else str(cursor),
            next_cursor,
            page_number,
        )

    def impact_read(
        self, run_id: str, candidate_id: str, context_lines: int = 20
    ) -> ImpactReadResult:
        run = self._load_run(run_id)
        if run.status not in ("open", "sealed", "verified"):
            raise ImpactStateError(f"impact run is not readable: {run.status}")
        candidate = next(
            (item for item in run.candidates if item.candidate_id == candidate_id), None
        )
        if candidate is None:
            raise ImpactStateError(f"unknown impact candidate: {candidate_id}")
        if self.indexer.current_corpus_revision() != run.corpus_revision:
            raise CorpusMutationError(
                "corpus changed after impact snapshot was frozen; restart discovery or verification"
            )
        location = self.retrieval.validate_location(candidate.location)
        candidate.location = location
        candidate.read = True
        if candidate_id not in run.read_candidates:
            run.read_candidates.append(candidate_id)
        if candidate_id not in run.seen_candidates:
            run.seen_candidates.append(candidate_id)
        self._persist(run)
        if location.page is not None:
            result = self.retrieval.read(location.path, page=location.page)
        else:
            start = max(1, int(location.start_line or 1) - max(0, context_lines))
            end = int(location.end_line or start) + max(0, context_lines)
            try:
                result = self.retrieval.read(location.path, start, end)
            except ValueError:
                result = self.retrieval.read(location.path)
        return ImpactReadResult(
            candidate_id,
            location,
            result.content,
            result.path,
            result.start_line,
            result.end_line,
            result.page,
            result.file_hash,
            result.role,
            result.format,
        )

    def impact_classify(
        self,
        run_id: str,
        decisions: Mapping[str, str]
        | Sequence[Mapping[str, str]]
        | Sequence[tuple[str, str]],
    ) -> ImpactRun:
        run = self._load_run(run_id)
        if run.status != "open":
            raise ImpactStateError("impact run is sealed and immutable")
        values: list[tuple[str | None, str | None]] = []
        if isinstance(decisions, Mapping):
            values.extend(decisions.items())
        else:
            for item in decisions:
                if isinstance(item, Mapping):
                    values.append(
                        (
                            item.get("candidate_id"),
                            item.get("classification", item.get("decision")),
                        )
                    )
                else:
                    values.append(item)
        known = {candidate.candidate_id: candidate for candidate in run.candidates}
        for candidate_id, classification in values:
            if candidate_id is None or candidate_id not in known:
                raise ValidationError(f"unknown impact candidate: {candidate_id}")
            if classification is None or classification not in (
                "needs_edit",
                "consistent",
                "unrelated",
                "uncertain",
            ):
                raise ValidationError(
                    f"invalid impact classification: {classification}"
                )
            if (
                known[candidate_id].classification == "uncertain"
                and classification != "uncertain"
                and not known[candidate_id].read
            ):
                raise ImpactStateError(
                    f"uncertain candidate {candidate_id} must be read before reclassification"
                )
            known[candidate_id].classification = classification
        self._persist(run)
        return run

    def _assert_finishable(self, run: ImpactRun) -> None:
        if run.status != "open":
            raise ImpactStateError("impact run has already been sealed")
        if self.indexer.current_corpus_revision() != run.corpus_revision:
            raise CorpusMutationError("corpus changed after impact snapshot was frozen")
        candidate_ids = {candidate.candidate_id for candidate in run.candidates}
        if candidate_ids - set(run.seen_candidates):
            raise ImpactStateError("all impact pages must be consumed before finish")
        if candidate_ids - {
            candidate.candidate_id
            for candidate in run.candidates
            if candidate.classification
        }:
            raise ImpactStateError(
                "every impact candidate must be classified before finish"
            )
        if any(candidate.classification == "uncertain" for candidate in run.candidates):
            raise ImpactStateError(
                "uncertain candidates require impact_read and reclassification"
            )
        if any(not candidate.resolved for candidate in run.candidates):
            raise StaleSourceError("impact run contains an unresolved source location")

    def impact_finish(self, run_id: str) -> Baseline | ImpactRun:
        run = self._load_run(run_id)
        self._assert_finishable(run)
        classifications = {
            candidate.candidate_id: str(candidate.classification)
            for candidate in run.candidates
        }
        relevant = sum(value == "needs_edit" for value in classifications.values())
        run.metrics["relevant_count"] = relevant
        run.metrics["candidate_relevant_ratio"] = (
            (len(run.candidates) / relevant) if relevant else None
        )
        if run.phase == "discover":
            inventory = sorted(
                {
                    candidate.location.path
                    for candidate in run.candidates
                    if candidate.classification == "needs_edit"
                }
            )
            baseline = Baseline(
                run.run_id,
                run.query_bundle,
                list(run.source_roles),
                run.candidates,
                classifications,
                run.corpus_revision,
                run.edit_generation,
                inventory,
                self.indexer.current_file_hashes(),
                _now(),
            )
            self.indexer.store.save_baseline(run.run_id, baseline.to_dict())
            run.status = "sealed"
            run.finished_at = _now()
            self._persist(run)
            return baseline
        baseline = self._load_baseline(str(run.baseline_run_id))
        if self.indexer.edit_generation != run.edit_generation:
            raise CorpusMutationError(
                "verification is stale because another edit generation was indexed"
            )
        if any(
            candidate.classification == "needs_edit" for candidate in run.candidates
        ):
            raise ImpactStateError(
                "verification failed: relevant needs_edit candidates remain"
            )
        run.status = "verified"
        run.finished_at = _now()
        self._persist(run)
        return run


def impact_start(indexer: Indexer, **kwargs: Any) -> ImpactRun:
    return ImpactEngine(indexer).impact_start(**kwargs)


def impact_page(
    indexer: Indexer, run_id: str, cursor: str | int | None = None
) -> ImpactPage:
    return ImpactEngine(indexer).impact_page(run_id, cursor)


def impact_read(
    indexer: Indexer, run_id: str, candidate_id: str, context_lines: int = 20
) -> ImpactReadResult:
    return ImpactEngine(indexer).impact_read(run_id, candidate_id, context_lines)


def impact_classify(indexer: Indexer, run_id: str, decisions: Any) -> ImpactRun:
    return ImpactEngine(indexer).impact_classify(run_id, decisions)


def impact_finish(indexer: Indexer, run_id: str) -> Baseline | ImpactRun:
    return ImpactEngine(indexer).impact_finish(run_id)
