"""Dependency-free command line interface for DocMesh V1."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import api
from .argspec import OPERATIONS, add_common_arguments
from .models import DocMeshError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docmesh", description="Local offline document retrieval and consistency"
    )
    parser.add_argument(
        "operation",
        nargs="?",
        default="status",
        choices=OPERATIONS,
    )
    return add_common_arguments(parser)


def _json_argument(value: str | None, name: str, default: Any = None) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--{name} must be JSON: {exc}") from exc


def execute(args: argparse.Namespace) -> Any:
    operation = args.operation.replace("-", "_")
    common: dict[str, Any] = {
        "project_root": args.project_root,
        "db_path": args.db_path,
        "deterministic": args.deterministic,
        "use_fastembed": args.use_fastembed,
    }
    if args.model:
        common["model"] = args.model
    if args.cache_dir:
        common["cache_dir"] = args.cache_dir
    if args.download_model is not None:
        common["download_model"] = args.download_model
    if operation in ("setup", "init"):
        setup_kwargs: dict[str, Any] = {"summary": not args.detailed}
        if args.dry_run:
            return api.setup(**common, dry_run=True, **setup_kwargs)
        if not args.approve:
            raise PermissionError(
                "explicit approval is required; rerun with --dry-run then --approve"
            )
        return api.setup(**common, approve=True, **setup_kwargs)
    if operation == "index":
        return api.index(**common, paths=args.paths, force=args.force)
    if operation == "status":
        return api.status(**common)
    if operation == "doctor":
        return api.doctor(**common)
    if operation == "probe_hooks":
        return api.probe_hooks(**common)
    if operation == "search":
        return api.search(
            **common,
            query=args.query or "",
            limit=args.limit if args.limit is not None else 8,
            source_roles=args.source_roles,
            snippet_only=args.snippet_only,
            max_snippet_length=args.max_snippet_length,
        )
    if operation == "bench":
        if not args.queries:
            raise ValueError("--queries is required for bench")
        queries = json.loads(Path(args.queries).read_text(encoding="utf-8"))
        return api.bench(**common, queries=queries)
    if operation == "find":
        return api.find(
            **common,
            pattern=args.pattern if args.pattern is not None else (args.query or ""),
            mode=args.mode if args.mode is not None else "literal",
            cursor=args.cursor,
            source_roles=args.source_roles,
            scope=args.scope,
            cwd=os.getcwd(),
        )
    if operation == "read":
        return api.read(
            **common,
            path=args.path or (args.paths[0] if args.paths else ""),
            start_line=args.start_line,
            end_line=args.end_line,
            page=args.page,
            cwd=os.getcwd(),
        )
    if operation == "impact_start":
        return api.impact_start(
            **common,
            phase=args.phase if args.phase is not None else "discover",
            query_bundle=_json_argument(args.query_bundle, "query-bundle"),
            source_roles=args.source_roles,
            page_size=args.page_size if args.page_size is not None else 20,
            baseline_run_id=args.baseline_run_id,
        )
    if operation == "impact_page":
        return api.impact_page(
            **common,
            run_id=args.run_id or "",
            cursor=args.cursor,
            page_size=args.page_size,
            snippet_only=True if args.snippet_only is None else args.snippet_only,
        )
    if operation == "impact_read":
        return api.impact_read(
            **common,
            run_id=args.run_id or "",
            candidate_id=args.candidate_id or "",
            context_lines=args.context_lines if args.context_lines is not None else 20,
        )
    if operation == "impact_classify":
        return api.impact_classify(
            **common,
            run_id=args.run_id or "",
            decisions=_json_argument(args.decisions, "decisions", {}),
        )
    if operation == "impact_finish":
        return api.impact_finish(**common, run_id=args.run_id or "")
    raise ValueError(f"unknown operation: {args.operation}")


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return _jsonable(value.to_dict())
    if hasattr(value, "__dataclass_fields__"):
        return {
            name: _jsonable(getattr(value, name)) for name in value.__dataclass_fields__
        }
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    # The harness fallback invokes ``python -m docmesh <operation>`` and sends
    # the full request on stdin.  Merge that request so query/impact arguments
    # are not lost when the package is not installed as an importable module.
    if not sys.stdin.isatty():
        try:
            encoded = sys.stdin.read().strip()
            if encoded:
                request = json.loads(encoded)
                if isinstance(request, Mapping):
                    values = vars(args)
                    if values.get("project_root") == "." and request.get(
                        "project_root"
                    ):
                        values["project_root"] = request["project_root"]
                    for key, value in request.items():
                        if (
                            key in values
                            and key not in ("operation", "project_root")
                            and value is not None
                        ):
                            values[
                                key.replace("_", "-")
                                if key.replace("_", "-") in values
                                else key
                            ] = value
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            # Argument parsing errors remain the primary CLI diagnostic; an
            # unrelated/non-JSON stdin stream must not crash status.
            pass
    try:
        result = execute(args)
        payload = {"ok": True, "data": _jsonable(result)}
        status = 0
    except (
        DocMeshError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        sqlite3.Error,
    ) as exc:
        payload = {"ok": False, "error": str(exc), "error_type": type(exc).__name__}
        status = 1
    print(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True
        )
    )
    return status


if __name__ == "__main__":
    raise SystemExit(main())
