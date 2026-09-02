"""Dependency-free command line interface for DocMesh V1."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import api
from .models import DocMeshError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docmesh", description="Local offline document retrieval and consistency"
    )
    parser.add_argument(
        "operation",
        nargs="?",
        default="status",
        choices=(
            "setup",
            "init",
            "index",
            "status",
            "doctor",
            "probe-hooks",
            "search",
            "find",
            "read",
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
        ),
    )
    parser.add_argument("--project-root", "--root", default=".")
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--approve", "--yes", action="store_true")
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
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--pattern", default=None)
    parser.add_argument("--mode", default="literal")
    parser.add_argument("--cursor", default=None)
    parser.add_argument("--path", default=None)
    parser.add_argument("--start-line", type=int, default=None)
    parser.add_argument("--end-line", type=int, default=None)
    parser.add_argument("--page", type=int, default=None)
    parser.add_argument("--phase", default="discover")
    parser.add_argument("--query-bundle", default=None)
    parser.add_argument("--source-roles", nargs="*", default=None)
    parser.add_argument("--page-size", type=int, default=20)
    parser.add_argument("--baseline-run-id", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--candidate-id", default=None)
    parser.add_argument("--context-lines", type=int, default=20)
    parser.add_argument("--decisions", default=None)
    return parser


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
        if args.dry_run:
            return api.setup(**common, dry_run=True)
        if not args.approve:
            raise PermissionError(
                "explicit approval is required; rerun with --dry-run then --approve"
            )
        return api.setup(**common, approve=True)
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
            limit=args.limit,
            source_roles=args.source_roles,
        )
    if operation == "find":
        return api.find(
            **common,
            pattern=args.pattern if args.pattern is not None else (args.query or ""),
            mode=args.mode,
            cursor=args.cursor,
            source_roles=args.source_roles,
        )
    if operation == "read":
        return api.read(
            **common,
            path=args.path or (args.paths[0] if args.paths else ""),
            start_line=args.start_line,
            end_line=args.end_line,
            page=args.page,
        )
    if operation == "impact_start":
        return api.impact_start(
            **common,
            phase=args.phase,
            query_bundle=_json_argument(args.query_bundle, "query-bundle"),
            source_roles=args.source_roles,
            page_size=args.page_size,
            baseline_run_id=args.baseline_run_id,
        )
    if operation == "impact_page":
        return api.impact_page(**common, run_id=args.run_id or "", cursor=args.cursor)
    if operation == "impact_read":
        return api.impact_read(
            **common,
            run_id=args.run_id or "",
            candidate_id=args.candidate_id or "",
            context_lines=args.context_lines,
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
            payload if True else payload["data"], ensure_ascii=False, sort_keys=True
        )
    )
    return status


if __name__ == "__main__":
    raise SystemExit(main())
