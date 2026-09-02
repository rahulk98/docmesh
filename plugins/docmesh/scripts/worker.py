#!/usr/bin/env python3
"""One-shot detached dirty-event worker.

Workers coalesce pending events, invoke the core incremental index operation,
and acknowledge events only after a successful return.  A process lock makes
multiple PostToolUse hooks harmless; failed work remains durable for the next
reconciliation.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from pyresolve import ensure_python

ensure_python()

from harness import (
    HarnessPaths,
    acknowledge_events,
    append_jsonl,
    atomic_write_json,
    core_call,
    pending_events,
    read_state,
    record_core_result,
    try_file_lock,
    utc_now,
)


def _set_worker_state(paths: HarnessPaths, update: dict[str, Any]) -> None:
    current = read_state(paths.project)
    current.update(update)
    current["harness_updated_at"] = utc_now()
    with contextlib.suppress(OSError):
        atomic_write_json(paths.state, current)


def run_once(root: Path | str | None = None) -> dict[str, Any]:
    """Drain one project's queue once and return a diagnostic record."""

    os.environ.setdefault("DOCMESH_OFFLINE", "1")
    os.environ.setdefault("DOCMESH_NO_NETWORK", "1")
    paths = HarnessPaths.for_project(root)
    # A read-only query in an uninitialized project should not create local
    # state merely to discover that there is no queue to drain.
    if not any(source.is_file() for source in paths.event_sources):
        return {"ok": True, "status": "empty", "project_root": str(paths.project)}
    paths.ensure()
    with try_file_lock(paths.worker_lock) as acquired:
        if not acquired:
            return {
                "ok": True,
                "status": "already_running",
                "project_root": str(paths.project),
            }
        events = pending_events(paths)
        if not events:
            _set_worker_state(
                paths, {"pending_events": 0, "last_worker_status": "empty"}
            )
            return {"ok": True, "status": "empty", "project_root": str(paths.project)}

        files = sorted(
            {
                str(item)
                for event in events
                for item in event.get("files", [])
                if isinstance(item, str)
            }
        )
        event_ids = [
            str(event["event_id"]) for event in events if event.get("event_id")
        ]
        result = core_call(
            "index",
            {
                "paths": files,
                "changed_paths": files,
                "incremental": True,
                "event_ids": event_ids,
            },
            project=paths.project,
        )
        if result.get("ok"):
            acknowledge_events(paths, event_ids)
            record_core_result(paths.project, "index", result)
            _set_worker_state(
                paths,
                {
                    "pending_events": 0,
                    "last_worker_status": "indexed",
                    "last_indexed_files": files,
                    "last_indexed_at": utc_now(),
                },
            )
            return {
                "ok": True,
                "status": "indexed",
                "project_root": str(paths.project),
                "event_ids": event_ids,
                "files": files,
                "core": result,
            }

        error = {
            "schema_version": 1,
            "event_type": "worker_error",
            "created_at": utc_now(),
            "project_root": str(paths.project),
            "event_ids": event_ids,
            "files": files,
            "error": result.get("error", "core index failed"),
            "core": result,
        }
        with contextlib.suppress(OSError):
            append_jsonl(paths.root / "worker-errors.jsonl", error)
        _set_worker_state(
            paths,
            {
                "pending_events": len(events),
                "last_worker_status": "failed",
                "last_worker_error": error["error"],
            },
        )
        return {"ok": False, "status": "failed", **error}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Drain DocMesh dirty-file events")
    parser.add_argument("--project-root", default=None)
    parser.add_argument(
        "--once",
        action="store_true",
        help="compatibility flag; workers are always one-shot",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = run_once(args.project_root)
    if args.json or not result.get("ok"):
        print(__import__("json").dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
