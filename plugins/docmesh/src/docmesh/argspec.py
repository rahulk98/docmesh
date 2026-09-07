"""Dependency-free argparse spec shared by the package CLI and the launcher.

This module must never import anything beyond the standard library (no
``.api``, ``.config``, etc.) so the harness (``scripts/entrypoint.py``) can
load it without pulling in fastembed/pypdf/sqlite-vec. It is loaded two ways:
normally, via ``from .argspec import ...`` inside the ``docmesh`` package
(``cli.py``); and by direct file path (bypassing ``docmesh/__init__.py``,
which does import the core) from the dependency-free launcher.
"""

from __future__ import annotations

import argparse

OPERATIONS = (
    "setup",
    "init",
    "index",
    "status",
    "doctor",
    "probe-hooks",
    "probe_hooks",
    "search",
    "find",
    "read",
    "bench",
    "impact-start",
    "impact_start",
    "impact-page",
    "impact_page",
    "impact-read",
    "impact_read",
    "impact-classify",
    "impact_classify",
    "impact-finish",
    "impact_finish",
)


def add_common_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Add every op-specific flag DocMesh operations accept.

    Shared verbatim by ``docmesh.cli`` and the plugin launcher so every flag
    reachable from the package CLI is reachable from the launcher too.
    """

    parser.add_argument("--project-root", "--root", default=".")
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--approve", "--yes", action="store_true")
    parser.add_argument("--detailed", action="store_true")
    parser.add_argument("--deterministic", action="store_true")
    parser.add_argument("--use-fastembed", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--model", default=None)
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--download-model", dest="download_model", action="store_true")
    parser.add_argument(
        "--no-download-model", dest="download_model", action="store_false"
    )
    parser.set_defaults(download_model=None)
    parser.add_argument("--paths", nargs="*", default=None)
    parser.add_argument("--query", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--snippet-only", action="store_true")
    parser.add_argument("--max-snippet-length", type=int, default=None)
    parser.add_argument("--pattern", default=None)
    parser.add_argument("--mode", default=None)
    parser.add_argument("--cursor", default=None)
    parser.add_argument("--path", default=None)
    parser.add_argument("--start-line", type=int, default=None)
    parser.add_argument("--end-line", type=int, default=None)
    parser.add_argument("--page", type=int, default=None)
    parser.add_argument("--phase", default=None)
    parser.add_argument("--query-bundle", default=None)
    parser.add_argument("--source-roles", "--roles", nargs="*", default=None)
    parser.add_argument("--no-snippet-only", dest="snippet_only", action="store_false")
    parser.set_defaults(snippet_only=None)
    parser.add_argument("--scope", default=None)
    parser.add_argument("--page-size", type=int, default=None)
    parser.add_argument("--baseline-run-id", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--candidate-id", default=None)
    parser.add_argument("--context-lines", type=int, default=None)
    parser.add_argument("--decisions", default=None)
    parser.add_argument("--queries", default=None)
    return parser
