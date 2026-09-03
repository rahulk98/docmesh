# Changelog

All notable changes to DocMesh are listed here, grouped by version. A new
release bumps the mirrored version (see the Versioning section in
AGENTS.md) and adds its own `## [x.y.z]` heading below.

## [1.1.1] - 2026-09-03

- `find` no longer returns the entire page text for every PDF match; each
  match now carries only the matched line and a match-centered snippet,
  cutting worst-case output from ~1MB to a few hundred bytes per match (#6).
- `find` accepts a `scope` path prefix (relative or absolute, directory
  boundaries respected) to restrict enumeration to a subtree; previously the
  argument was silently ignored. Also exposed as `--scope` on the CLI.

## [1.1.0] - 2026-09-03

- Every MCP tool result is now bounded by a total output budget (16000 chars
  by default, `DOCMESH_MAX_RESULT_CHARS` to tune). Oversized results trim
  trailing list entries first, then over-long strings, and carry explicit
  `truncated`/`omitted` markers instead of flooding the caller's context.
- MCP results report paths relative to the project root (the root itself is
  included once as `project_root`).
- `search` collapses results from the same file with overlapping line ranges,
  keeping the higher-ranked one and backfilling further candidates.
- `doctor` detects a stale MCP server: it compares the running server version
  against the newest installed plugin version in the Claude/Codex plugin
  caches and advises a restart when the server is older.
- New `docmesh bench --queries <file>` CLI command: runs a JSON query set
  against the index and reports mean reciprocal rank, recall@8, and per-query
  hit ranks for retrieval tuning.
- New `docmesh-latex-check` skill: LaTeX/BibTeX consistency recipes (dangling
  `\ref`s, unused labels, undefined or uncited citation keys, duplicate
  labels and bib keys) driven by `find` regex enumeration.
- Rewrote the `impact_*` tool and `docmesh-global-edit` skill descriptions in
  plain task language ("batch edit that can't miss a spot") and added a
  redirect from `search` to `impact_start` for multi-location edits, so
  agents actually reach for the exhaustive workflow when it applies.

## [1.0.5] - 2026-09-03

- `search`/`find`/`read` locations no longer emit the legacy alias keys
  (`path`, `breadcrumb`, `page`, `span_hash`, `source_span_hash`,
  `file_hash`, `revision_hash`, `snippet`) alongside the canonical ones;
  this roughly halves per-result wire size. Readers still accept the older
  persisted shapes.
- `search` results no longer repeat the snippet text in both `text` and
  `location` when they're identical.
- `setup`/`init` (CLI and MCP) now return a summarized discovery report by
  default: counts by role/format and exclusion reason, estimated size, model
  status, and bounded sample lists instead of every file. A project with tens
  of thousands of excluded files no longer produces a 10MB+ JSON tool result;
  pass `--detailed` (`summary: false` on the MCP tool) for the full report.
- The indexer now survives corrupt, truncated, or unparseable sources (for
  example a PDF whose header or stream is broken): it warns, records the path
  and reason in `IndexStatus.skipped_documents`, drops any stale entry for
  that source, and keeps indexing the rest of the corpus instead of failing
  the entire run.
- `search` results now carry concise match-centered snippets (≤200 chars by
  default, `max_snippet_length` to tune) and the MCP `search` tool defaults
  to snippet-only output; the full chunk text remains one flag away
  (`snippet_only: false`).

## [1.0.4] - 2026-09-02

- Indexing memory: embeddings are now computed in bounded batches
  (`EMBED_BATCH_SIZE = 64`) instead of one multi-GB onnxruntime call per
  document or per corpus re-embed. A thesis-sized corpus that peaked at
  ~6.7 GB of RSS now indexes at ~2.2 GB, with identical results; vectors
  are serialized straight to BLOBs instead of 12 KB Python float lists.
- Corpus-wide vector refresh (embedding-strategy change) streams chunks
  lazily in batches and runs in one transaction, so an interrupted
  re-embed can no longer leave the vector tables partially rewritten
  while the strategy metadata already names the new one.
- `status`/revision/hash checks use `COUNT(*)` and lightweight column
  selects instead of loading every document and chunk (full text
  included) into memory; vec0 rehydration compares id sets and streams
  BLOBs instead of materializing all vectors at startup.
- Files are read once during indexing: the bytes read for hashing are
  reused for parsing, and PDFs decode from that in-memory copy instead of
  re-reading the file a second time.

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