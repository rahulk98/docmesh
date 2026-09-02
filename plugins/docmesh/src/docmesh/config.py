"""Manifest handling and safe corpus discovery.

Discovery is intentionally independent of model setup.  ``init`` can therefore
show a complete proposed corpus while offline and without changing the project.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .models import (
    ApprovalRequired,
    DiscoveryItem,
    DiscoveryReport,
    Exclusion,
    Manifest,
    SourceConfig,
)

SUPPORTED_FORMATS = {
    ".md": "markdown",
    ".mdx": "mdx",
    ".tex": "latex",
    ".latex": "latex",
    ".bib": "bibtex",
    ".txt": "text",
    ".text": "text",
    ".pdf": "pdf",
}

DEFAULT_EXCLUDES = (
    ".git",
    ".docmesh",
    ".venv",
    "venv",
    "node_modules",
    "build",
    "dist",
    "*.egg-info",
    "__pycache__",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
)

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"


def model_cache_dir(root: os.PathLike | str) -> Path:
    """Return the project-local cache used by explicit model setup."""

    return Path(root).expanduser().resolve(strict=False) / ".docmesh" / "models"


def model_ready_marker(root: os.PathLike | str, model: str = DEFAULT_MODEL) -> Path:
    digest = hashlib.sha256(str(model).encode("utf-8")).hexdigest()[:16]
    return model_cache_dir(root) / (".ready-" + digest)


def is_model_ready(root: os.PathLike | str, model: str = DEFAULT_MODEL) -> bool:
    """Check readiness without importing or constructing FastEmbed."""

    return model_ready_marker(root, model).is_file()


def canonical_path(
    path: os.PathLike | str, *, base: os.PathLike | str | None = None
) -> str:
    """Return a normalized absolute path without requiring it to exist."""

    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = Path(base or Path.cwd()) / candidate
    return str(candidate.expanduser().resolve(strict=False))


def source_format(path: os.PathLike | str) -> str | None:
    return SUPPORTED_FORMATS.get(Path(path).suffix.lower())


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _matches(path: str, patterns: Sequence[str]) -> bool:
    normalized = path.replace(os.sep, "/").lstrip("./")
    for pattern in patterns:
        pattern = pattern.replace(os.sep, "/").lstrip("./")
        if fnmatch.fnmatch(normalized, pattern) or fnmatch.fnmatch(
            "/" + normalized, "/" + pattern
        ):
            return True
        if pattern.startswith("**/") and fnmatch.fnmatch(normalized, pattern[3:]):
            return True
        # ``Path.match`` gives ``**/*.md`` useful recursive semantics on the
        # Python versions used by the plugin.
        try:
            if Path(normalized).match(pattern):
                return True
        except ValueError:
            pass
    return False


def infer_role(
    path: os.PathLike | str,
    fmt: str | None = None,
    *,
    root: os.PathLike | str | None = None,
) -> tuple[str, str, str | None]:
    """Infer a safe default role and explain the decision.

    Generated/mirror names are treated as non-editable.  PDFs are always
    references unless they clearly live in a generated mirror area.
    """

    path_obj = Path(path).expanduser()
    if root is not None:
        root_obj = Path(root).expanduser().resolve(strict=False)
        if not path_obj.is_absolute():
            path_obj = root_obj / path_obj
    path_obj = path_obj.resolve(strict=False)
    fmt = fmt or source_format(path_obj) or "text"
    if root is not None:
        try:
            relative_parts = path_obj.relative_to(root_obj).parts
        except ValueError:
            relative_parts = path_obj.parts
    else:
        relative_parts = path_obj.parts
    # Only exact directory components carry generated/mirror semantics.  A
    # filename such as ``rebuild.md`` or an ancestor such as ``build-42`` must
    # not become a mirror merely because it contains a marker substring.
    generated_markers = ("generated", "mirror", "derived", "build", "_gen")
    directory_parts = relative_parts[:-1]
    marker = next(
        (part.lower() for part in directory_parts if part.lower() in generated_markers),
        None,
    )
    if marker:
        return "mirror", "generated or mirror path detected", marker
    if fmt == "pdf":
        return "reference", "PDFs are reference evidence in V1", None
    return "editable", "supported editable text source", None


def discover_corpus(
    root: os.PathLike | str,
    *,
    include: Sequence[str] | None = None,
    exclude: Sequence[str] | None = None,
    configured_sources: Sequence[SourceConfig] | None = None,
) -> DiscoveryReport:
    """Discover supported documents with deterministic inclusions/exclusions."""

    root_path = Path(root).expanduser().resolve(strict=False)
    if not root_path.exists():
        raise FileNotFoundError(str(root_path))
    if not root_path.is_dir():
        raise NotADirectoryError(str(root_path))
    include_patterns = tuple(
        include
        or (
            "**/*.md",
            "**/*.mdx",
            "**/*.tex",
            "**/*.latex",
            "**/*.bib",
            "**/*.txt",
            "**/*.text",
            "**/*.pdf",
        )
    )
    exclude_patterns = tuple(exclude or DEFAULT_EXCLUDES)
    configured: dict[str, SourceConfig] = {}
    for item in configured_sources or ():
        configured[canonical_path(item.path, base=root_path)] = item

    included: list[DiscoveryItem] = []
    excluded: list[Exclusion] = []
    seen: set[str] = set()

    def consider(candidate: Path) -> None:
        relative = _relative(candidate, root_path)
        parts = Path(relative).parts
        excluded_part = next(
            (part for part in parts if part in DEFAULT_EXCLUDES), None
        )
        if excluded_part or _matches(relative, exclude_patterns):
            excluded.append(
                Exclusion(str(candidate), "excluded by manifest/default rule")
            )
            return
        fmt = source_format(candidate)
        if fmt is None:
            excluded.append(
                Exclusion(
                    str(candidate),
                    "unsupported format (V1 supports Markdown/MDX/LaTeX/BibTeX/text/PDF)",
                )
            )
            return
        # Exact configured external paths are authoritative; normal in-project
        # files still obey include globs.
        is_exact_configured = canonical_path(candidate) in configured
        if (
            include
            and not is_exact_configured
            and not _matches(relative, include_patterns)
        ):
            excluded.append(
                Exclusion(str(candidate), "not matched by manifest include rule")
            )
            return
        matched_config = configured.get(canonical_path(candidate))
        if matched_config and not matched_config.enabled:
            excluded.append(Exclusion(str(candidate), "disabled in manifest"))
            return
        if matched_config:
            role = matched_config.role
            reason = "role assigned by manifest"
            generated_from = matched_config.generated_from
        else:
            role, reason, generated_from = infer_role(candidate, fmt, root=root_path)
        included.append(
            DiscoveryItem(str(candidate), role, fmt, reason, generated_from)
        )

    def visit(directory: Path) -> None:
        for entry in sorted(directory.iterdir(), key=lambda item: item.name):
            if entry.is_dir() and not entry.is_symlink():
                relative = _relative(entry, root_path)
                parts = Path(relative).parts
                excluded_part = next(
                    (part for part in parts if part in DEFAULT_EXCLUDES), None
                )
                if excluded_part or _matches(relative, exclude_patterns):
                    # Record an excluded directory once instead of once per
                    # contained file, keeping dry-run reports bounded.
                    excluded.append(
                        Exclusion(str(entry), "excluded by manifest/default rule")
                    )
                    continue
                visit(entry)
                continue
            if entry.is_file():
                seen.add(str(entry))
                consider(entry)

    visit(root_path)

    # A manifest may deliberately point outside the project (for example to a
    # shared thesis bibliography).  Include those exact files without walking
    # their entire parent directory.
    for configured_item in configured.values():
        configured_path = Path(canonical_path(configured_item.path, base=root_path))
        if configured_path.is_file() and str(configured_path) not in seen:
            consider(configured_path)

    role_order = {"editable": 0, "reference": 1, "mirror": 2}
    included.sort(key=lambda item: (role_order.get(item.role, 99), item.path))
    estimated_bytes = sum(
        Path(item.path).stat().st_size for item in included if Path(item.path).exists()
    )
    return DiscoveryReport(
        str(root_path), included, excluded, estimated_bytes, len(included), True
    )


def _toml_string(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def manifest_to_toml(manifest: Manifest) -> str:
    """Serialize the portable subset with a small dependency-free writer."""

    lines = [
        "# Generated by docmesh init; paths are relative to this project when possible.",
        "version = 1",
        "model = " + _toml_string(manifest.model),
        "tokenizer = " + _toml_string(manifest.tokenizer),
        "max_embedding_tokens = " + str(manifest.max_embedding_tokens),
        "hard_embedding_tokens = " + str(manifest.hard_embedding_tokens),
        "breadcrumb_format = " + _toml_string(manifest.breadcrumb_format),
        "retrieval_prefix = " + _toml_string(manifest.retrieval_prefix),
        "chunking_version = " + _toml_string(manifest.chunking_version),
        "include = ["
        + ", ".join(_toml_string(item) for item in manifest.include)
        + "]",
        "exclude = ["
        + ", ".join(_toml_string(item) for item in manifest.exclude)
        + "]",
        "",
    ]
    for item in manifest.sources:
        lines.append("[[sources]]")
        path_obj = Path(item.path)
        root_obj = Path(manifest.root)
        try:
            path_value = (
                path_obj.resolve(strict=False)
                .relative_to(root_obj.resolve(strict=False))
                .as_posix()
            )
        except ValueError:
            path_value = str(path_obj)
        lines.append("path = " + _toml_string(path_value))
        lines.append("role = " + _toml_string(item.role))
        lines.append("enabled = " + ("true" if item.enabled else "false"))
        if item.generated_from:
            lines.append("generated_from = " + _toml_string(item.generated_from))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _read_toml(path: Path) -> Mapping[str, Any]:
    try:
        import tomllib  # type: ignore
    except ImportError:  # pragma: no cover - Python 3.11+ is the supported runtime
        try:
            import tomli as tomllib  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "Python 3.12 or tomli is required to read a manifest"
            ) from exc
    with path.open("rb") as handle:
        return tomllib.load(handle)


def manifest_from_mapping(
    data: Mapping[str, Any], *, root: os.PathLike | str
) -> Manifest:
    root_path = Path(root).expanduser().resolve(strict=False)
    source_values = []
    for value in data.get("sources", []) or []:
        if isinstance(value, str):
            value = {"path": value}
        path_value = str(value.get("path", ""))
        if not Path(path_value).is_absolute():
            path_value = str((root_path / path_value).resolve(strict=False))
        source_values.append(
            SourceConfig(
                path_value,
                str(value.get("role", "editable")),
                value.get("generated_from"),
                bool(value.get("enabled", True)),
            )
        )
    return Manifest(
        root=str(root_path),
        sources=source_values,
        include=list(data.get("include", []) or [])
        or list(Manifest(str(root_path)).include),
        exclude=list(data.get("exclude", []) or [])
        or list(Manifest(str(root_path)).exclude),
        model=str(data.get("model", "BAAI/bge-small-en-v1.5")),
        dimensions=data.get("dimensions"),
        tokenizer=str(data.get("tokenizer", "fastembed")),
        max_embedding_tokens=int(data.get("max_embedding_tokens", 400)),
        hard_embedding_tokens=int(data.get("hard_embedding_tokens", 480)),
        breadcrumb_format=str(data.get("breadcrumb_format", " > ")),
        retrieval_prefix=str(data.get("retrieval_prefix", "")),
        chunking_version=str(
            data.get(
                "chunking_version", "v1-recursive-lines-paragraphs-token-count-probe"
            )
        ),
    )


def load_manifest(root_or_path: os.PathLike | str) -> Manifest:
    value = Path(root_or_path).expanduser()
    if value.name == "manifest.toml":
        path = value
        root = value.parent.parent
    else:
        root = value
        path = value / ".docmesh" / "manifest.toml"
    if not path.exists():
        return Manifest(str(root.resolve(strict=False)))
    return manifest_from_mapping(_read_toml(path), root=root)


def initialize_project(
    root: os.PathLike | str,
    *,
    approve: bool = False,
    approval: bool | None = None,
    yes: bool | None = None,
    download_model: bool | None = None,
    model: str = DEFAULT_MODEL,
    cache_dir: os.PathLike | str | None = None,
) -> DiscoveryReport:
    """Discover and optionally write a manifest after explicit approval.

    Model downloads are never implicit.  Approval is the explicit opt-in for
    setup; callers can pass ``download_model=False`` when they only want to
    persist a manifest after a model was installed by another mechanism.
    """

    if approval is not None:
        approve = approval
    if yes is not None:
        approve = yes
    report = discover_corpus(root)
    if not approve:
        approval_error = ApprovalRequired(
            "docmesh init requires explicit approval before writing manifest or downloading a model"
        )
        approval_error.report = report  # type: ignore[attr-defined]
        raise approval_error
    root_path = Path(root).expanduser().resolve(strict=False)
    model_dir = (
        Path(cache_dir).expanduser().resolve(strict=False)
        if cache_dir
        else model_cache_dir(root_path)
    )
    report.model_cache_dir = str(model_dir)
    report.model_ready = is_model_ready(root_path, model)
    if download_model is not False:
        # Import lazily so discovery/status remain dependency- and
        # network-free.  FastEmbed itself receives local_files_only=False only
        # on this approved setup path.
        from .embeddings import FastEmbedBackend, ModelNotInstalledError

        model_dir.mkdir(parents=True, exist_ok=True)
        try:
            FastEmbedBackend(model, cache_dir=str(model_dir), local_files_only=False)
        except ModelNotInstalledError as exc:
            report.model_error = str(exc)
            model_error = ModelNotInstalledError(str(exc))
            model_error.report = report  # type: ignore[attr-defined]
            raise model_error from exc
        marker = model_ready_marker(root_path, model)
        # Keep readiness state under the canonical project cache even when a
        # caller supplied an alternate FastEmbed cache directory.
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(model, encoding="utf-8")
        report.model_ready = True
    docmesh_dir = root_path / ".docmesh"
    docmesh_dir.mkdir(parents=True, exist_ok=True)
    sources = [
        SourceConfig(item.path, item.role, item.generated_from)
        for item in report.included
    ]
    manifest = Manifest(str(root_path), sources=sources, model=model)
    manifest.manifest_path.write_text(manifest_to_toml(manifest), encoding="utf-8")
    # local.toml is deliberately tiny and contains no machine-specific secret.
    # The project .gitignore is expected to ignore the directory/local file.
    if not manifest.local_path.exists():
        manifest.local_path.write_text(
            "# Machine-local DocMesh state; model setup is explicit.\n",
            encoding="utf-8",
        )
    return report


# Friendly aliases used by integrations that call discovery as a service.
discover_sources = discover_corpus
init_project = initialize_project
