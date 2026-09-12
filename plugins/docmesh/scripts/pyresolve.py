#!/usr/bin/env python3
"""Resolve a Python 3.12+ interpreter for DocMesh harness entrypoints.

This module is imported by the dependency-free launchers and hooks, including
on macOS where the system ``python3`` is commonly older than Python 3.12.  It
therefore stays parseable on Python 3.9 and keeps all version/dependency probes
in short-lived child processes.  Resolution is offline; dependency bootstrap
is opt-in and is reserved for an explicitly approved CLI setup invocation.
"""

from __future__ import annotations

import json
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
PROBE_TIMEOUT_SECONDS = 15
BOOTSTRAP_TIMEOUT_SECONDS = 600
_MARKER_VERSION = 1

# Import probes can load fastembed and its transitive dependencies.  Keep the
# result local to this process so a worker/hook does not repeat that work, and
# clear it after a successful dependency bootstrap because the environment may
# have changed.
_MISSING_DEPS_CACHE: dict[str, tuple[str, ...]] = {}


def scripts_dir() -> Path:
    return Path(__file__).resolve().parent


def plugin_root() -> Path:
    return scripts_dir().parent


def _lexical_path(value: str | os.PathLike[str]) -> str:
    """Normalize a path without resolving symlinks.

    A virtualenv's ``bin/python`` is commonly a symlink into a shared Python
    installation.  Resolving that symlink here would discard the virtualenv's
    site-packages identity and can make a healthy environment look empty.
    """

    return os.path.normcase(os.path.abspath(os.path.expanduser(os.fspath(value))))


def _same_path(left: str | os.PathLike[str], right: str | os.PathLike[str]) -> bool:
    return _lexical_path(left) == _lexical_path(right)


def _version_probe(interpreter: str) -> bool:
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
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _accepts(interpreter: str) -> bool:
    if not os.path.isfile(interpreter) or not os.access(interpreter, os.X_OK):
        return False
    return _version_probe(interpreter)


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
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    return completed.stdout.strip().splitlines()[-1]


def candidates(root: Path) -> list[str]:
    """Return unique interpreter paths in preference order.

    The plugin venv comes before an ambient venv.  The current interpreter is
    included explicitly because a caller can already be running from a valid
    environment that is not discoverable through ``VIRTUAL_ENV`` or PATH.
    De-duplication is lexical so distinct venv paths remain distinct even when
    their executable symlinks share the same real target.
    """

    seen: set[str] = set()
    ordered: list[str] = []

    def add(value: str | None) -> None:
        if not value:
            return
        location = _lexical_path(value)
        if location in seen:
            return
        seen.add(location)
        ordered.append(location)

    add(str(root / ".venv" / "bin" / "python"))
    virtual = os.environ.get("VIRTUAL_ENV")
    if virtual:
        add(os.path.join(virtual, "bin", "python"))
    add(sys.executable)
    for minor in CANDIDATE_MINORS:
        add(shutil.which(f"python3.{minor}"))
    add(shutil.which("python3"))
    add(shutil.which("python"))
    add(_uv_interpreter())
    return ordered


def _missing_deps(interpreter: str) -> list[str]:
    """Return missing runtime modules, probing only inside ``interpreter``.

    The probe locates each module instead of importing it.  Importing
    ``fastembed`` costs roughly 0.3s and every hook, worker, and MCP start
    pays that before doing any work; locating it costs ~0.02s and answers the
    only question interpreter selection asks.  A module that is present but
    fails at import time still surfaces later as a structured core diagnostic.
    """

    key = _lexical_path(interpreter)
    cached = _MISSING_DEPS_CACHE.get(key)
    if cached is not None:
        return list(cached)

    probe = (
        "import json\n"
        "from importlib.util import find_spec\n"
        "missing = []\n"
        f"for name in {REQUIRED_MODULES!r}:\n"
        "    try:\n"
        "        found = find_spec(name) is not None\n"
        "    except BaseException:\n"
        "        found = False\n"
        "    if not found:\n"
        "        missing.append(name)\n"
        "print(json.dumps({'missing': missing}))\n"
    )
    missing: list[str] = list(REQUIRED_MODULES)
    try:
        completed = subprocess.run(
            [interpreter, "-c", probe],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        _MISSING_DEPS_CACHE[key] = tuple(missing)
        return missing

    for line in reversed((completed.stdout or "").splitlines()):
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict) and isinstance(value.get("missing"), list):
            reported = value["missing"]
            missing = [name for name in REQUIRED_MODULES if name in reported]
            break
    _MISSING_DEPS_CACHE[key] = tuple(missing)
    return list(missing)


def _clear_probe_cache() -> None:
    _MISSING_DEPS_CACHE.clear()


