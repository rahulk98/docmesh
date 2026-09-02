#!/usr/bin/env python3
"""Synchronous DocMesh PostToolUse hook.

The hook's critical section is deliberately tiny: parse harness metadata,
append and fsync one dirty-file event, then detach the worker.  It does not
index inline, download dependencies, inspect document text, or fail the
user's edit when the optional worker is unavailable.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from pyresolve import ensure_python

ensure_python()

from harness import (
    HarnessPaths,
    append_dirty_event,
    extract_changed_paths,
    project_root,
)


def load_payload() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    value = json.loads(raw)
    return value if isinstance(value, dict) else {}


def detach_worker(root: Path) -> int | None:
    """Start one detached, loop-suppressed worker and return its pid."""

    if os.environ.get("DOCMESH_NO_WORKER") or os.environ.get("DOCMESH_WORKER"):
        return None
    worker = SCRIPT_DIR / "worker.py"
    paths = HarnessPaths.for_project(root)
    paths.ensure()
    log = paths.worker_log.open("a", encoding="utf-8")
    environment = {
        **os.environ,
        "DOCMESH_WORKER": "1",
        "DOCMESH_HOOK_SUPPRESS": "1",
        "DOCMESH_OFFLINE": "1",
        "DOCMESH_NO_NETWORK": "1",
        "DOCMESH_PROJECT_ROOT": str(root),
    }
    try:
        child = subprocess.Popen(
            [sys.executable, str(worker), "--project-root", str(root), "--once"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            cwd=root,
            env=environment,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        log.write(f"DocMesh worker launch failed: {exc}\n")
        log.flush()
        log.close()
        return None
    # The child owns the inherited descriptor.  Closing our copy is important
    # for the hook to return immediately to the harness.
    log.close()
    return child.pid


def main() -> int:
    try:
        payload = load_payload()
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"DocMesh hook ignored malformed payload: {exc}", file=sys.stderr)
        return 0
    root = project_root(payload.get("cwd"))
    changed = extract_changed_paths(payload, root)
    if not changed:
        return 0
    try:
        event = append_dirty_event(changed, project=root, payload=payload)
        detach_worker(root)
    except (OSError, ValueError) as exc:
        # A PostToolUse hook must never turn a successful user edit into a
        # failed tool call.  The failure remains visible for diagnostics.
        print(f"DocMesh could not record dirty files: {exc}", file=sys.stderr)
        return 0
    if os.environ.get("DOCMESH_HOOK_VERBOSE"):
        print(
            json.dumps(
                {"event_id": event["event_id"], "files": event["files"]}, sort_keys=True
            ),
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
