"""Regression tests for DocMesh's dependency-free Python resolver."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = ROOT / "plugins" / "docmesh" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pyresolve  # type: ignore  # noqa: E402


class CandidateTests(unittest.TestCase):
    def test_plugin_venv_keeps_lexical_path_when_ambient_venv_is_same_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "plugin"
            plugin_python = root / ".venv" / "bin" / "python"
            ambient_python = Path(directory) / "ambient" / "bin" / "python"
            target = Path(directory) / "python-base"
            target.write_text("", encoding="utf-8")
            plugin_python.parent.mkdir(parents=True)
            ambient_python.parent.mkdir(parents=True)
            plugin_python.symlink_to(target)
            ambient_python.symlink_to(target)
            with mock.patch.dict(os.environ, {"VIRTUAL_ENV": str(ambient_python.parent.parent)}, clear=False), mock.patch.object(
                pyresolve.shutil, "which", return_value=None
            ), mock.patch.object(pyresolve, "_uv_interpreter", return_value=None):
                values = pyresolve.candidates(root)

        self.assertEqual(values[0], str(plugin_python))
        self.assertIn(str(plugin_python), values)

    def test_candidates_include_the_current_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "plugin"
            current = Path(directory) / "current-python"
            current.write_text("", encoding="utf-8")
            with mock.patch.object(pyresolve.sys, "executable", str(current)), mock.patch.dict(
                os.environ, {}, clear=True
            ), mock.patch.object(pyresolve.shutil, "which", return_value=None), mock.patch.object(
                pyresolve, "_uv_interpreter", return_value=None
            ):
                values = pyresolve.candidates(root)
        self.assertIn(str(current), values)


class ResolutionTests(unittest.TestCase):
    def test_ready_environment_wins_over_newer_bare_interpreter(self) -> None:
        bare = "/opt/python3.16/bin/python"
        plugin = "/workspace/plugins/docmesh/.venv/bin/python"
        with mock.patch.object(pyresolve, "candidates", return_value=[bare, plugin]), mock.patch.object(
            pyresolve, "_accepts", return_value=True
        ), mock.patch.object(
            pyresolve,
            "_missing_deps",
            side_effect=[["fastembed"], []],
        ):
            self.assertEqual(pyresolve.resolve_interpreter(Path("/workspace/plugins/docmesh")), plugin)

    def test_bare_valid_interpreter_is_fallback_when_no_environment_is_ready(self) -> None:
        first = "/workspace/plugins/docmesh/.venv/bin/python"
        second = "/opt/python3.12/bin/python"
        with mock.patch.object(pyresolve, "candidates", return_value=[first, second]), mock.patch.object(
            pyresolve, "_accepts", return_value=True
        ), mock.patch.object(
            pyresolve,
            "_missing_deps",
            return_value=["fastembed", "pypdf", "sqlite_vec"],
        ):
            self.assertEqual(pyresolve.resolve_interpreter(Path("/workspace/plugins/docmesh")), first)


class ProbeTests(unittest.TestCase):
    def test_version_probe_timeout_is_a_rejected_candidate(self) -> None:
        with mock.patch.object(pyresolve.os.path, "isfile", return_value=True), mock.patch.object(
            pyresolve.os, "access", return_value=True
        ), mock.patch.object(
            pyresolve.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired("python", 5),
        ):
            self.assertFalse(pyresolve._accepts("/tmp/python"))


class ReexecTests(unittest.TestCase):
    def test_stale_unscoped_marker_does_not_block_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "plugin"
            target = root / ".venv" / "bin" / "python"
            target.parent.mkdir(parents=True)
            target.write_text("", encoding="utf-8")
            environment = {**os.environ, pyresolve.RESOLVED_MARKER: "1"}
            with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(
                pyresolve.sys, "version_info", (3, 9, 0)
            ), mock.patch.object(pyresolve.sys, "executable", "/usr/bin/python3"), mock.patch.object(
                pyresolve, "resolve_interpreter", return_value=str(target)
            ), mock.patch.object(pyresolve, "_missing_deps", return_value=[]), mock.patch.object(
                pyresolve.os, "execvpe"
            ) as execvpe:
                pyresolve.ensure_python(root, bootstrap=False)
        execvpe.assert_called_once()

    def test_dry_run_does_not_enable_bootstrap(self) -> None:
        self.assertFalse(pyresolve.bootstrap_allowed(["setup", "--dry-run", "--approve"]))

    def test_approved_setup_enables_bootstrap(self) -> None:
        self.assertTrue(pyresolve.bootstrap_allowed(["setup", "--approve"]))
        self.assertTrue(pyresolve.bootstrap_allowed(["init", "--approve"]))
        self.assertFalse(pyresolve.bootstrap_allowed(["status", "--approve"]))


if __name__ == "__main__":
    unittest.main()
