"""End-to-end launcher checks that do not require the core dependencies."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SETUP = ROOT / "plugins" / "docmesh" / "scripts" / "setup.sh"
INDEX = ROOT / "plugins" / "docmesh" / "scripts" / "index.sh"


class LauncherTests(unittest.TestCase):
    def test_setup_dry_run_is_offline_and_reports_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [str(SETUP), "--dry-run"],
                cwd=directory,
                env={**os.environ, "DOCMESH_NO_NETWORK": "1"},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("dry", (result.stdout + result.stderr).lower())
            self.assertFalse((Path(directory) / ".docmesh" / "manifest.toml").exists())

    def test_approved_setup_forwards_explicit_model_without_downloading_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            calls = project / "calls.json"
            core = project / "fake-core.py"
            core.write_text(
                "import json, os, sys\n"
                "request = json.loads(sys.stdin.read())\n"
                "json.dump(request, open(os.environ['DOCMESH_CALLS'], 'w', encoding='utf-8'))\n"
                "print(json.dumps({'ok': True, 'model': request.get('model')}))\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    str(SETUP),
                    "--approve",
                    "--model",
                    "fixture/model",
                    "--use-fastembed",
                    "--cache-dir",
                    str(project / "model-cache"),
                    "--json",
                ],
                cwd=project,
                env={
                    **os.environ,
                    "DOCMESH_NO_NETWORK": "1",
                    "DOCMESH_CALLS": str(calls),
                    "DOCMESH_CORE_COMMAND": f"{sys.executable} {core}",
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            request = __import__("json").loads(calls.read_text(encoding="utf-8"))
            self.assertEqual(request["model"], "fixture/model")
            self.assertTrue(request["use_fastembed"])
            self.assertEqual(request["cache_dir"], str(project / "model-cache"))
            self.assertFalse((project / "model-cache").exists())

    def test_index_launcher_exposes_json_errors_without_shell_trace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [str(INDEX), "--json"],
                cwd=directory,
                env={**os.environ, "DOCMESH_NO_NETWORK": "1"},
                text=True,
                capture_output=True,
                check=False,
            )
            # A checkout may already contain an installed deterministic core;
            # either a structured success or a structured failure is valid at
            # this harness boundary.
            self.assertIn(result.returncode, (0, 1))
            if result.stdout.strip():
                __import__("json").loads(result.stdout)
            self.assertNotIn("set -", result.stderr)


if __name__ == "__main__":
    unittest.main()
