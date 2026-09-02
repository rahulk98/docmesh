"""Subprocess tests for the synchronous dirty-event hook contract."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
HOOK = ROOT / "plugins" / "docmesh" / "hooks" / "post-edit.py"
WORKER = ROOT / "plugins" / "docmesh" / "scripts" / "worker.py"


class DirtyEventHookTests(unittest.TestCase):
    def test_hook_durably_records_external_and_project_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            external = Path(directory) / "references" / "source.md"
            project.mkdir()
            external.parent.mkdir()
            external.write_text("reference", encoding="utf-8")
            source = project / "README.md"
            source.write_text("editable", encoding="utf-8")
            payload = {
                "hook_event_name": "PostToolUse",
                "cwd": str(project),
                "tool_name": "Edit",
                "tool_input": {"file_path": str(source)},
                "tool_response": {"path": str(external)},
            }
            env = {
                "DOCMESH_NO_WORKER": "1",
                "DOCMESH_RUNTIME": "claude",
            }
            result = subprocess.run(
                [sys.executable, str(HOOK)],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                cwd=project,
                env={**__import__("os").environ, **env},
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            queue = project / ".docmesh" / "harness" / "dirty-events.jsonl"
            self.assertTrue(queue.is_file())
            events = [
                json.loads(line)
                for line in queue.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertEqual(event["schema_version"], 1)
            self.assertEqual(event["event_type"], "dirty_files")
            self.assertEqual(
                event["files"],
                sorted({str(source.resolve()), str(external.resolve())}),
            )
            self.assertTrue(event["event_id"])
            self.assertTrue(event["durability"]["fsynced"])

    def test_hook_is_idempotently_empty_for_non_document_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            payload = {
                "hook_event_name": "PostToolUse",
                "cwd": str(project),
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"},
            }
            result = subprocess.run(
                [sys.executable, str(HOOK)],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                cwd=project,
                env={**__import__("os").environ, "DOCMESH_NO_WORKER": "1"},
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((project / ".docmesh").exists())

    def test_hook_returns_before_detached_worker_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            source = project / "README.md"
            source.write_text("editable", encoding="utf-8")
            core = project / "fake-core.py"
            calls = project / "calls.jsonl"
            core.write_text(
                "import json, os, sys, time\n"
                "request = json.loads(sys.stdin.read())\n"
                "time.sleep(0.5)\n"
                "with open(os.environ['DOCMESH_TEST_LOG'], 'a', encoding='utf-8') as stream:\n"
                "    stream.write(json.dumps(request) + '\\n')\n"
                "print(json.dumps({'ok': True}))\n",
                encoding="utf-8",
            )
            payload = {
                "hook_event_name": "PostToolUse",
                "cwd": str(project),
                "tool_name": "Edit",
                "tool_input": {"file_path": str(source)},
            }
            started = time.monotonic()
            result = subprocess.run(
                [sys.executable, str(HOOK)],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                cwd=project,
                env={
                    **__import__("os").environ,
                    "DOCMESH_CORE_COMMAND": f"{sys.executable} {core}",
                    "DOCMESH_TEST_LOG": str(calls),
                },
                check=False,
            )
            elapsed = time.monotonic() - started
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLess(elapsed, 0.3)
            for _ in range(30):
                if calls.exists() and calls.read_text(encoding="utf-8").strip():
                    break
                time.sleep(0.05)
            self.assertTrue(calls.exists())
            worker_state = project / ".docmesh" / "harness" / "state.json"
            for _ in range(30):
                if (
                    worker_state.exists()
                    and '"last_worker_status": "indexed"'
                    in worker_state.read_text(encoding="utf-8")
                ):
                    break
                time.sleep(0.05)
            self.assertTrue(worker_state.exists())


if __name__ == "__main__":
    unittest.main()
