"""Black-box checks for the two harness manifests and shared skills."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
PLUGIN = ROOT / "plugins" / "docmesh"


class ManifestAndSkillTests(unittest.TestCase):
    def _json(self, path: Path) -> dict:
        with path.open(encoding="utf-8") as stream:
            value = json.load(stream)
        self.assertIsInstance(value, dict)
        return value

    def test_codex_manifest_declares_shared_assets(self) -> None:
        manifest = self._json(PLUGIN / ".codex-plugin" / "plugin.json")
        self.assertEqual(manifest["name"], "docmesh")
        self.assertEqual(manifest["version"], "1.0.4")
        self.assertEqual(manifest["skills"], "./skills/")
        self.assertEqual(manifest["mcpServers"], "./mcp.codex.json")
        self.assertEqual(manifest["hooks"], "./hooks/codex-hooks.json")

    def test_claude_manifest_and_hook_file_exist(self) -> None:
        manifest = self._json(PLUGIN / ".claude-plugin" / "plugin.json")
        self.assertEqual(manifest["name"], "docmesh")
        self.assertEqual(manifest["version"], "1.0.4")
        hooks = self._json(PLUGIN / "hooks" / "hooks.json")
        self.assertIn("PostToolUse", hooks["hooks"])
        self.assertIn("Stop", hooks["hooks"])
        for event in ("PostToolUse", "Stop"):
            command = hooks["hooks"][event][0]["hooks"][0]
            self.assertFalse(command.get("async", True))
            self.assertIn("CLAUDE_PLUGIN_ROOT", command["command"])

    def test_runtime_specific_mcp_manifests_use_absolute_plugin_scripts(self) -> None:
        claude = self._json(PLUGIN / ".mcp.json")
        self.assertEqual(set(claude), {"mcpServers"})
        claude_server = claude["mcpServers"]["docmesh"]
        self.assertEqual(
            claude_server["args"], ["${CLAUDE_PLUGIN_ROOT}/scripts/mcp_server.py"]
        )
        self.assertEqual(
            claude_server["env"]["DOCMESH_PLUGIN_ROOT"], "${CLAUDE_PLUGIN_ROOT}"
        )

        codex = self._json(PLUGIN / "mcp.codex.json")
        self.assertNotIn("mcpServers", codex)
        codex_server = codex["docmesh"]
        self.assertEqual(codex_server["args"], ["${PLUGIN_ROOT}/scripts/mcp_server.py"])
        self.assertEqual(codex_server["env"]["DOCMESH_PLUGIN_ROOT"], "${PLUGIN_ROOT}")

    def test_runtime_specific_hook_roots_are_not_crossed(self) -> None:
        claude = self._json(PLUGIN / "hooks" / "hooks.json")
        codex = self._json(PLUGIN / "hooks" / "codex-hooks.json")
        for event in ("PostToolUse", "Stop"):
            claude_command = claude["hooks"][event][0]["hooks"][0]["command"]
            codex_command = codex["hooks"][event][0]["hooks"][0]["command"]
            self.assertIn("${CLAUDE_PLUGIN_ROOT}/hooks/", claude_command)
            self.assertNotIn("${PLUGIN_ROOT}", claude_command)
            self.assertIn("${PLUGIN_ROOT}/hooks/", codex_command)
            self.assertNotIn("CODEX_PLUGIN_ROOT", codex_command)

    def test_marketplaces_point_at_plugin(self) -> None:
        marketplace = self._json(ROOT / ".claude-plugin" / "marketplace.json")
        self.assertEqual(marketplace["name"], "docmesh")
        self.assertEqual(marketplace["plugins"][0]["source"], "./plugins/docmesh")
        personal = self._json(ROOT / ".agents" / "plugins" / "marketplace.json")
        self.assertEqual(personal["plugins"][0]["source"]["path"], "./plugins/docmesh")

    def test_all_three_skills_are_present_and_untrusted_content_safe(self) -> None:
        names = {
            "docmesh-init": "corpus",
            "docmesh-search": "search",
            "docmesh-global-edit": "impact",
        }
        for name, required_word in names.items():
            path = PLUGIN / "skills" / name / "SKILL.md"
            self.assertTrue(path.is_file(), path)
            text = path.read_text(encoding="utf-8").lower()
            self.assertIn(required_word, text)
            self.assertIn("untrusted", text)
            self.assertIn("docmesh", text)


if __name__ == "__main__":
    unittest.main()
