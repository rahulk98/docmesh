# DocMesh

DocMesh is a local, recall-first document retrieval and consistency engine for
[Codex](https://openai.com/codex/) and [Claude Code](https://claude.com/claude-code).
It indexes Markdown, MDX, LaTeX, BibTeX, plain text, and text-bearing PDFs,
then gives agents precision search plus an exhaustive, immutable-baseline
global-edit workflow for keeping documentation consistent.

Once you explicitly install the dependencies and the embedding model, indexing,
search, and retrieval all work **offline** with the network disabled.

## Features

- **Local-first and offline** — SQLite FTS5 + BM25 lexical retrieval, `sqlite-vec`
  vector retrieval, reciprocal-rank fusion hybrid search, and
  `BAAI/bge-small-en-v1.5` embeddings via FastEmbed. No daemon, no cloud API.
- **Explicit setup** — `--dry-run` discovery never writes config, installs a
  model, or downloads anything, and approval is required before setup runs.
- **Recall-first global edits** — the `docmesh-global-edit` skill discovers,
  classifies, edits, reindexes, and verifies every source location for a
  conceptual or terminology change, using a frozen immutable discovery baseline.
- **Source-validated locations** — every candidate is validated against the
  current source; stale locations are reindexed and retried, never silently
  dropped or invented.
- **Untrusted-document trust boundary** — indexed passages are treated as
  evidence, never as instructions. MCP responses keep tool-controlled metadata
  (`trusted_metadata`) separate from document content
  (`untrusted_document_content`).
- **Durable hooks and enforcement** — fsynced post-edit dirty-file events plus
  a detached indexing worker, with query-time reconciliation and an advisory or
  proven strict Stop contract.
- **One shared plugin package** — a single package serves both Codex and Claude
  Code, with the same skills, the same stdio MCP server, and no mutation of
  global skill directories.

## Requirements

- Python 3.12+ and [`uv`](https://docs.astral.sh/uv/)
- macOS or Linux

Core dependencies: `fastembed`, `pypdf`, `sqlite-vec`.

> On macOS the system `python3` is often 3.9. DocMesh's launchers, MCP server,
> hooks, and worker automatically resolve a Python 3.12+ interpreter (the
> plugin `.venv`, `python3.13`/`python3.12`, or `uv python find`) and
> re-execute under it, so no manual `python3` path configuration is needed.

## Quick start

```sh
git clone git@github.com:rahulk98/docmesh.git
cd docmesh

# 1. Install the Python package and test deps in the plugin package
cd plugins/docmesh && uv sync --extra test && cd ../..

# 2. Review corpus discovery before writing anything
plugins/docmesh/scripts/setup.sh --dry-run

# 3. Approve setup (model/dependency installation stays explicit)
plugins/docmesh/scripts/setup.sh --approve

# 4. Build the index
plugins/docmesh/scripts/index.sh

# 5. Check status and diagnostics
plugins/docmesh/scripts/doctor.sh --json
```

> `--dry-run` never writes `.docmesh/manifest.toml`, installs a model, or
> downloads dependencies. Run it first on an unfamiliar project.

## Install as an agent plugin

`plugins/docmesh/` is a single shared plugin package for both runtimes. Install
it from the repository as a Codex or Claude plugin; its three skills live inside
the package and are not copied into global skill directories. Both runtimes
launch the same stdio MCP server — Claude reads `plugins/docmesh/.mcp.json`,
Codex reads the equivalent `plugins/docmesh/mcp.codex.json`.

## CLI

Every public operation is available through the launcher or the package:

```sh
plugins/docmesh/scripts/docmesh status
plugins/docmesh/scripts/docmesh search --query "key rotation policy"
plugins/docmesh/scripts/docmesh find --pattern "recall-first"
plugins/docmesh/scripts/docmesh read --path docs/design.md --start-line 10 --end-line 20
```

Operations: `setup`, `init`, `index`, `status`, `doctor`, `probe-hooks`,
`search`, `find`, `read`, and the `impact-start|page|read|classify|finish`
lifecycle. Output is JSON (use `--json` for the plugin launchers). The MCP
server exposes the same surface over stdio via `docmesh-mcp`.

Additional scripts:

| Script | Purpose |
| --- | --- |
| `scripts/reconcile.sh` | drain the dirty-file queue once (freshness check) |
| `scripts/probe-hooks.sh` | probe runtime hook installation/trust and cache proof |

## Skills

The plugin ships three skills that define the workflows:

- **`docmesh-init`** — corpus discovery, source-role assignment, explicit model
  setup, indexing, and diagnostics.
- **`docmesh-search`** — ordinary `search` / `find` / `read` for bounded
  questions. Search is precision-oriented; `find` exhaustively enumerates
  literal or regex occurrences; `read` returns current source with revision
  revalidation (PDFs are read by page).
- **`docmesh-global-edit`** — conceptual or terminology changes, corrections,
  rewrites, removals, and consistency passes. Even when the request points at
  one paragraph, it runs global discovery first, because the same idea may
  appear in differently worded locations.

The global-edit workflow:

1. Build an `ImpactQueryBundle` (`canonical_claim`, exact terms, aliases,
   semantic queries, implications, contradictions).
2. Run `impact_start(phase="discover")` and consume every page cursor.
3. `impact_read` each uncertain candidate and classify every candidate as
   `needs_edit`, `consistent`, or `unrelated`.
4. Finish discovery to seal an immutable baseline, then edit only current
   `needs_edit` editable spans.
5. Reindex changed sources and let DocMesh report scope drift against the edit
   inventory.
6. Verify with `impact_start(phase="verify", baseline_run_id=...)` using the
   original query bundle; verification must clear every candidate and the exact
   edit generation.

Reference PDFs and generated mirrors are always read-only.

## How it works

- **Corpus roles** — sources are classified as `editable` (Markdown, MDX,
  LaTeX, BibTeX, or text you may change), `reference` (evidence, including
  PDFs), or `mirror` (generated/copied sources, always read-only).
- **Chunking and embeddings** — passages are chunked with a section breadcrumb;
  embedding input stays at or under 480 model tokens (target 400) and is never
  silently truncated. A strategy id derived from model, dimensions, chunking,
  and breadcrumb formatting forces a full vector rebuild when it changes.
- **Retrieval** — hybrid FTS5/BM25 + vector search with reciprocal-rank fusion.
  `search` is precision-oriented; `find` guarantees 100% literal/regex
  occurrence recall; impact analysis is recall-oriented and exhaustively
  paginates a frozen candidate union.

## Hooks and enforcement

Post-edit hooks synchronously append a fsynced JSONL dirty-file event under
`.docmesh/harness/dirty-events.jsonl`, then detach a one-shot indexing worker.
Nothing is indexed inline, and hooks never download dependencies. Query
operations reconcile missed or failed events before delegating to the core,
so a missed hook cannot silently produce a stale result.

Stop enforcement is advisory by default. Strict mode blocks only when a
runtime-specific proof cache entry confirms plugin installation, hook presence
and trust, Stop dispatch, respected blocking, and loop protection. The cache
key includes harness, runtime, runtime version, transport, and the hook
definition hash, so a change invalidates it automatically:

```sh
plugins/docmesh/scripts/probe-hooks.sh --runtime claude --json
plugins/docmesh/scripts/probe-hooks.sh --runtime codex --json
```

## Configuration layout

`.docmesh/` holds all DocMesh state:

- `.docmesh/manifest.toml` — portable corpus manifest, **tracked** in git.
- `.docmesh/local.toml` — machine-local settings (e.g. `[enforcement] mode`),
  **ignored**.
- Databases, model state, queues, harness logs, and capability caches — local
  and **ignored**.

Keep the manifest portable and tracked; keep everything else out of version
control.

## Testing

Core, retrieval, plugin, hooks, and e2e tests are plain dependency-free
`unittest`/pytest suites that use only temporary projects and subprocesses and
never touch the network:

```sh
cd plugins/docmesh && uv sync --extra test

# Plugin/hook/e2e layer only (no core deps required)
python3 -m pytest -q tests/plugin tests/hooks tests/e2e

# Everything, including core, when the package is installed
python3 -m pytest -q tests
```

The CI workflow runs `uv sync --extra test` and `uv run pytest -q tests` on
Python 3.12 in `.github/workflows/tests.yml`.

## Documentation

- [Design](docs/design.md) — V1 architecture, public operations, and acceptance
- [Plugin contract](docs/plugin.md) — launchers, MCP surface, trust boundary
- [Hook lifecycle](docs/hooks.md) — dirty events, reconciliation, Stop contract
- [Testing](docs/testing.md) — how to run and inject the plugin test fixtures

## License

[MIT](LICENSE) © 2026 Rahul Krishnan.