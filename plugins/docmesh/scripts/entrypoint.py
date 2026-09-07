#!/usr/bin/env python3
"""Offline-safe command launcher for the DocMesh public operations."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from pyresolve import ensure_python

ensure_python()

from capability_probe import probe
from harness import (
    core_call,
    project_root,
    record_core_result,
    to_jsonable,
)
from worker import run_once

# ``argspec.py`` lives inside the ``docmesh`` package but must not be imported
# through it: ``docmesh/__init__.py`` imports the core (fastembed et al.),
# which would break the dependency-free harness contract. Load the module
# directly by file path instead, bypassing the package ``__init__``.
import importlib.util as _importlib_util

_ARGSPEC_PATH = SCRIPT_DIR.parent / "src" / "docmesh" / "argspec.py"
_argspec_spec = _importlib_util.spec_from_file_location(
    "docmesh_argspec", _ARGSPEC_PATH
)
assert _argspec_spec is not None and _argspec_spec.loader is not None
_argspec = _importlib_util.module_from_spec(_argspec_spec)
_argspec_spec.loader.exec_module(_argspec)
add_common_arguments = _argspec.add_common_arguments


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docmesh", description="DocMesh local retrieval and consistency operations"
    )
    parser.add_argument("operation", nargs="?", default="status")
    add_common_arguments(parser)
    # Launcher-only flags not part of the shared docmesh.cli operation set.
    # ``--json`` forces JSON output even for a scalar result; the package CLI
    # (docmesh.cli) always prints JSON already, so it has no flag for this.
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--runtime", default=None)
    parser.add_argument("--plugin-root", default=None)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--test-double", action="store_true")
    return parser


def _arguments(args: argparse.Namespace) -> dict[str, Any]:
    values = vars(args).copy()
    for name in (
        "operation",
        "project_root",
        "json",
        "dry_run",
        "approve",
        "runtime",
        "plugin_root",
        "refresh",
    ):
        values.pop(name, None)
    if values.get("query_bundle"):
        try:
            values["query_bundle"] = json.loads(values["query_bundle"])
        except json.JSONDecodeError as exc:
            raise ValueError(f"--query-bundle must be JSON: {exc}")
    if values.get("decisions"):
        try:
            values["decisions"] = json.loads(values["decisions"])
        except json.JSONDecodeError as exc:
            raise ValueError(f"--decisions must be JSON: {exc}")
    return {key: value for key, value in values.items() if value is not None}


def _print(value: Any, *, as_json: bool) -> None:
    value = to_jsonable(value)
    if as_json or isinstance(value, (dict, list, tuple)):
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    else:
        print(value)


def _setup_arguments(
    args: argparse.Namespace, *, approve: bool, dry_run: bool
) -> dict[str, Any]:
    """Forward explicit setup/model choices without performing setup locally."""

    values: dict[str, Any] = {"approve": approve, "dry_run": dry_run}
    if bool(getattr(args, "detailed", False)):
        values["summary"] = False
    for name in ("model", "cache_dir", "db_path"):
        value = getattr(args, name, None)
        if value is not None:
            values[name] = value
    for name in ("use_fastembed", "deterministic", "test_double", "force"):
        if bool(getattr(args, name, False)):
            values[name] = True
    return values


def execute(args: argparse.Namespace) -> tuple[int, Any]:
    operation = str(args.operation).strip().lower().replace("-", "_")
    root = project_root(args.project_root)
    if operation in {"setup", "init"}:
        if args.dry_run:
            result = {
                "ok": True,
                "dry_run": True,
                "project_root": str(root),
                "message": "Discovery/setup plan only; no configuration, model, or dependency files were written.",
            }
            # If the core has a dry-run discovery operation, include its report
            # while retaining the no-write contract.
            core = core_call(
                operation,
                _setup_arguments(args, approve=False, dry_run=True),
                project=root,
            )
            if core.get("ok"):
                result["core"] = core.get("data")
            return 0, result
        if not args.approve:
            return 2, {
                "ok": False,
                "error": "explicit approval is required; rerun with --approve after reviewing --dry-run",
                "project_root": str(root),
            }
        return _core_result(
            core_call(
                operation,
                _setup_arguments(args, approve=True, dry_run=False),
                project=root,
            )
        )
    if operation == "probe_hooks":
        result = probe(
            root, runtime=args.runtime, plugin=args.plugin_root, refresh=args.refresh
        )
        return 0, result
    if operation == "doctor":
        core = core_call("doctor", {"project_root": str(root)}, project=root)
        hooks = probe(
            root, runtime=args.runtime, plugin=args.plugin_root, refresh=args.refresh
        )
        if core.get("ok"):
            return 0, {"ok": True, "core": core.get("data"), "hooks": hooks}
        return 1, {"ok": False, "core": core, "hooks": hooks}
    if operation == "freshness":
        return _core_result(run_once(root))
    if operation == "worker":
        return _core_result(run_once(root))

    freshness = None
    if operation in {
        "status",
        "search",
        "find",
        "read",
        "impact_start",
        "impact_page",
        "impact_read",
        "impact_classify",
        "impact_finish",
    } and not os.environ.get("DOCMESH_NO_RECONCILE"):
        freshness = run_once(root)

    arguments = _arguments(args)
    if operation == "bench":
        # ``--queries`` is a path to a JSON file of query cases (matches
        # docmesh.cli's contract); the core `bench` operation itself takes
        # an already-parsed list.
        queries_path = arguments.pop("queries", None)
        if not queries_path:
            return 1, {"ok": False, "error": "--queries is required for bench"}
        try:
            arguments["queries"] = json.loads(
                Path(queries_path).read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            return 1, {"ok": False, "error": f"unable to read --queries: {exc}"}
    # Normalize operation-specific command spelling to the Python API contract.
    if (
        operation == "find"
        and arguments.get("pattern") is None
        and arguments.get("query") is not None
    ):
        arguments["pattern"] = arguments.pop("query")
    if operation == "read" and arguments.get("path") is None and args.paths:
        arguments["path"] = args.paths[0]
    arguments["project_root"] = str(root)
    result = core_call(operation, arguments, project=root)
    record_core_result(root, operation, result)
    status, value = _core_result(result)
    if freshness and freshness.get("status") not in {
        "empty",
        "indexed",
        "already_running",
    }:
        value = {"data": value, "freshness": freshness}
    return status, value


def _core_result(result: Mapping[str, Any]) -> tuple[int, Any]:
    if result.get("ok"):
        return 0, result.get("data", result)
    return 1, result


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        status, value = execute(args)
    except (OSError, ValueError, TypeError) as exc:
        status, value = 1, {"ok": False, "error": str(exc)}
    _print(value, as_json=args.json)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
