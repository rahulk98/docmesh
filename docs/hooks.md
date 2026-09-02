# Hook lifecycle and enforcement

## Post-edit event contract

`hooks/post-edit.py` accepts a Claude Code or Codex JSON hook payload on stdin.
It extracts path-shaped fields (`file_path`, `path`, `files`, and their common
variants) from tool metadata, normalizes them to absolute paths, and filters to
the V1 source suffixes (`.md`, `.mdx`, `.tex`, `.bib`, `.txt`, `.pdf`).
`.docmesh` paths are excluded. Missing files are retained so deletions can be
reconciled. Document contents are never parsed by the hook.

For every non-empty event it creates or appends
`.docmesh/harness/dirty-events.jsonl`. The record has schema version 1, a UUID,
UTC timestamp, normalized project root, sorted absolute files, runtime/tool
metadata, and a durability marker. The append holds an advisory lock, flushes
and fsyncs the file, then fsyncs the containing directory. A detached worker is
started only after that append returns. Set `DOCMESH_NO_WORKER=1` in fixtures
that need to inspect the queue without a child process.

`scripts/worker.py` obtains a non-blocking project lock, coalesces every
unacknowledged event, invokes core `index(paths=..., changed_paths=...,
incremental=True)`, and appends acknowledgements only after success. A failed
index writes `worker-errors.jsonl` and leaves the original event pending. The
worker sets `DOCMESH_WORKER=1` and `DOCMESH_HOOK_SUPPRESS=1`, so its own writes
cannot recursively enqueue more work.

## Freshness reconciliation

MCP query calls and `reconcile.sh` run the same worker once. A missing or failed
event therefore remains visible and is retried; no query silently asserts a
fresh index. Worker diagnostics are kept in harness state and do not replace
the core index/status source of truth.

## Stop contract

`hooks/stop.py` reads `.docmesh/local.toml` (`[enforcement] mode =
"advisory"|"strict"`) and the latest core/harness status snapshot. A clean
verification for the current edit generation emits nothing. An unverified or
dirty state emits a controlled advisory warning and exits successfully unless
strict mode has a proven runtime capability entry. In proven strict mode it
prints a JSON `{ "decision": "block", "reason": ... }` response; it still
exits 0 because the harness consumes the decision response.

Strict mode requires all of these facts in the cache entry: plugin installed,
both hook classes present, hook trusted, Stop dispatched, blocking respected,
and loop protection. The cache key includes harness, runtime, runtime version,
transport, and SHA-256 hook-definition hash. A static manifest alone cannot
prove runtime behavior. Unknown surfaces are advisory by design.

Stop re-entry is guarded in three layers: worker environment suppression,
the runtime's `stop_hook_active` signal, and a short-lived persistent invocation
counter that allows the hook after a repeated burst. This prevents a blocked
Stop hook from trapping a session in an infinite loop.
