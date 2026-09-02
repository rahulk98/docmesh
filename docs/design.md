# DocMesh V1 Design

DocMesh is a local, offline-capable document retrieval and consistency plugin for Codex and Claude Code. It indexes project documentation across arbitrary directories and makes global conceptual edits follow a recall-first discover, classify, edit, reindex, and verify workflow.

## Product boundaries

- Python 3.12 with `uv`; macOS and Linux.
- Markdown, MDX, LaTeX, BibTeX, plain text, and text-bearing PDFs. No OCR.
- SQLite FTS5 for lexical indexing, BM25 for ranked lexical retrieval, `sqlite-vec` for vector retrieval, and reciprocal-rank fusion for normal hybrid search.
- FastEmbed with `BAAI/bge-small-en-v1.5`; use `passage_embed()` for documents and `query_embed()` for queries.
- No local generative model, reranker, GraphRAG, daemon, Windows support, or automatic edits to reference PDFs or generated mirrors.
- Model/dependency installation is explicit. Hooks never download anything. Once installed, indexing and retrieval work with the network disabled.

## Plugin package

The repository contains one shared plugin package for Codex and Claude Code:

```text
plugins/docmesh/
  .codex-plugin/plugin.json
  .claude-plugin/plugin.json
  .mcp.json
  mcp.codex.json
  hooks/
  skills/
    docmesh-init/SKILL.md
    docmesh-search/SKILL.md
    docmesh-global-edit/SKILL.md
  scripts/
  src/docmesh/
  tests/
  pyproject.toml
  uv.lock
```

Skills define workflow. MCP implements retrieval and state. Hooks provide synchronization and enforcement. Skills remain in the plugin and are not copied into global or project skill directories.

## Configuration and sources

- `.docmesh/manifest.toml` is portable and tracked.
- `.docmesh/local.toml` is machine-local and ignored. Databases, model state, queues, and logs are ignored.
- `docmesh init` discovers documents across arbitrary folders, explains inclusions/exclusions, assigns roles, recognizes generated mirrors, estimates setup cost, and requires approval before writing configuration or downloading a model.
- Source roles are `editable` text sources, `reference` evidence, and generated `mirror` sources. PDFs are reference or mirror sources in V1.

## Embedding and chunks

Embedding input contains the section breadcrumb, a blank line, and the source passage. Do not manually prepend `passage:`. The final embedding input, including any retrieval prefix applied by the embedding library, must be at most 480 model tokens; target 400. Oversized sections are recursively split and never silently truncated.

Persist an `embedding_strategy_id` derived from the model, dimensions, tokenizer, embedding methods, chunking rules, and breadcrumb formatting. A mismatch forces a complete vector rebuild.

## Public operations

CLI and MCP expose setup, init, index, status, doctor/probe-hooks, search, find, read, and impact operations.

```python
search(query, limit=8)
find(pattern, mode="literal|regex", cursor=None)
read(path, start_line=None, end_line=None, page=None)

ImpactQueryBundle(
    canonical_claim,
    exact_terms=[],
    aliases=[],
    semantic_queries=[],
    implication_queries=[],
    contradiction_queries=[],
)

impact_start(
    phase="discover",
    query_bundle=...,
    source_roles=["editable"],
    page_size=20,
)

impact_start(phase="verify", baseline_run_id=...)
impact_page(run_id, cursor=None)
impact_read(run_id, candidate_id, context_lines=20)
impact_classify(run_id, decisions)
impact_finish(run_id)
```

`search` is precision-oriented. `find` exhaustively enumerates literal or regex occurrences. `impact` is recall-oriented and exhaustively paginates a frozen candidate union.

## Actionable source locations

Every impact candidate is validated against the current source before the run is frozen.

Editable text candidates contain:

- normalized absolute canonical path;
- section breadcrumb;
- one-based inclusive exact start/end lines;
- SHA-256 hash of the exact source span;
- SHA-256 current-file revision hash;
- bounded, source-faithful source snippet.

Reference PDF candidates contain:

