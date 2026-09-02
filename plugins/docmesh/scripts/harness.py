"""Small, dependency-free primitives shared by DocMesh harness entrypoints.

The core package owns indexing and retrieval.  This module owns only the
process boundary around it: project-local queue/state locations, safe path
extraction from hook metadata, durable JSONL writes, and subprocess delegation.
It intentionally never interprets the contents of an indexed document.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as _datetime
import fcntl
import hashlib
import inspect
import json
import os
import shlex
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SOURCE_SUFFIXES = frozenset(
    {
        ".md",
        ".mdx",
        ".tex",
        ".latex",
        ".bib",
        ".txt",
        ".text",
        ".pdf",
    }
)
_PATH_KEYS = frozenset(
    {
        "file_path",
        "filepath",
        "path",
        "notebook_path",
        "file",
        "files",
        "file_paths",
        "changed_file",
        "changed_files",
        "edited_file",
        "edited_files",
        "modified_file",
        "modified_files",
        "source_path",
    }
)


def utc_now() -> str:
    """Return a stable, UTC ISO-8601 timestamp suitable for event records."""

    return _datetime.datetime.now(_datetime.UTC).isoformat().replace("+00:00", "Z")


def project_root(value: str | os.PathLike[str] | None = None) -> Path:
    """Resolve a project root without assuming a repository or a home path."""

    candidate = value or os.environ.get("DOCMESH_PROJECT_ROOT") or os.getcwd()
    return Path(candidate).expanduser().resolve()


@dataclasses.dataclass(frozen=True)
class HarnessPaths:
    """Paths for state owned by the plugin harness.

    They are deliberately separate from the core database and are all under
    `.docmesh/`, which the design reserves for local queue/log state.
    """

    project: Path
    root: Path
    events: Path
    acknowledgements: Path
    worker_lock: Path
    worker_log: Path
    state: Path
    capability_cache: Path
    stop_guard: Path
    trust: Path

    @classmethod
    def for_project(cls, value: str | os.PathLike[str] | None = None) -> HarnessPaths:
        project = project_root(value)
        root = project / ".docmesh" / "harness"
        return cls(
            project=project,
            root=root,
            events=root / "dirty-events.jsonl",
            acknowledgements=root / "dirty-events.ack.jsonl",
            worker_lock=root / "worker.lock",
            worker_log=root / "worker.log",
            state=root / "state.json",
            capability_cache=root / "capability-cache.json",
            stop_guard=root / "stop-guard.json",
            trust=root / "trust.json",
        )

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def event_sources(self) -> tuple[Path, ...]:
        """Canonical queue plus read-only paths used by early V1 adapters."""

        return (
            self.events,
            self.project / ".docmesh" / "queue" / "dirty-events.jsonl",
            self.project / ".docmesh" / "dirty-events.jsonl",
        )


def normalise_path(value: str | os.PathLike[str], base: Path | None = None) -> Path:
    """Return an absolute, lexical path; preserve missing/deleted sources."""

    candidate = Path(str(value)).expanduser()
    if not candidate.is_absolute():
        candidate = (base or project_root()) / candidate
    return candidate.resolve(strict=False)


def is_source_path(value: str | os.PathLike[str]) -> bool:
    """Whether a path has a V1 source suffix and is not harness metadata."""

    path = Path(str(value))
    if ".docmesh" in path.parts:
        return False
    return path.suffix.lower() in SOURCE_SUFFIXES


def _path_values(value: Any, key: str | None = None) -> Iterator[str]:
    """Extract only path-shaped hook metadata, never arbitrary text content."""

    if isinstance(value, Mapping):
        for child_key, child in value.items():
            child_name = str(child_key).lower()
            if child_name in _PATH_KEYS:
                yield from _path_values(child, child_name)
            elif isinstance(child, (Mapping, list, tuple)):
                # Nested tool metadata often wraps edits in `input`, `result`,
                # or `changes`; recurse but keep scalar values gated by a path
                # key at every level.
                yield from _path_values(child, child_name)
    elif isinstance(value, (list, tuple, set)):
        for child in value:
            yield from _path_values(child, key)
    elif isinstance(value, str) and key in _PATH_KEYS:
        stripped = value.strip()
        if stripped:
            yield stripped


def extract_changed_paths(
    payload: Mapping[str, Any], base: Path | None = None
) -> list[Path]:
    """Extract current source locations from a PostToolUse payload.

    Claude Code and Codex have used slightly different names for the same
    fields.  We accept both while filtering to V1 source suffixes.  An explicit
    `DOCMESH_CHANGED_FILES` value is useful for harness adapters that cannot
    preserve the original JSON payload.
    """

    values: list[str] = []
    for field in ("tool_input", "tool_response", "result", "input"):
        child = payload.get(field)
        if isinstance(child, (Mapping, list, tuple)):
            values.extend(_path_values(child))
    values.extend(_path_values(payload))
    encoded = os.environ.get("DOCMESH_CHANGED_FILES", "")
    if encoded:
        values.extend(item for item in encoded.split(os.pathsep) if item.strip())

    paths = {
        normalise_path(item, base or project_root(payload.get("cwd")))
        for item in values
    }
    return sorted(
        (item for item in paths if is_source_path(item)), key=lambda item: str(item)
    )


@contextlib.contextmanager
def locked_append(path: Path) -> Iterator[Any]:
    """Open a JSONL file and fsync one append while holding an advisory lock."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield stream
            stream.flush()
            os.fsync(stream.fileno())
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def append_jsonl(path: Path, value: Mapping[str, Any]) -> None:
    """Durably append one JSON object and sync its containing directory."""

    with locked_append(path) as stream:
        stream.write(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
        stream.write("\n")
    _fsync_directory(path.parent)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read valid JSON object lines; malformed lines are returned as errors."""

    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
        try:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    records.append({"_malformed": True, "line": line_number})
                    continue
                if isinstance(value, dict):
                    records.append(value)
                else:
                    records.append({"_malformed": True, "line": line_number})
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    return records


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Write JSON via a same-directory replace and fsync."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary_path.unlink()


def _fsync_directory(path: Path) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def append_dirty_event(
    paths: Sequence[str | os.PathLike[str]],
    *,
    project: str | os.PathLike[str] | None = None,
    payload: Mapping[str, Any] | None = None,
    runtime: str | None = None,
) -> dict[str, Any]:
    """Append a durable dirty-file event and return the exact event record."""

    root = project_root(project or (payload or {}).get("cwd"))
    canonical = sorted(
        {str(normalise_path(item, root)) for item in paths if is_source_path(item)}
    )
    if not canonical:
        raise ValueError("dirty-file event must contain at least one V1 source path")
    paths_obj = HarnessPaths.for_project(root)
    paths_obj.ensure()
    event: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event_type": "dirty_files",
        "event_id": str(uuid.uuid4()),
        "created_at": utc_now(),
        "project_root": str(root),
        "files": canonical,
        "runtime": runtime or os.environ.get("DOCMESH_RUNTIME", "unknown"),
        "hook_event": (payload or {}).get("hook_event_name", "PostToolUse"),
        "tool_name": (payload or {}).get("tool_name"),
        "pid": os.getpid(),
        "durability": {"format": "jsonl-v1", "fsynced": True},
    }
    append_jsonl(paths_obj.events, event)
    return event


def acknowledged_event_ids(paths: HarnessPaths) -> set[str]:
    return {
        str(record["event_id"])
        for record in read_jsonl(paths.acknowledgements)
        if not record.get("_malformed") and record.get("event_id")
    }


def pending_events(paths: HarnessPaths) -> list[dict[str, Any]]:
    """Return unacknowledged events for one project, preserving event order."""

    acknowledged = acknowledged_event_ids(paths)
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in paths.event_sources:
        for record in read_jsonl(source):
            if record.get("_malformed"):
                continue
            event_id = str(record.get("event_id", ""))
            if not event_id or event_id in seen or event_id in acknowledged:
                continue
            if project_root(record.get("project_root")) != paths.project:
                continue
            if record.get("event_type") != "dirty_files":
                continue
            seen.add(event_id)
            result.append(record)
    return result


def acknowledge_events(paths: HarnessPaths, event_ids: Sequence[str]) -> None:
    for event_id in event_ids:
        append_jsonl(
            paths.acknowledgements,
            {
                "schema_version": SCHEMA_VERSION,
                "event_id": event_id,
                "acked_at": utc_now(),
            },
        )


@contextlib.contextmanager
def try_file_lock(path: Path) -> Iterator[bool]:
    """Yield whether an exclusive non-blocking process lock was acquired."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as stream:
        acquired = False
        try:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                acquired = False
            yield acquired
        finally:
            if acquired:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def to_jsonable(value: Any) -> Any:
    """Convert core return values to JSON without importing core dependencies."""

    if dataclasses.is_dataclass(value):
        return {
            key: to_jsonable(item) for key, item in dataclasses.asdict(value).items()
        }
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "model_dump"):
        with contextlib.suppress(Exception):
            return to_jsonable(value.model_dump())
    if hasattr(value, "__dict__") and not isinstance(
        value, (str, bytes, int, float, bool)
    ):
        with contextlib.suppress(Exception):
            return to_jsonable(vars(value))
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _core_module_candidates() -> list[str]:
    return ["docmesh.api", "docmesh.cli", "docmesh"]


