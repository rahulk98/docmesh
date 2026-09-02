# Changelog

All notable changes to DocMesh are listed here, grouped by version. A new
release bumps the mirrored version (see the Versioning section in
AGENTS.md) and adds its own `## [x.y.z]` heading below.

## [Unreleased]

## [1.0.3] - 2026-09-02

- When the resolved Python 3.12+ interpreter is missing DocMesh's
  dependencies, `pyresolve.py` now bootstraps them by running
  `uv sync --extra test` in the plugin root, then re-checks before
  falling back to an error. This fixes the MCP server never starting on
  a fresh install where the plugin's `.venv` was never created (`uv sync`
  had not been run manually), the common case on machines whose system
  `python3` is old (e.g. macOS's 3.9).
- The bootstrap only runs from explicit, user-initiated entrypoints (the
  MCP server, `entrypoint.py`). Hooks (`post-edit.py`, `stop.py`) and the
  detached indexing worker pass `bootstrap=False` and keep failing fast
  instead, preserving the offline contract that hooks never touch the
  network.

## [1.0.2] - 2026-09-02

- Fixed `scripts/pyresolve.py` resolving `plugins/docmesh/.venv/bin/python`
  to its symlink target (the bare uv-managed interpreter) instead of the
  venv path itself, which bypassed the venv's site-packages and made the
  MCP server (launched via `python3`) unable to find `fastembed`, `pypdf`,
  or `sqlite-vec` even when the venv was fully set up.
- `pyresolve.py` now checks the resolved interpreter for these packages
  and fails with a clear message pointing at `uv sync --extra test`
  instead of re-executing into an interpreter that crashes with a raw
  `ModuleNotFoundError`.
- Discovery (`setup`/`init` dry runs) now prunes excluded directories
  (`.venv`, `dist/`, `node_modules`, `__pycache__`, `*.egg-info`, etc.) and
  records each one once instead of emitting an entry per contained file.
  Reports on repositories with virtual environments went from megabytes
  to a few kilobytes, with no change to which sources are included.
- Added `CHANGELOG.md`.

## [1.0.1] - 2026-09-02

- Every entrypoint (shell launchers, MCP server, hooks, detached worker)
  now resolves a Python 3.12+ interpreter through `scripts/pyresolve.py`
  and re-executes under it when the system `python3` is older (macOS
  ships 3.9). The MCP configs still name `python3`; resolution is
  automatic and never downloads anything.
- Bumped the mirrored version to 1.0.1 so Codex and Claude Code caches
  re-sync the fixed plugin package (pyproject, plugin manifests,
  marketplace metadata, MCP `serverInfo`, `uv.lock`, manifest tests).
- Added `AGENTS.md` with repository layout, commands, and invariants.

## [1.0.0] - 2026-09-02

- Initial release: local, offline-capable document retrieval and
  consistency plugin for Codex and Claude Code.
- Indexes Markdown, MDX, LaTeX, BibTeX, plain text, and text-bearing
  PDFs with FTS5/BM25 + `sqlite-vec` hybrid search and
  `BAAI/bge-small-en-v1.5` embeddings.
- Precision `search`, exhaustive `find`, source-validated `read`, and
  the recall-first `docmesh-global-edit` workflow with immutable
  discovery baselines and verification.
- Durable post-edit hooks with a detached indexing worker, query-time
  reconciliation, and advisory-or-proven-strict Stop enforcement.
- Untrusted-document trust boundary: document text stays in
  `untrusted_document_content`; tool-controlled metadata in
  `trusted_metadata`.
- Project README rewritten into structured quick-start and usage docs.