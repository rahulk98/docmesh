"""Durability and retry semantics for the detached worker."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
WORKER = ROOT / "plugins" / "docmesh" / "scripts" / "worker.py"
SCRIPTS = ROOT / "plugins" / "docmesh" / "scripts"
sys.path.insert(0, str(SCRIPTS))
from harness import HarnessPaths, append_dirty_event, pending_events


class WorkerTests(unittest.TestCase):
    def _fixture_command(
        self, directory: Path, *, fail: bool = False
    ) -> tuple[str, Path]:
        log = directory / "core-calls.jsonl"
        command = directory / "fake-core.py"
        command.write_text(
            "import json, os, sys\n"
            "request = json.loads(sys.stdin.read())\n"
            "with open(os.environ['DOCMESH_TEST_LOG'], 'a', encoding='utf-8') as stream:\n"
            "    stream.write(json.dumps(request) + '\\n')\n"
            + (
                "raise SystemExit(9)\n"
                if fail
                else "print(json.dumps({'indexed': request.get('paths', [])}))\n"
            ),
            encoding="utf-8",
        )
        return f"{sys.executable} {command}", log

    def test_worker_acknowledges_only_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            first = project / "a.md"
            second = project / "b.md"
            first.write_text("a", encoding="utf-8")
            second.write_text("b", encoding="utf-8")
            paths = HarnessPaths.for_project(project)
            append_dirty_event([first, second], project=project)
            command, log = self._fixture_command(project)
            environment = {
                **os.environ,
                "DOCMESH_CORE_COMMAND": command,
                "DOCMESH_TEST_LOG": str(log),
            }
            result = subprocess.run(
                [sys.executable, str(WORKER), "--project-root", str(project), "--json"],
                env=environment,
                cwd=project,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(pending_events(paths), [])
            request = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(request["operation"], "index")
            self.assertEqual(
                request["paths"], sorted([str(first.resolve()), str(second.resolve())])
            )
            again = subprocess.run(
                [sys.executable, str(WORKER), "--project-root", str(project), "--json"],
                env=environment,
                cwd=project,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), 1)

    def test_worker_leaves_event_pending_when_core_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            source = project / "a.md"
            source.write_text("a", encoding="utf-8")
            paths = HarnessPaths.for_project(project)
            append_dirty_event([source], project=project)
            command, log = self._fixture_command(project, fail=True)
            result = subprocess.run(
                [sys.executable, str(WORKER), "--project-root", str(project), "--json"],
                env={
                    **os.environ,
                    "DOCMESH_CORE_COMMAND": command,
                    "DOCMESH_TEST_LOG": str(log),
                },
                cwd=project,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(len(pending_events(paths)), 1)
            self.assertTrue((paths.root / "worker-errors.jsonl").is_file())


if __name__ == "__main__":
    unittest.main()