def _call_imported_core(
    operation: str, arguments: Mapping[str, Any]
) -> tuple[bool, Any]:
    """Try a public Python core operation, returning (found, value)."""

    # A source checkout is not necessarily installed yet.  Add the sibling
    # src/ directory without changing the caller's global environment.
    source_dir = Path(__file__).resolve().parents[1] / "src"
    if source_dir.is_dir() and str(source_dir) not in sys.path:
        sys.path.insert(0, str(source_dir))
    aliases = {
        "setup": ("setup", "initialize_project", "init"),
        "init": ("init", "initialize_project", "setup"),
        "index": ("index", "reindex"),
        "doctor": ("doctor", "probe_hooks"),
        "probe-hooks": ("probe_hooks", "probe_hooks_capabilities"),
    }
    names = aliases.get(operation, (operation,))
    for module_name in _core_module_candidates():
        try:
            module = __import__(module_name, fromlist=["*"])
        except (
            ImportError,
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
            AttributeError,
        ):
            continue
        for name in names:
            function = getattr(module, name, None)
            if not callable(function):
                continue
            kwargs = dict(arguments)
            try:
                signature = inspect.signature(function)
                if not any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD
                    for parameter in signature.parameters.values()
                ):
                    kwargs = {
                        key: value
                        for key, value in kwargs.items()
                        if key in signature.parameters
                    }
                return True, to_jsonable(function(**kwargs))
            except TypeError as exc:
                # A function accepting a single request object is a useful
                # fallback for small adapters and black-box fixtures.
                try:
                    return True, to_jsonable(function(kwargs))
                except (
                    OSError,
                    RuntimeError,
                    ValueError,
                    TypeError,
                    KeyError,
                    IndexError,
                    AttributeError,
                ):
                    return True, {
                        "ok": False,
                        "error": f"core {operation} failed: {exc}",
                    }
            except (
                OSError,
                RuntimeError,
                ValueError,
                KeyError,
                IndexError,
                AttributeError,
            ) as exc:
                return True, {"ok": False, "error": f"core {operation} failed: {exc}"}
    return False, None


