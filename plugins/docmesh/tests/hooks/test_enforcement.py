"""Strict/advisory Stop behavior and loop protection."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
STOP = ROOT / "plugins" / "docmesh" / "hooks" / "stop.py"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class EnforcementTests(unittest.TestCase):
    def _run(self, project: Path, payload: dict, extra: dict[str, str] | None = None):
        environment = {**os.environ, "DOCMESH_NO_PROBE": "1"}
        if extra:
            environment.update(extra)
        return subprocess.run(
            [sys.executable, str(STOP)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            cwd=project,
            env=environment,
            check=False,
        )

    def test_unknown_capability_never_blocks_and_warns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / ".docmesh").mkdir()
            (project / ".docmesh" / "local.toml").write_text(
                '[enforcement]\nmode = "strict"\n', encoding="utf-8"
            )
            write_json(project / ".docmesh" / "harness" / "state.json", {"dirty": True})
            result = self._run(project, {"hook_event_name": "Stop"})
            self.assertEqual(result.returncode, 0)
            self.assertIn("advisory", (result.stderr + result.stdout).lower())

    def test_proven_strict_mode_blocks_unverified_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            local = project / ".docmesh" / "local.toml"
            local.parent.mkdir()
            local.write_text('[enforcement]\nmode = "strict"\n', encoding="utf-8")
            write_json(
                project / ".docmesh" / "harness" / "capability-cache.json",
                {
                    "entries": [
                        {
                            "key": {
                                "harness": "claude",
                                "runtime": "claude-code",
                                "version": "test",
                                "transport": "command",
                                "hook_definition_hash": "*",
                            },
                            "capabilities": {
                                "plugin_installed": True,
                                "hook_present": True,
                                "hook_trusted": True,
                                "stop_dispatched": True,
                                "blocking_respected": True,
                                "loop_protected": True,
                                "proven": True,
                            },
                        }
                    ]
                },
            )
            write_json(
                project / ".docmesh" / "harness" / "state.json",
                {"edit_generation": 3, "verified_generation": 2, "dirty_files": []},
            )
            result = self._run(
                project,
                {"hook_event_name": "Stop"},
                {
                    "DOCMESH_RUNTIME_VERSION": "test",
                    "DOCMESH_RUNTIME": "claude",
                    "DOCMESH_LOOP_PROTECTION_PROOF": "1",
                },
            )
            self.assertEqual(result.returncode, 0)
            message = json.loads(result.stdout)
            self.assertEqual(message["decision"], "block")
            self.assertIn("verification", message["reason"].lower())

    def test_stop_hook_active_allows_once_to_break_recursion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            result = self._run(
                project,
                {"hook_event_name": "Stop", "stop_hook_active": True},
                {"DOCMESH_ENFORCEMENT_MODE": "strict"},
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
