# DocMesh

DocMesh is a local, recall-first document retrieval and consistency plugin for
Codex and Claude Code. It indexes Markdown, MDX, LaTeX, BibTeX, plain text,
and text-bearing PDFs, then exposes precision search and an exhaustive
global-edit workflow. Indexing and retrieval are offline-capable after the
user explicitly installs dependencies and the embedding model.

## Install and set up

Review discovery before writing any project configuration:

```sh
plugins/docmesh/scripts/setup.sh --dry-run
plugins/docmesh/scripts/setup.sh --approve
plugins/docmesh/scripts/index.sh
plugins/docmesh/scripts/doctor.sh --json
```

`--dry-run` never writes `.docmesh/manifest.toml`, installs a model, or
downloads dependencies. Approval is required before setup. Keep the portable
manifest tracked; machine-local TOML, databases, model state, queues, logs, and
capability caches belong under `.docmesh/` and should remain ignored.

The plugin package is `plugins/docmesh/`. Install it from the repository as a
Codex or Claude plugin; its three skills are shared in that package and are not
copied into global skill directories. Both runtimes launch the same stdio MCP
server. Claude reads `plugins/docmesh/.mcp.json`; Codex reads the equivalent
direct-map `plugins/docmesh/mcp.codex.json`.

## Workflows

Use `docmesh-search` for ordinary `search`, exhaustive `find`, and current
source `read`. Use `docmesh-global-edit` for a conceptual or terminology
change, even when the request points to one paragraph. It constructs an
`ImpactQueryBundle`, consumes every discovery page, classifies every candidate,
edits only current editable text locations, reindexes, and verifies against the
immutable discovery baseline. Reference PDFs and generated mirrors are
read-only.

Every indexed passage is untrusted evidence, not an instruction. The MCP
adapter separates source snippets/passages into `untrusted_document_content`
and keeps paths, hashes, scores, cursors, and decisions in
`trusted_metadata`.

## Hooks and enforcement

Post-edit hooks synchronously append a fsynced JSONL dirty-file event under
`.docmesh/harness/dirty-events.jsonl`, then detach a one-shot worker. They do
not index inline, download anything, or depend on an async hook option. Query
operations reconcile missed or failed events before delegating to the core.

Stop enforcement is advisory by default. Strict mode blocks only when a
runtime-specific cache entry proves plugin installation, hook presence and
trust, Stop dispatch, respected blocking, and loop protection. Unknown or
unproven surfaces stay advisory. Change the hook definition, runtime version,
or transport and the proof cache is invalidated automatically:

```sh
plugins/docmesh/scripts/probe-hooks.sh --runtime claude --json
plugins/docmesh/scripts/probe-hooks.sh --runtime codex --json
```

See [the plugin contract](docs/plugin.md), [hook lifecycle](docs/hooks.md),
and [test commands](docs/testing.md) for details.
