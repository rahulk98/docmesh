#!/usr/bin/env python3
"""DocMesh Stop hook: advisory by default, strict only with runtime proof."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from pyresolve import ensure_python

ensure_python()

from capability_probe import capabilities_for_enforcement, runtime_harness
from harness import (
    HarnessPaths,
    atomic_write_json,
    core_call,
    enforcement_mode,
    pending_events,
    project_root,
    read_state,
    truthy,
)


def _nested(state: Mapping[str, Any], *keys: str) -> Any:
    value: Any = state
    for key in keys:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def verification_clean(
    state: Mapping[str, Any], project: Path
) -> tuple[bool, list[str]]:
    """Evaluate current-generation verification from common core snapshots."""

    reasons: list[str] = []
    if not state:
        reasons.append("no DocMesh status has been recorded")
    dirty = state.get(
        "dirty_files", state.get("dirty", state.get("unverified_files", []))
    )
    if truthy(dirty) or (
        isinstance(dirty, (list, tuple, set, Mapping)) and len(dirty) > 0
    ):
        reasons.append("dirty files are pending indexing")
    try:
        if pending_events(HarnessPaths.for_project(project)):
            reasons.append("durable dirty-file events are pending")
    except OSError:
        reasons.append("dirty-file queue could not be read")

    verification = state.get("verification")
    if not isinstance(verification, Mapping):
        verification = (
            state.get("last_verification")
            if isinstance(state.get("last_verification"), Mapping)
            else {}
        )
    status = (
        str(
            state.get("verification_status")
            or state.get("status")
            or verification.get("status", "")
        )
        .strip()
        .lower()
    )
    explicit_clean = state.get("verification_clean")
    if explicit_clean is False:
        reasons.append("verification is not clean")
    if (
        status not in {"verified", "passed", "clean", "ok", "success"}
        and explicit_clean is not True
    ):
        reasons.append("current edit generation has no clean verification")

    generation = state.get("edit_generation", state.get("generation"))
    verified_generation = state.get("verified_generation")
    if verified_generation is None:
        verified_generation = verification.get(
            "edit_generation", verification.get("generation")
        )
    if (
        generation is not None
        and verified_generation is not None
        and str(generation) != str(verified_generation)
    ):
        reasons.append(f"verification is stale for edit generation {generation}")

    for field in (
        "needs_edit",
        "unresolved",
        "uncertain",
        "unclassified",
        "scope_drift_files",
    ):
        value = state.get(field, verification.get(field))
        if value and (not isinstance(value, (int, float)) or value > 0):
            reasons.append(f"verification reports {field}")
    return not reasons, reasons


def _loop_breaker(paths: HarnessPaths, payload: Mapping[str, Any]) -> bool:
    """Stop re-entry after an active block or a short repeated-loop burst."""

    if truthy(payload.get("stop_hook_active")) or os.environ.get("DOCMESH_WORKER"):
        return True
    now = time.time()
    state: dict[str, Any] = {}
    try:
        state = json.loads(paths.stop_guard.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        state = {}
    generation = read_state(paths.project).get(
        "edit_generation", read_state(paths.project).get("generation")
    )
    if (
        state.get("generation") == generation
        and now - float(state.get("updated_at", 0)) < 30
    ):
        count = int(state.get("count", 0)) + 1
    else:
        count = 1
    try:
        paths.ensure()
        atomic_write_json(
            paths.stop_guard,
            {"generation": generation, "count": count, "updated_at": now},
        )
    except OSError:
        pass
    return count > 3


def evaluate(
    payload: Mapping[str, Any], project: Path | str | None = None
) -> dict[str, Any] | None:
    """Return a block/advisory record, or None when no action is required."""

    root = project_root(project or payload.get("cwd"))
    paths = HarnessPaths.for_project(root)
    if _loop_breaker(paths, payload):
        return None
    mode = enforcement_mode(root)
    state = read_state(root)
    if state and not any(
        key in state for key in ("verification_status", "verification_clean")
    ):
        # An index operation may have been run outside the launcher.  Reading
        # status is safe and gives the hook the current generation, while the
        # absence of verification still remains a block reason in proven
        # strict mode.
        status_result = core_call("status", {}, project=root)
        if status_result.get("ok") and isinstance(status_result.get("data"), Mapping):
            merged = dict(status_result["data"])
            merged.update(state)
            state = merged
    clean, reasons = verification_clean(state, root)
    if clean:
        return None
    capabilities = capabilities_for_enforcement(root, os.environ.get("DOCMESH_RUNTIME"))
    proven = bool(capabilities.get("proven"))
    if mode == "strict" and proven:
        return {
            "decision": "block",
            "reason": "DocMesh strict verification required: " + "; ".join(reasons),
            "mode": "strict",
            "runtime": runtime_harness(os.environ.get("DOCMESH_RUNTIME")),
            "capabilities_proven": True,
        }
    unknown = capabilities.get("unknown_surfaces") or ["runtime capability proof"]
    return {
        "decision": "advisory",
        "reason": "DocMesh advisory: " + "; ".join(reasons),
        "mode": mode,
        "runtime": runtime_harness(os.environ.get("DOCMESH_RUNTIME")),
        "capabilities_proven": proven,
        "unknown_surfaces": unknown,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DocMesh Stop enforcement hook")
    parser.add_argument("--project-root", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"DocMesh Stop hook ignored malformed payload: {exc}", file=sys.stderr)
        return 0
    if not isinstance(payload, Mapping):
        payload = {}
    result = evaluate(payload, args.project_root)
    if result is None:
        return 0
    if result.get("decision") == "block":
        # Claude Code and Codex both consume this hook decision shape.  Keep
        # source content out of the response: only controlled diagnostics are
        # emitted.
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
