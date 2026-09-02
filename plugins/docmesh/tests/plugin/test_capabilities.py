"""Runtime/trust capability cache tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
PROBE = ROOT / "plugins" / "docmesh" / "scripts" / "capability_probe.py"


class CapabilityProbeTests(unittest.TestCase):
    def test_probe_is_keyed_by_runtime_version_transport_and_definition_hash(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            plugin = project / "plugin"
            (plugin / "hooks").mkdir(parents=True)
            (plugin / "hooks" / "hooks.json").write_text(
                '{"hooks":{"Stop":[],"PostToolUse":[],"SessionStart":[]}}',
                encoding="utf-8",
            )
            environment = {
                **os.environ,
                "DOCMESH_PLUGIN_INSTALLED": "1",
                "DOCMESH_HOOK_TRUSTED": "1",
                "DOCMESH_STOP_DISPATCH_PROOF": "1",
                "DOCMESH_BLOCKING_PROOF": "1",
                "DOCMESH_LOOP_PROTECTION_PROOF": "1",
                "DOCMESH_RUNTIME_VERSION": "fixture-1",
            }
            command = [
                sys.executable,
                str(PROBE),
                "--project-root",
                str(project),
                "--plugin-root",
                str(plugin),
                "--runtime",
                "claude",
                "--json",
            ]
            first = subprocess.run(
                command, env=environment, text=True, capture_output=True, check=False
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            value = json.loads(first.stdout)
            self.assertTrue(value["capabilities"]["proven"])
            cache = project / ".docmesh" / "harness" / "capability-cache.json"
            self.assertTrue(cache.is_file())
            second = subprocess.run(
                command, env=environment, text=True, capture_output=True, check=False
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertTrue(json.loads(second.stdout)["cache_hit"])
            (plugin / "hooks" / "hooks.json").write_text(
                '{"hooks":{"Stop":[],"PostToolUse":[]}}', encoding="utf-8"
            )
            third = subprocess.run(
                command, env=environment, text=True, capture_output=True, check=False
            )
            self.assertEqual(third.returncode, 0, third.stderr)
            self.assertFalse(json.loads(third.stdout)["cache_hit"])
            changed_runtime = {**environment, "DOCMESH_RUNTIME_VERSION": "fixture-2"}
            fourth = subprocess.run(
                command,
                env=changed_runtime,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(fourth.returncode, 0, fourth.stderr)
            self.assertFalse(json.loads(fourth.stdout)["cache_hit"])

    def test_local_proof_adapter_must_return_explicit_json_fact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            plugin = project / "plugin"
            (plugin / "hooks").mkdir(parents=True)
            (plugin / "hooks" / "hooks.json").write_text(
                '{"hooks":{"Stop":[],"PostToolUse":[]}}', encoding="utf-8"
            )
            proof = project / "proof.py"
            proof.write_text(
                "import json, sys\n"
                "json.loads(sys.stdin.read())\n"
                "print(json.dumps({'hook_trusted': True, 'stop_dispatched': True, 'blocking_respected': True, 'loop_protected': True}))\n",
                encoding="utf-8",
            )
            environment = {
                **os.environ,
                "DOCMESH_PLUGIN_INSTALLED": "1",
                "DOCMESH_TRUST_PROBE_COMMAND": f"{sys.executable} {proof}",
                "DOCMESH_STOP_PROBE_COMMAND": f"{sys.executable} {proof}",
                "DOCMESH_BLOCK_PROBE_COMMAND": f"{sys.executable} {proof}",
                "DOCMESH_LOOP_PROBE_COMMAND": f"{sys.executable} {proof}",
                "DOCMESH_RUNTIME_VERSION": "proof-1",
            }
            result = subprocess.run(
                [
                    sys.executable,
                    str(PROBE),
                    "--project-root",
                    str(project),
                    "--plugin-root",
                    str(plugin),
                    "--runtime",
                    "codex",
                    "--json",
                ],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)["capabilities"]["proven"])


if __name__ == "__main__":
    unittest.main()