def core_call(
    operation: str,
    arguments: Mapping[str, Any] | None = None,
    *,
    project: Path | None = None,
) -> dict[str, Any]:
    """Call a core operation through an injected fixture or installed package.

    `DOCMESH_CORE_COMMAND` is intentionally supported for subprocess/e2e
    harnesses.  The command receives a JSON request on stdin and the operation
    name as an argument unless `{operation}` or `{payload}` placeholders are
    present.  Production installations use the public Python API or
    `python -m docmesh` as a final fallback.
    """

    root = project or project_root()
    request: dict[str, Any] = {"operation": operation, "project_root": str(root)}
    request.update(dict(arguments or {}))
    source_dir = Path(__file__).resolve().parents[1] / "src"
    child_environment = {**os.environ, "DOCMESH_HOOK_SUPPRESS": "1"}
    if source_dir.is_dir():
        existing = child_environment.get("PYTHONPATH")
        child_environment["PYTHONPATH"] = str(source_dir) + (
            os.pathsep + existing if existing else ""
        )
    command_template = os.environ.get("DOCMESH_CORE_COMMAND")
    if command_template:
        try:
            command_parts = shlex.split(command_template)
            payload = json.dumps(request, ensure_ascii=False)
            rendered: list[str] = []
            has_operation = False
            has_payload = False
            for part in command_parts:
                if "{operation}" in part:
                    has_operation = True
                    part = part.replace("{operation}", operation)
                if "{payload}" in part:
                    has_payload = True
                    part = part.replace("{payload}", payload)
                rendered.append(part)
            if not has_operation:
                rendered.append(operation)
            completed = subprocess.run(
                rendered,
                input="" if has_payload else payload,
                text=True,
                capture_output=True,
                cwd=root,
                env=child_environment,
                check=False,
            )
            return _subprocess_result(completed, operation)
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": f"unable to run DOCMESH_CORE_COMMAND: {exc}"}

    found, result = _call_imported_core(operation, request)
    if found:
        if isinstance(result, dict) and "ok" in result:
            return result
        return {"ok": True, "data": result}

    # Keep the fallback offline and explicit.  If the package is not installed,
    # returning a structured diagnostic lets advisory hooks continue safely.
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "docmesh", operation, "--json"],
            input=json.dumps(request, ensure_ascii=False),
            text=True,
            capture_output=True,
            cwd=root,
            env=child_environment,
            check=False,
        )
    except OSError as exc:
        return {"ok": False, "error": f"unable to start DocMesh core: {exc}"}
    return _subprocess_result(completed, operation)


