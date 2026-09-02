#!/usr/bin/env python3
"""Offline runtime/trust capability probe and cache.

Static files prove only that a plugin *could* be loaded.  Runtime-specific
facts (trust, Stop dispatch, and whether a block is respected) must be supplied
by the harness adapter or a fixture proof.  Until every required fact is true,
the enforcement layer stays advisory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from harness import (
    HarnessPaths,
    atomic_write_json,
    project_root,
    truthy,
    utc_now,
)

CACHE_SCHEMA_VERSION = 1
REQUIRED_CAPABILITIES = (
    "plugin_installed",
    "hook_present",
    "hook_trusted",
    "stop_dispatched",
    "blocking_respected",
    "loop_protected",
)


def runtime_harness(runtime: str | None = None) -> str:
    value = (runtime or os.environ.get("DOCMESH_RUNTIME") or "unknown").strip().lower()
    if value in {"claude-code", "claude_code", "claude"}:
        return "claude"
    if value in {"codex", "openai-codex"}:
        return "codex"
    return value or "unknown"


def runtime_version(harness: str) -> str:
    return (
        os.environ.get("DOCMESH_RUNTIME_VERSION")
        or (
            os.environ.get("CLAUDE_CODE_VERSION")
            if harness == "claude"
            else os.environ.get("CODEX_VERSION")
        )
        or "unknown"
    )


def runtime_transport() -> str:
    return (
        os.environ.get("DOCMESH_RUNTIME_TRANSPORT", "command").strip().lower()
        or "command"
    )


def plugin_root(value: str | os.PathLike[str] | None = None) -> Path:
    candidate = (
        value
        or os.environ.get("DOCMESH_PLUGIN_ROOT")
        or os.environ.get("CLAUDE_PLUGIN_ROOT")
        or os.environ.get("CODEX_PLUGIN_ROOT")
    )
    if candidate:
        return Path(candidate).expanduser().resolve()
    return SCRIPT_DIR.parent.resolve()


def definition_hash(root: Path) -> str:
    """Hash hook definitions only; changing definitions invalidates proof."""

    digest = hashlib.sha256()
    found = False
    for name in ("hooks/hooks.json", "hooks/codex-hooks.json"):
        path = root / name
        if not path.is_file():
            continue
        found = True
        digest.update(name.encode("utf-8"))
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    if not found:
        digest.update(b"no-hook-definition")
    return digest.hexdigest()


def _env_proof(name: str) -> bool:
    return truthy(os.environ.get(name, ""))


def _file_proof(paths: HarnessPaths, key: str) -> bool:
    try:
        value = json.loads(paths.trust.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return False
    if not isinstance(value, Mapping):
        return False
    return truthy(value.get(key, False))


def _command_proof(
    variable: str, *, project: Path, harness: str, plugin: Path, key: str
) -> bool:
    """Run an explicitly configured, local proof adapter with no network.

    The adapter must print JSON containing the requested truthy field.  A
    non-zero exit, malformed output, or a plain-text response is unknown and
    therefore cannot enable strict mode.
    """

    command = os.environ.get(variable)
    if not command:
        return False
    try:
        completed = subprocess.run(
            shlex.split(command),
            input=json.dumps(
                {
                    "project_root": str(project),
                    "harness": harness,
                    "plugin_root": str(plugin),
                }
            ),
            text=True,
            capture_output=True,
            cwd=project,
            env={**os.environ, "DOCMESH_HOOK_SUPPRESS": "1"},
            timeout=10,
            check=False,
        )
        if completed.returncode != 0:
            return False
        value: Any = json.loads((completed.stdout or "").strip())
        return isinstance(value, Mapping) and truthy(value.get(key, False))
    except (
        OSError,
        ValueError,
        TypeError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ):
        return False


def inspect_static_hooks(root: Path) -> dict[str, bool]:
    result = {
        "hook_present": False,
        "post_edit_hook_present": False,
        "stop_hook_present": False,
    }
    for name in ("hooks/hooks.json", "hooks/codex-hooks.json"):
        path = root / name
        if not path.is_file():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        hooks = value.get("hooks") if isinstance(value, Mapping) else None
        if not isinstance(hooks, Mapping):
            continue
        result["stop_hook_present"] |= "Stop" in hooks
        result["post_edit_hook_present"] |= "PostToolUse" in hooks
    result["hook_present"] = (
        result["stop_hook_present"] and result["post_edit_hook_present"]
    )
    return result


def cache_key(project: Path, harness: str, plugin: Path) -> dict[str, str]:
    return {
        "harness": harness,
        "runtime": "claude-code" if harness == "claude" else harness,
        "version": runtime_version(harness),
        "transport": runtime_transport(),
        "hook_definition_hash": definition_hash(plugin),
    }


def _cache_value(paths: HarnessPaths) -> dict[str, Any]:
    try:
        value = json.loads(paths.capability_cache.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {"schema_version": CACHE_SCHEMA_VERSION, "entries": []}
    if not isinstance(value, dict):
        return {"schema_version": CACHE_SCHEMA_VERSION, "entries": []}
    if "entries" not in value and isinstance(value.get("proofs"), list):
        value["entries"] = value["proofs"]
    if "entries" not in value and "key" in value and "capabilities" in value:
        value["entries"] = [value]
    if not isinstance(value.get("entries", []), list):
        return {"schema_version": CACHE_SCHEMA_VERSION, "entries": []}
    return value


def _key_field_equal(field: str, left: Any, right: Any) -> bool:
    left_text, right_text = str(left), str(right)
    if left_text == right_text:
        return True
    if field == "runtime" and {left_text, right_text} <= {
        "claude",
        "claude-code",
        "claude_code",
    }:
        return True
    return bool(
        field == "harness"
        and {left_text, right_text} <= {"claude", "claude-code", "claude_code"}
    )


def _same_key(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return all(
        _key_field_equal(field, left.get(field, ""), value)
        for field, value in right.items()
    )


def find_cached(
    project: Path, key: Mapping[str, str], *, allow_wildcard: bool = False
) -> dict[str, Any] | None:
    paths = HarnessPaths.for_project(project)
    cache = _cache_value(paths)
    for entry in reversed(cache.get("entries", [])):
        if not isinstance(entry, Mapping):
            continue
        candidate = entry.get("key")
        capabilities = entry.get("capabilities")
        if not isinstance(candidate, Mapping) or not isinstance(capabilities, Mapping):
            continue
        if _same_key(candidate, key):
            return dict(capabilities)
        if allow_wildcard:
            comparable = dict(key)
            if all(
                str(candidate.get(field, "")) in {"", "*", str(value)}
                for field, value in comparable.items()
            ):
                return dict(capabilities)
    return None


def save_cache(
    project: Path, key: Mapping[str, str], capabilities: Mapping[str, Any]
) -> None:
    paths = HarnessPaths.for_project(project)
    paths.ensure()
    cache = _cache_value(paths)
    entries = [
        entry
        for entry in cache.get("entries", [])
        if isinstance(entry, Mapping) and not _same_key(entry.get("key", {}), key)
    ]
    entries.append(
        {"key": dict(key), "capabilities": dict(capabilities), "probed_at": utc_now()}
    )
    atomic_write_json(
        paths.capability_cache,
        {"schema_version": CACHE_SCHEMA_VERSION, "entries": entries},
    )


def probe(
    project: Path | str | None = None,
    *,
    runtime: str | None = None,
    plugin: Path | str | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    project_path = project_root(project)
    plugin_path = plugin_root(plugin)
    harness = runtime_harness(runtime)
    key = cache_key(project_path, harness, plugin_path)
    if not refresh and not os.environ.get("DOCMESH_NO_PROBE"):
        cached = find_cached(project_path, key)
        if cached is not None:
            return {
                "schema_version": CACHE_SCHEMA_VERSION,
                "key": key,
                "capabilities": cached,
                "cache_hit": True,
            }

    paths = HarnessPaths.for_project(project_path)
    static = inspect_static_hooks(plugin_path)
    installed_default = (plugin_path / ".codex-plugin" / "plugin.json").is_file() or (
        plugin_path / ".claude-plugin" / "plugin.json"
    ).is_file()
    capabilities: dict[str, Any] = {
        "plugin_installed": _env_proof("DOCMESH_PLUGIN_INSTALLED") or installed_default,
        "hook_present": _env_proof("DOCMESH_HOOK_PRESENT") or static["hook_present"],
        "hook_trusted": _env_proof("DOCMESH_HOOK_TRUSTED")
        or _file_proof(paths, "hook_trusted")
        or _command_proof(
            "DOCMESH_TRUST_PROBE_COMMAND",
            project=project_path,
            harness=harness,
            plugin=plugin_path,
            key="hook_trusted",
        ),
        "stop_dispatched": _env_proof("DOCMESH_STOP_DISPATCH_PROOF")
        or _file_proof(paths, "stop_dispatched")
        or _command_proof(
            "DOCMESH_STOP_PROBE_COMMAND",
            project=project_path,
            harness=harness,
            plugin=plugin_path,
            key="stop_dispatched",
        ),
        "blocking_respected": _env_proof("DOCMESH_BLOCKING_PROOF")
        or _file_proof(paths, "blocking_respected")
        or _command_proof(
            "DOCMESH_BLOCK_PROBE_COMMAND",
            project=project_path,
            harness=harness,
            plugin=plugin_path,
            key="blocking_respected",
        ),
        "loop_protected": _env_proof("DOCMESH_LOOP_PROTECTION_PROOF")
        or _file_proof(paths, "loop_protected")
        or _command_proof(
            "DOCMESH_LOOP_PROBE_COMMAND",
            project=project_path,
            harness=harness,
            plugin=plugin_path,
            key="loop_protected",
        ),
    }
    capabilities["proven"] = all(
        bool(capabilities.get(name)) for name in REQUIRED_CAPABILITIES
    )
    capabilities["unknown_surfaces"] = [
        name for name in REQUIRED_CAPABILITIES if not capabilities.get(name)
    ]
    save_cache(project_path, key, capabilities)
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "key": key,
        "capabilities": capabilities,
        "cache_hit": False,
    }


def capabilities_for_enforcement(
    project: Path, runtime: str | None = None
) -> dict[str, Any]:
    plugin = plugin_root(None)
    harness = runtime_harness(runtime)
    key = cache_key(project, harness, plugin)
    cached = find_cached(project, key, allow_wildcard=True)
    return cached or {"proven": False, "unknown_surfaces": list(REQUIRED_CAPABILITIES)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Probe and cache DocMesh runtime/trust capability"
    )
    parser.add_argument("--project-root", default=None)
    parser.add_argument("--plugin-root", default=None)
    parser.add_argument("--runtime", default=None)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = probe(
        args.project_root,
        runtime=args.runtime,
        plugin=args.plugin_root,
        refresh=args.refresh,
    )
    output = json.dumps(result, ensure_ascii=False, sort_keys=True)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
