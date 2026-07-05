"""C6 (ONE-E) — the command registry is THE single source for dispatch + help. Proves every entry is
well-formed and maps to a real cli.py handler, and that the whole config/verbs.json + pwsh scaffolding is
gone (registry.COMMANDS is now the only catalog — no generated table to keep in sync)."""
import unittest
from pathlib import Path

import _common  # noqa: F401 — puts scripts/ on sys.path
from bob import cli, registry


class TestRegistry(unittest.TestCase):
    def test_enumerable_and_well_formed(self):
        cmds = registry.commands()
        self.assertTrue(cmds)
        for c in cmds:
            for field in ("name", "group", "summary", "args", "handler"):
                self.assertIn(field, c, f"{c.get('name')} missing {field}")
            self.assertNotIn("runtime", c, f"{c['name']} still carries the retired runtime field")
            self.assertIn(c["handler"], cli._HANDLERS, f"{c['name']} handler not wired")

    def test_every_verb_maps_to_a_handler(self):
        # spot-check the load-bearing verbs (all Python since ONE-D/E — no pwsh, no verbs.json table).
        by = registry.by_name()
        for name in ("agent serve", "agent mcp", "clip", "serve", "stop", "up", "setup", "doctor",
                     "diagnose", "version", "build", "fetch", "lock", "mlock", "eval", "update",
                     "fabric-setup", "status", "chat", "code", "think", "agent schedule", "help"):
            self.assertIn(name, by, name)
            self.assertIn(by[name]["handler"], cli._HANDLERS, name)

    def test_no_verbs_json_machinery(self):
        # ONE-E collapsed the verb table: the generated file + its generator/sync gate are gone.
        self.assertFalse((Path(registry.REPO) / "config" / "verbs.json").exists())
        for gone in ("verbs_json_dict", "write_verbs", "_check", "VERBS_FILE"):
            self.assertFalse(hasattr(registry, gone), f"registry.{gone} should be removed")


class TestNoPowerShellFrontDoor(unittest.TestCase):
    """ONE-E — the PowerShell front door (bob.ps1) + seam library are retired; registry.COMMANDS is the
    sole catalog."""

    def test_help_is_registry_driven(self):
        self.assertEqual(registry.by_name()["help"]["handler"], "help")

    def test_no_pwsh_scripts_under_scripts(self):
        self.assertEqual(list((Path(registry.REPO) / "scripts").glob("*.ps1")), [])


class TestResolve(unittest.TestCase):
    def test_two_token_command_wins(self):
        self.assertEqual(cli._resolve(["agent", "serve"]), ("agent serve", []))
        self.assertEqual(cli._resolve(["agent", "mcp", "x"]), ("agent mcp", ["x"]))

    def test_bare_verb_with_trailing_args(self):
        # 'agent <goal>' — the goal is not a subcommand, so it stays the bare 'agent'
        self.assertEqual(cli._resolve(["agent", "fix", "the", "bug"]),
                         ("agent", ["fix", "the", "bug"]))

    def test_single_verb(self):
        self.assertEqual(cli._resolve(["status"]), ("status", []))

    def test_empty(self):
        self.assertEqual(cli._resolve([]), (None, []))


if __name__ == "__main__":
    unittest.main()