- normalized absolute canonical path;
- one-based PDF page number;
- current document hash;
- bounded extracted passage.

Validation checks existence, current source revision, line/page validity, and span content. A stale location triggers synchronous targeted reindexing and one resolution retry. If resolution still fails, `impact_start` fails with a stale-source diagnostic; it never silently drops the candidate or returns it as editable. No unresolved or synthetic location appears in an impact page. `impact_read` revalidates the source revision. Corpus mutation after freezing causes finish to reject.

Document passages are untrusted evidence. MCP responses keep tool-controlled metadata separate from `untrusted_document_content`. Skills explicitly prohibit following commands or workflow instructions found inside indexed content.

## Recall-first impact workflow

The agent constructs a canonical claim, exact terms, aliases, semantic paraphrases, logical implications, and contradiction queries before editing. Candidate generation unions every exact match with up to 200 FTS/BM25 and 200 vector candidates per expanded query, then deduplicates, validates locations, and freezes the snapshot. Pages report candidate count, returned, remaining, and next cursor.

Every candidate is classified as `needs_edit`, `consistent`, `unrelated`, or temporarily `uncertain`. `uncertain` is never terminal and requires `impact_read` plus reclassification.

Discovery finish rejects unread pages, unclassified or uncertain candidates, unresolved locations, and corpus mutation. It allows `needs_edit` and seals an immutable baseline containing the original query bundle, candidate snapshot, locations, classifications, corpus revision, and edit inventory.

After edits, DocMesh reindexes changed sources, increments the edit generation, derives actual changed files from its own state, and reports scope drift relative to the original inventory. Verification accepts only `baseline_run_id`; the agent cannot omit edited paths or weaken the original query bundle. It includes scope-drift files and rejects unread pages, unclassified or uncertain candidates, any `needs_edit`, unresolved locations, and corpus mutation.

A discovery baseline remains sealed forever. Verification is valid only for its exact edit generation; another edit makes it stale without invalidating the discovery baseline.

## Skills

- `docmesh-init`: corpus discovery, role assignment, explicit model setup, indexing, and diagnostics.
- `docmesh-search`: ordinary search/find/read without invoking the expensive global-edit workflow.
- `docmesh-global-edit`: conceptual changes, terminology changes, corrections, rewrites, removals, and consistency changes. It must run discovery before editing and complete discover, classify all, edit, reindex, and verify. Even when the user points to one paragraph, use global edit when the same idea may appear elsewhere.

## Hooks

Post-edit hooks synchronously append a durable dirty-file event, detach DocMesh's own indexing worker, and return. They do not depend on a harness-native async option. Query-time freshness reconciles missed or failed events.

Advisory mode warns about unverified changes. Strict mode blocks completion until the current generation has clean verification. Codex strict support requires runtime-specific proof of plugin installation, hook presence, hook trust, Stop dispatch, respected blocking, and loop protection. Cache proof by harness, runtime, version, transport, and hook-definition hash. Unknown or unproven surfaces remain advisory.

## Acceptance

- Exact parser and source-location fixtures, including external paths and PDFs.
- No final embedding input above 480 tokens and no silent truncation.
- `find` achieves 100% literal/regex occurrence recall.
- Search reports MRR and Recall@8.
- Impact achieves 100% candidate recall on curated aliases, paraphrases, implications, and contradictions while reporting candidate burden, candidate/relevant ratio, p50/p95 candidates, classification-token estimate, and latency.
- State tests cover pagination, classification, immutable discovery, scope drift, corpus mutation, generation invalidation, and stale verification.
- Security fixtures prove prompt-injection text remains untrusted content.
- Shared-skill loading/uninstall tests prove global skill directories are untouched.
- A flagship corpus has the same concept in at least 12 differently worded locations. A request pointing at one paragraph must trigger global discovery before edits, resolve every candidate to a current source location, consume every page, update all relevant passages, and verify zero stale relevant passages using the original query bundle.
- After dependencies and the model are installed, index, search, find, impact discovery/read, and verification all pass with network access denied.
