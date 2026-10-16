"""Plugin trust-policy tests: untrusted source must not execute at discovery."""
import json
import tempfile
import unittest
from pathlib import Path

from core.plugin_loader import discover_plugins


class PluginPolicyTests(unittest.TestCase):
    def test_untrusted_plugin_is_not_imported_or_executed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plugins = root / "plugins"
            config = root / "config"
            plugins.mkdir()
            config.mkdir()
            marker = root / "plugin-side-effect.txt"
            (plugins / "sample.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('executed')\n"
                "PLUGIN = {'name': 'sample', 'description': 'sample plugin', "
                "'parameters': {'type': 'OBJECT', 'properties': {}}, "
                "'permissions': ['filesystem.write']}\n"
                "def run(parameters): return 'ok'\n",
                encoding="utf-8",
            )
            registry = discover_plugins(plugins, set(), logger=lambda _m: None)
            self.assertFalse(marker.exists())
            self.assertFalse(registry.has("sample"))
            entry = registry.list_for_ui()[0]
            self.assertIn("Untrusted extension was not imported", entry["error"])

    def test_trusted_plugin_requires_declared_permissions_to_be_granted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plugins = root / "plugins"
            config = root / "config"
            plugins.mkdir()
            config.mkdir()
            (plugins / "sample.py").write_text(
                "PLUGIN = {'name': 'sample', 'description': 'sample plugin', "
                "'parameters': {'type': 'OBJECT', 'properties': {}}, "
                "'permissions': ['network']}\n"
                "def run(parameters): return 'ok'\n",
                encoding="utf-8",
            )
            policy = config / "plugin_permissions.json"
            policy.write_text(json.dumps({"plugins": {
                "sample": {"trusted": True, "permissions": []}
            }}), encoding="utf-8")
            registry = discover_plugins(plugins, set(), logger=lambda _m: None)
            self.assertFalse(registry.has("sample"))
            self.assertIn("Permission grant is missing", registry.list_for_ui()[0]["error"])

    def test_trusted_plugin_loads_only_with_explicit_grant(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plugins = root / "plugins"
            config = root / "config"
            plugins.mkdir()
            config.mkdir()
            (plugins / "sample.py").write_text(
                "PLUGIN = {'name': 'sample', 'description': 'sample plugin', "
                "'parameters': {'type': 'OBJECT', 'properties': {}}, "
                "'permissions': ['network']}\n"
                "def run(parameters): return 'ok'\n",
                encoding="utf-8",
            )
            (config / "plugin_permissions.json").write_text(json.dumps({"plugins": {
                "sample": {"trusted": True, "permissions": ["network"]}
            }}), encoding="utf-8")
            registry = discover_plugins(plugins, set(), logger=lambda _m: None)
            self.assertTrue(registry.has("sample"))
            self.assertTrue(registry.list_for_ui()[0]["trusted"])
            self.assertEqual(["network"], registry.list_for_ui()[0]["permissions"])


if __name__ == "__main__":
    unittest.main()