def resolve_interpreter(root: Path | None = None) -> str | None:
    """Select a dependency-ready interpreter, then a valid fallback.

    Version validity is checked for every candidate first.  Among those
    candidates, dependency readiness wins over version/order, so a newer bare
    Python cannot displace a usable plugin environment, while a valid but
    dependency-free interpreter remains available for structured harness
    diagnostics.
    """

    # Candidates are probed lazily: the common case is the plugin venv first,
    # so a healthy environment never pays for spawning probes into every other
    # interpreter on PATH.  The selection is the same one an exhaustive scan
    # would make, because dependency readiness is checked in candidate order.
    first_valid: str | None = None
    for candidate in candidates(root or plugin_root()):
        if not _accepts(candidate):
            continue
        if first_valid is None:
            first_valid = candidate
        if not _missing_deps(candidate):
            return candidate
    return first_valid


def _bootstrap_deps(root: Path) -> bool:
    """Run the explicit setup dependency install and report success."""

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
            timeout=BOOTSTRAP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def bootstrap_allowed(argv: list[str] | None = None) -> bool:
    """Whether CLI arguments explicitly approve dependency bootstrap.

    This is intentionally a small, dependency-free pre-parser used before
    ``argparse`` and core imports.  Dry-run always remains offline.
    """

    values = list(argv) if argv is not None else sys.argv[1:]
    operation = next(
        (
            value.strip().lower().replace("-", "_")
            for value in values
            if value.strip().lower().replace("-", "_") in {"setup", "init"}
        ),
        None,
    )
    return (
        operation in {"setup", "init"}
        and "--approve" in values
        and "--dry-run" not in values
    )


def _marker_payload(root: Path, interpreter: str) -> str:
    return json.dumps(
        {
            "version": _MARKER_VERSION,
            "plugin": _lexical_path(root),
            "interpreter": _lexical_path(interpreter),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _marker_matches(root: Path) -> bool:
    raw = os.environ.get(RESOLVED_MARKER)
    if not raw:
        return False
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return False
    return (
        isinstance(value, dict)
        and value.get("version") == _MARKER_VERSION
        and value.get("plugin") == _lexical_path(root)
        and value.get("interpreter") == _lexical_path(sys.executable)
    )


def _error(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(2)


def ensure_python(root: Path | None = None, *, bootstrap: bool = False) -> None:
    """Keep the current entrypoint on a usable Python 3.12+ interpreter.

    ``bootstrap`` is false by default.  Hooks, workers, MCP startup, dry-runs,
    and shell path discovery therefore never install dependencies.  The CLI
    entrypoint enables it only for an explicitly approved setup/init command.
    If no dependency-ready interpreter exists, a valid Python 3.12+ fallback is
    still selected so the dependency-free harness can return a structured core
    diagnostic instead of failing during interpreter resolution.
    """

    plugin_dir = Path(root or plugin_root()).expanduser().resolve(strict=False)
    current = f"{sys.executable} (Python {sys.version_info[0]}.{sys.version_info[1]})"
    current_valid = sys.version_info[:2] >= MINIMUM_PYTHON

    # A marker from an older plugin or another interpreter is stale and must
    # not suppress resolution.  An exact marker means a prior re-exec landed
    # back in the same unsupported interpreter, so stop instead of looping.
    if not current_valid and _marker_matches(plugin_dir):
        _error(
            f"DocMesh requires Python {MINIMUM_VERSION_SPEC}+; found {current}. "
            f"No compatible interpreter could be selected for {plugin_dir}."
        )

    if current_valid and not _marker_matches(plugin_dir):
        if not _missing_deps(sys.executable):
            return

    target = resolve_interpreter(plugin_dir)
    if target is None:
        if current_valid:
            # Keep dependency-free status/doctor/hook paths alive; core_call
            # will report the missing package diagnostic in structured form.
            return
        _error(
            f"DocMesh requires Python {MINIMUM_VERSION_SPEC}+ (found {current}), "
            "but no suitable interpreter was found. Install Python 3.12+ or "
            f"run `uv sync --extra test` in {plugin_dir}."
        )

    missing = _missing_deps(target)
    if missing and bootstrap and _bootstrap_deps(plugin_dir):
        _clear_probe_cache()
        target = resolve_interpreter(plugin_dir)
        if target is not None:
            missing = _missing_deps(target)

    if current_valid:
        # A valid current interpreter with no ready alternative must remain in
        # place so dependency-free harness calls can emit structured errors.
        if missing or _same_path(target, sys.executable):
            return
    environment = dict(os.environ)
    environment[RESOLVED_MARKER] = _marker_payload(plugin_dir, target)
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
    ensure_python(bootstrap=bootstrap_allowed(values))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
