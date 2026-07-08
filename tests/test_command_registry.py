"""The command registry is THE single source for dispatch + help. Proves every entry is well-formed and
maps to a real cli.py handler, and that no generated verb table or PowerShell scaffolding remains
(registry.COMMANDS is the only catalog — nothing to keep in sync). This is also the one place that pins
every verb to a live handler, so the per-domain test files don't each re-assert their own wiring."""
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
        # The load-bearing verbs across every domain map to a live handler. test_enumerable_and_well_formed
        # already proves this for the WHOLE catalog; this list makes the per-domain coverage explicit in one
        # place (lifecycle, models, provisioning, build, health, scheduling, memory/meta) so the domain test
        # files can drop their own redundant wiring checks.
        by = registry.by_name()
        for name in (
            # entry / server / misc
            "agent serve", "agent mcp", "clip", "help",
            # lifecycle
            "serve", "stop", "up", "restart", "status", "ps", "logs", "webui",
            "litellm", "whisper", "piper", "services",
            # health
            "setup", "doctor", "diagnose", "version",
            # models + eval
            "models", "show", "profiles", "profile", "verify-urls", "bench", "eval",
            # build / update / provisioning
            "build", "fabric-setup", "update", "fetch", "lock", "mlock", "setup-voice",
            # config generation
            "gen",
            # memory + meta
            "remember", "recall", "memory", "budget", "tools", "plugins", "fabric", "aider",
            # conversation
            "chat", "code", "think",
            # scheduling
            "agent schedule", "agent log", "agent install", "agent uninstall", "agent status",
        ):
            self.assertIn(name, by, name)
            self.assertIn(by[name]["handler"], cli._HANDLERS, name)

    def test_no_verbs_json_machinery(self):
        # The verb table was collapsed: the generated file + its generator/sync gate are gone.
        self.assertFalse((Path(registry.REPO) / "config" / "verbs.json").exists())
        for gone in ("verbs_json_dict", "write_verbs", "_check", "VERBS_FILE"):
            self.assertFalse(hasattr(registry, gone), f"registry.{gone} should be removed")


class TestNoPowerShellFrontDoor(unittest.TestCase):
    """The PowerShell front door (bob.ps1) + seam library are retired; registry.COMMANDS is the sole
    catalog."""

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
