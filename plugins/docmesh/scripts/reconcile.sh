#!/usr/bin/env bash
set -euo pipefail

plugin_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python_bin="$(python3 "$plugin_root/pyresolve.py" --print-path 2>/dev/null)" || {
    echo "DocMesh requires a Python 3.12+ interpreter (see plugins/docmesh/README.md)." >&2
    exit 2
}
exec "$python_bin" "$plugin_root/entrypoint.py" freshness "$@"