def _subprocess_result(
    completed: subprocess.CompletedProcess[str], operation: str
) -> dict[str, Any]:
    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    parsed: Any = None
    if stdout:
        for line in reversed(stdout.splitlines()):
            with contextlib.suppress(json.JSONDecodeError):
                parsed = json.loads(line)
                break
    if completed.returncode == 0:
        if isinstance(parsed, Mapping) and "ok" in parsed:
            normalized = dict(parsed)
            normalized.setdefault("stderr", stderr)
            return normalized
        return {
            "ok": True,
            "data": parsed if parsed is not None else stdout,
            "stderr": stderr,
        }
    return {
        "ok": False,
        "error": f"core {operation} exited with status {completed.returncode}",
        "stderr": stderr,
        "stdout": stdout,
        "returncode": completed.returncode,
    }


def read_local_config(project: Path) -> dict[str, Any]:
    """Read the tiny harness-owned config subset without requiring TOML libs."""

    path = project / ".docmesh" / "local.toml"
    if not path.is_file():
        return {}
    try:
        import tomllib  # type: ignore

        with path.open("rb") as stream:
            value = tomllib.load(stream)
        return value if isinstance(value, dict) else {}
    except (ImportError, ValueError, OSError):
        # Python 3.9 fallback for the two scalar settings used by the harness.
        result: dict[str, Any] = {}
        section = ""
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return result
        for raw in lines:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
                continue
            if "=" not in line:
                continue
            key, raw_value = (item.strip() for item in line.split("=", 1))
            raw_value = raw_value.strip().strip('"').strip("'")
            target = result
            if section:
                target = result.setdefault(section, {})
            target[key] = raw_value
        return result


def enforcement_mode(project: Path) -> str:
    override = os.environ.get("DOCMESH_ENFORCEMENT_MODE")
    if override:
        return override.strip().lower()
    config = read_local_config(project)
    value = config.get("enforcement", {})
    if isinstance(value, Mapping):
        mode = value.get("mode", "advisory")
    else:
        mode = value
    if not mode or mode == "advisory":
        mode = config.get("enforcement_mode", config.get("mode", mode or "advisory"))
    return str(mode).strip().lower() if mode else "advisory"


def read_state(project: Path) -> dict[str, Any]:
    """Read harness/core status snapshots, tolerating absent initialization."""

    candidates = [
        project / ".docmesh" / "harness" / "state.json",
        project / ".docmesh" / "status.json",
        project / ".docmesh" / "state.json",
    ]
    for path in candidates:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            return value
    return {}


def record_core_result(
    project: Path, operation: str, result: Mapping[str, Any]
) -> None:
    """Persist harness-visible generation/verification facts from core output.

    The core owns its SQLite state machine; this small mirror lets a Stop hook
    answer quickly without guessing.  It never replaces the core result and
    only records fields that are explicit in the returned operation payload.
    """

    if not result.get("ok"):
        return
    value: Any = result.get("data", result)
    if dataclasses.is_dataclass(value):
        value = to_jsonable(value)
    if not isinstance(value, Mapping):
        return
    operation = operation.replace("-", "_")
    paths = HarnessPaths.for_project(project)
    state = read_state(project)
    if operation in {"index", "reindex"}:
        if value.get("edit_generation") is not None:
            state["edit_generation"] = value["edit_generation"]
        state["last_index_status"] = "indexed"
    elif operation == "impact_start":
        if value.get("edit_generation") is not None:
            state["edit_generation"] = value["edit_generation"]
        if value.get("phase") == "verify":
            state["verification_status"] = "pending"
        elif value.get("run_id"):
            state["baseline_run_id"] = value["run_id"]
            state["verification_status"] = "baseline_open"
    elif operation == "impact_finish":
        if value.get("edit_generation") is not None:
            state["edit_generation"] = value["edit_generation"]
        if value.get("baseline_run_id"):
            state["baseline_run_id"] = value["baseline_run_id"]
            state["verification_status"] = "baseline_sealed"
        if str(value.get("status", "")).lower() == "verified":
            state["verification_status"] = "verified"
            if value.get("edit_generation") is not None:
                state["verified_generation"] = value["edit_generation"]
    state["harness_updated_at"] = utc_now()
    with contextlib.suppress(OSError):
        atomic_write_json(paths.state, state)


def source_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def truthy(value: Any) -> bool:
    return str(value).strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
        "proved",
        "proven",
    }
