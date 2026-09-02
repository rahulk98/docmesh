# DocMesh plugin contract

The shared package lives at `plugins/docmesh/` and contains Codex and Claude
manifests, runtime-specific MCP configuration for one shared stdio server,
three workflow skills, and harness scripts.
Both runtimes point at the same skill files and the same core public
operations. Skills are intentionally kept inside the plugin so uninstalling it
does not mutate a global or project skill directory.

## Public entrypoints

The shell launchers are thin, offline-safe wrappers around
`scripts/entrypoint.py`:

| Launcher | Operation |
| --- | --- |
| `setup.sh` | dry-run/approved setup and corpus discovery |
| `index.sh` | full or supplied-path indexing |
| `docmesh` | any public operation |
| `doctor.sh` | core diagnostics plus optional hook diagnostics |
| `probe-hooks.sh` | runtime/trust capability probe and cache |
| `reconcile.sh` | drain the dirty queue once |

The launcher delegates to the installed core Python API when available. A
`DOCMESH_CORE_COMMAND` adapter is supported for subprocess tests and alternate
packaging; it receives one JSON request on stdin and the operation name as an
argument. A normal installation falls back to `python -m docmesh`. Core errors
are returned as JSON and never masquerade as a successful index.

## MCP surface

The stdio server exposes setup, init, index, status, doctor, probe-hooks,
search, find, read, and all impact lifecycle operations. It emits no logs on
stdout. Notifications receive no response, malformed requests receive a JSON-
RPC error, and tool failures use `isError` while retaining a structured text
result.

Before search/find/read/impact/status calls, the server drains pending dirty
events unless `DOCMESH_NO_RECONCILE=1` is set by a test harness. This is a
freshness check, not permission to make edits.

## Source trust boundary

Only hook metadata supplies changed paths. The harness never scans arbitrary
tool output for commands, and the MCP adapter never interprets document text.
Source snippets, passages, and extracted PDF text are placed under
`untrusted_document_content`; paths, roles, line/page coordinates, hashes,
scores, cursors, and classifications remain under `trusted_metadata`.
