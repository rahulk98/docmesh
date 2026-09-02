#!/usr/bin/env python3
"""Resolve a Python 3.12+ interpreter for DocMesh harness entrypoints.

DocMesh requires Python >= 3.12, but macOS ships the system `python3` as
Python 3.9.  This module must parse on whatever interpreter launches a
harness entrypoint, so it stays compatible with Python 3.9.  It either
re-executes the current script under a suitable interpreter or prints the
path of one for the shell launchers.  If the resolved interpreter is
missing DocMesh's dependencies, it bootstraps them by running
`uv sync --extra test` in the plugin root (only when `uv` is available),
then re-checks before falling back to a clear error.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

MINIMUM_PYTHON = (3, 12)
MINIMUM_VERSION_SPEC = "3.12"
RESOLVED_MARKER = "DOCMESH_PYTHON_RESOLVED"
CANDIDATE_MINORS = range(16, 11, -1)  # python3.16 ... python3.12
REQUIRED_MODULES = ("fastembed", "pypdf", "sqlite_vec")


def scripts_dir() -> Path:
    return Path(__file__).resolve().parent


def plugin_root() -> Path:
    return scripts_dir().parent


def _accepts(interpreter: str) -> bool:
    if not os.path.isfile(interpreter) or not os.access(interpreter, os.X_OK):
        return False
    try:
        completed = subprocess.run(
            [
                interpreter,
                "-c",
                "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 12) else 1)",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return False
    return completed.returncode == 0


def _uv_interpreter() -> str | None:
    executable = shutil.which("uv")
    if not executable:
        return None
    try:
        completed = subprocess.run(
            [executable, "python", "find", ">=3.12"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    return completed.stdout.strip().splitlines()[-1]


def candidates(root: Path) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []

    def add(value: str | None) -> None:
        if not value:
            return
        location = os.path.realpath(value)
        if location in seen:
            return
        seen.add(location)
        ordered.append(value)

    virtual = os.environ.get("VIRTUAL_ENV")
    if virtual:
        add(os.path.join(virtual, "bin", "python"))
    add(str(root / ".venv" / "bin" / "python"))
    for minor in CANDIDATE_MINORS:
        add(shutil.which(f"python3.{minor}"))
    add(shutil.which("python3"))
    add(shutil.which("python"))
    add(_uv_interpreter())
    return ordered


def resolve_interpreter(root: Path | None = None) -> str | None:
    for candidate in candidates(root or plugin_root()):
        if _accepts(candidate):
            return candidate
    return None


def _missing_deps(interpreter: str) -> list[str]:
    probe = "; ".join(f"import {name}" for name in REQUIRED_MODULES)
    try:
        completed = subprocess.run(
            [interpreter, "-c", probe],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError:
        return list(REQUIRED_MODULES)
    if completed.returncode == 0:
        return []
    return [name for name in REQUIRED_MODULES if name not in (completed.stderr or "")] or list(
        REQUIRED_MODULES
    )


def _bootstrap_deps(root: Path) -> bool:
    """Run `uv sync --extra test` in the plugin root. Returns True on success."""
    executable = shutil.which("uv")
    if not executable:
        return False
    try:
        completed = subprocess.run(
            [executable, "sync", "--extra", "test"],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def ensure_python(root: Path | None = None, *, bootstrap: bool = True) -> None:
    """Re-execute the current entrypoint under a Python 3.12+ interpreter.

    `bootstrap` controls whether a missing-deps interpreter triggers
    `uv sync`. Callers bound by the offline contract (hooks, the detached
    indexing worker) must pass `bootstrap=False` so they never touch the
    network; only explicit, user-initiated entrypoints (the MCP server,
    `entrypoint.py`) may bootstrap.
    """
    if sys.version_info[:2] >= MINIMUM_PYTHON:
        return
    plugin_dir = root or plugin_root()
    current = f"{sys.executable} (Python {sys.version_info[0]}.{sys.version_info[1]})"
    if os.environ.get(RESOLVED_MARKER):
        print(
            f"DocMesh requires Python {MINIMUM_VERSION_SPEC}+; found {current}. "
            "Install Python 3.12+ or run `uv sync --extra test` in "
            f"{plugin_dir}.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    target = resolve_interpreter(plugin_dir)
    if target is None:
        print(
            f"DocMesh requires Python {MINIMUM_VERSION_SPEC}+ (found {current}), "
            "but no suitable interpreter was found. Install Python 3.12+ or run "
            f"`uv sync --extra test` in {plugin_dir}.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    missing = _missing_deps(target)
    if missing and bootstrap and _bootstrap_deps(plugin_dir):
        target = resolve_interpreter(plugin_dir) or target
        missing = _missing_deps(target)
    if missing:
        print(
            f"Found Python 3.12+ at {target}, but it is missing required packages "
            f"({', '.join(missing)}), and `uv sync --extra test` in {plugin_dir} "
            "did not resolve it. Install uv (https://docs.astral.sh/uv/) or run "
            "that command manually.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    environment = dict(os.environ)
    environment[RESOLVED_MARKER] = "1"
    arguments = [target, os.path.abspath(sys.argv[0]), *sys.argv[1:]]
    os.execvpe(target, arguments, environment)


def main(argv: list[str] | None = None) -> int:
    values = list(argv) if argv is not None else sys.argv[1:]
    if "--print-path" in values:
        target = resolve_interpreter()
        if not target:
            return 2
        print(target)
        return 0
    ensure_python()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())