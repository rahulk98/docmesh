"""Exercise combined classification/finish through the installed MCP adapter."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys


SERVER = Path(__file__).resolve().parents[2] / "scripts" / "mcp_server.py"


def test_mcp_finish_forwards_batch_decisions_and_keeps_response_sanitized(tmp_path):
    captured = tmp_path / "request.json"
    provider = tmp_path / "provider.py"
    provider.write_text(
        "import json, pathlib, sys\n"
        "request = json.load(sys.stdin)\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps(request))\n"
        "print(json.dumps({'ok': True, 'data': {'run_id': request['run_id'], "
        "'status': 'sealed', 'snippet': 'untrusted evidence'}}))\n",
        encoding="utf-8",
    )
    decisions = [{"candidate_id": "candidate-1", "classification": "needs_edit"}]
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "impact_finish",
                "arguments": {
                    "project_root": str(tmp_path),
                    "run_id": "run-1",
                    "decisions": decisions,
                },
            },
        },
    ]
    result = subprocess.run(
        [sys.executable, str(SERVER)],
        input="".join(json.dumps(request) + "\n" for request in requests),
        text=True,
        capture_output=True,
        cwd=tmp_path,
        env={
            **os.environ,
            "DOCMESH_NO_RECONCILE": "1",
            "DOCMESH_NO_NETWORK": "1",
            "DOCMESH_CORE_COMMAND": shlex.join(
                [sys.executable, str(provider), str(captured)]
            ),
        },
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    finish = next(
        tool for tool in responses[0]["result"]["tools"]
        if tool["name"] == "impact_finish"
    )
    assert "decisions" in finish["inputSchema"]["properties"]
    assert not responses[1]["result"]["isError"]
    request = json.loads(captured.read_text(encoding="utf-8"))
    assert request["operation"] == "impact_finish"
    assert request["run_id"] == "run-1"
    assert request["decisions"] == decisions
    payload = json.loads(responses[1]["result"]["content"][0]["text"])
    assert payload["trusted_metadata"]["status"] == "sealed"
    assert "snippet" not in payload["trusted_metadata"]
    assert payload["untrusted_document_content"][0]["value"] == "untrusted evidence"
