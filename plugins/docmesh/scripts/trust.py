"""Shared, dependency-free classification of MCP result fields.

Single source of truth (RT-MCP-16) for what counts as document-derived
content (untrusted) vs tool-controlled metadata (trusted): both
``src/docmesh/mcp.py`` (in-package adapter) and ``scripts/mcp_server.py``
(the shipped server) import this so they can never disagree again.
"""

from __future__ import annotations

UNTRUSTED_KEYS = frozenset(
    {
        "content",
        "document_content",
        "extracted_passage",
        "passage",
        "snippet",
        "source_snippet",
        "text",
        "untrusted_document_content",
        "excerpt",
        "line_text",
        "match",
        "breadcrumb",
    }
)

# Any key ending in one of these is document-derived, regardless of exact name
# (e.g. ``section_breadcrumb``, ``bounded_passage``, ``source_span_excerpt``).
UNTRUSTED_SUFFIXES = (
    "_content",
    "_snippet",
    "_passage",
    "_excerpt",
    "_line_text",
    "_breadcrumb",
)


def is_untrusted_key(value: str) -> bool:
    """Whether a result field key holds document text, not tool metadata."""

    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in UNTRUSTED_KEYS:
        return True
    return normalized.endswith(UNTRUSTED_SUFFIXES)


# --- Tool input schemas (F-mcp-5) -----------------------------------------
#
# One shared shape per tool so `mcp.py` and `mcp_server.py` cannot drift.
# Deliberately permissive (`additionalProperties: True`): core API functions
# accept extra kwargs (`deterministic`, `use_fastembed`, `model`, ...) that
# are not part of the documented contract but must keep working.

_ROOT = {"project_root": {"type": "string"}}

TOOL_SCHEMAS: dict[str, dict] = {
    "setup": {
        **_ROOT,
        "approve": {"type": "boolean"},
        "dry_run": {"type": "boolean"},
        "summary": {"type": "boolean"},
        "included_samples": {"type": "integer"},
        "excluded_samples": {"type": "integer"},
        "model": {"type": "string"},
        "cache_dir": {"type": "string"},
        "download_model": {"type": "boolean"},
    },
    "index": {
        **_ROOT,
        "paths": {"type": "array", "items": {"type": "string"}},
        "changed_paths": {"type": "array", "items": {"type": "string"}},
        "db_path": {"type": "string"},
        "force": {"type": "boolean"},
    },
    "status": {**_ROOT, "db_path": {"type": "string"}},
    "doctor": {
        **_ROOT,
        "runtime": {"type": "string"},
        "plugin_root": {"type": "string"},
        "refresh": {"type": "boolean"},
    },
    "search": {
        **_ROOT,
        "query": {"type": "string"},
        "limit": {"type": "integer"},
        "source_roles": {"type": "array", "items": {"type": "string"}},
        "roles": {"type": "array", "items": {"type": "string"}},
        "snippet_only": {"type": "boolean"},
        "max_snippet_length": {"type": "integer"},
    },
    "find": {
        **_ROOT,
        "pattern": {"type": "string"},
        "mode": {"type": "string", "enum": ["literal", "regex"]},
        "cursor": {"type": ["string", "integer"]},
        "source_roles": {"type": "array", "items": {"type": "string"}},
        "roles": {"type": "array", "items": {"type": "string"}},
        "scope": {"type": "string"},
    },
    "read": {
        **_ROOT,
        "path": {"type": "string"},
        "start_line": {"type": "integer"},
        "end_line": {"type": "integer"},
        "page": {"type": "integer"},
    },
    "impact_start": {
        **_ROOT,
        "phase": {"type": "string", "enum": ["discover", "verify"]},
        "query_bundle": {"type": "object"},
        "source_roles": {"type": "array", "items": {"type": "string"}},
        "roles": {"type": "array", "items": {"type": "string"}},
        "page_size": {"type": "integer"},
        "baseline_run_id": {"type": "string"},
    },
    "impact_page": {
        **_ROOT,
        "run_id": {"type": "string"},
        "cursor": {"type": ["string", "integer"]},
        "page_size": {"type": "integer"},
        "snippet_only": {"type": "boolean"},
    },
    "impact_read": {
        **_ROOT,
        "run_id": {"type": "string"},
        "candidate_id": {"type": "string"},
        "context_lines": {"type": "integer"},
    },
    "impact_classify": {
        **_ROOT,
        "run_id": {"type": "string"},
        "decisions": {"type": ["object", "array"]},
    },
    "impact_finish": {
        **_ROOT,
        "run_id": {"type": "string"},
        "decisions": {"type": ["object", "array"]},
    },
}
TOOL_SCHEMAS["init"] = TOOL_SCHEMAS["setup"]
TOOL_SCHEMAS["probe-hooks"] = {
    **_ROOT,
    "runtime": {"type": "string"},
    "plugin_root": {"type": "string"},
    "refresh": {"type": "boolean"},
}

TOOL_REQUIRED: dict[str, list[str]] = {
    "find": ["pattern"],
    "read": ["path"],
    "impact_page": ["run_id"],
    "impact_read": ["run_id", "candidate_id"],
    "impact_classify": ["run_id"],
    "impact_finish": ["run_id"],
}


def tool_input_schema(name: str) -> dict:
    """Full JSON Schema (type/properties/required) for one tool's arguments."""

    properties = TOOL_SCHEMAS.get(name, dict(_ROOT))
    return {
        "type": "object",
        "properties": properties,
        "required": TOOL_REQUIRED.get(name, []),
        "additionalProperties": True,
    }


def _type_ok(value, expected: object) -> bool:
    types = expected if isinstance(expected, list) else [expected]
    for type_name in types:
        if type_name == "string" and isinstance(value, str):
            return True
        if type_name == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if type_name == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if type_name == "boolean" and isinstance(value, bool):
            return True
        if type_name == "object" and isinstance(value, dict):
            return True
        if type_name == "array" and isinstance(value, (list, tuple)):
            return True
        if type_name == "null" and value is None:
            return True
    return False


def validate_arguments(name: str, arguments: dict) -> str | None:
    """Return an error message if ``arguments`` violates the tool's schema."""

    schema = tool_input_schema(name)
    missing = [key for key in schema["required"] if key not in arguments]
    if missing:
        return f"missing required argument(s): {', '.join(missing)}"
    properties = schema["properties"]
    for key, value in arguments.items():
        if value is None:
            continue
        prop = properties.get(key)
        if prop is None:
            continue
        expected = prop.get("type")
        if expected and not _type_ok(value, expected):
            return f"argument '{key}' must be of type {expected}, got {type(value).__name__}"
        enum = prop.get("enum")
        if enum and value not in enum:
            return f"argument '{key}' must be one of {enum}"
    return None
