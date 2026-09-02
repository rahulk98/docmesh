# Testing the plugin layer

The plugin/harness tests are dependency-free Python `unittest`/pytest tests
and use only temporary projects and subprocesses. They cover manifest and
skill loading, JSONL durability/path normalization, detached-worker contracts,
advisory/strict enforcement, loop protection, cache invalidation, MCP JSON-RPC,
metadata/content separation, and offline launcher behavior.

From the repository root:

```sh
python3 -m pytest -q plugins/docmesh/tests/plugin plugins/docmesh/tests/hooks plugins/docmesh/tests/e2e
python3 -m pytest -q plugins/docmesh/tests
```

The second command includes core tests when the core package is installed. On a
minimal checkout without core dependencies, the harness tests still run; core
delegation failures are deliberately represented as structured diagnostics.

For an injected core fixture, set `DOCMESH_CORE_COMMAND` to a command that
accepts the operation name and JSON stdin. For runtime proof fixtures, set
`DOCMESH_PLUGIN_INSTALLED`, `DOCMESH_HOOK_TRUSTED`,
`DOCMESH_STOP_DISPATCH_PROOF`, `DOCMESH_BLOCKING_PROOF`, and
`DOCMESH_LOOP_PROTECTION_PROOF`; production adapters should write equivalent
proof only after observing the actual runtime behavior. No test or hook is
allowed to download a model or require network access.
