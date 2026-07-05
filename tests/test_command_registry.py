"""NB4 (contracts C1, C6) — the command registry is enumerable and is the single source that
config/verbs.json is generated from. Proves every command declares a valid runtime, python
commands map to a real handler, and the on-disk verbs.json is in sync with the registry."""
import json
import re
import shutil
import unittest
from pathlib import Path

import _common  # noqa: F401 — puts scripts/ on sys.path
from bob import cli, registry


class TestRegistry(unittest.TestCase):
    def test_enumerable_and_well_formed(self):
        cmds = registry.commands()
        self.assertTrue(cmds)
        for c in cmds:
            for field in ("name", "group", "summary", "args", "runtime", "handler"):
                self.assertIn(field, c, f"{c.get('name')} missing {field}")
            self.assertIn(c["runtime"], {"python", "pwsh"}, c["name"])
            if c["runtime"] == "python":
                self.assertIn(c["handler"], cli._HANDLERS, f"{c['name']} handler not wired")
            else:
                self.assertIsNone(c["handler"], f"pwsh {c['name']} should have no handler")

    def test_runtime_spot_checks(self):
        rt = registry.verbs_json_dict()["commands"]
        self.assertEqual(rt["agent serve"], "python")
        self.assertEqual(rt["agent mcp"], "python")
        self.assertEqual(rt["clip"], "python")
        self.assertEqual(rt["serve"], "python")     # ONE-C Slice 2 — lifecycle ported to Python (stack.py)
        self.assertEqual(rt["stop"], "python")
        self.assertEqual(rt["up"], "python")
        self.assertEqual(rt["setup"], "python")      # ONE-C Slice 3 — setup(check)/doctor/version/diagnose (health.py)
        self.assertEqual(rt["doctor"], "python")
        self.assertEqual(rt["diagnose"], "python")
        self.assertEqual(rt["version"], "python")
        self.assertEqual(rt["build"], "pwsh")        # build/update/mlock stay pwsh through ONE-D (D2)
        self.assertEqual(rt["status"], "pwsh")       # status needs live VRAM query — later
        self.assertEqual(rt["chat"], "python")      # S2 — chat/code/think ported onto the agent loop
        self.assertEqual(rt["code"], "python")
        self.assertEqual(rt["think"], "python")
        self.assertEqual(rt["agent schedule"], "pwsh")

    def test_verbs_json_on_disk_in_sync(self):
        disk = json.loads((Path(registry.REPO) / "config" / "verbs.json").read_text(encoding="utf-8"))
        self.assertEqual(disk, registry.verbs_json_dict(),
                         "config/verbs.json is stale — regenerate: python -m bob.registry")

    def test_check_gate(self):
        import tempfile

        # in sync (the real committed file) -> 0
        self.assertEqual(registry._check(), 0)
        # a stale/mismatched file -> 1 (this is what the pre-commit gate catches)
        stale = Path(tempfile.mkdtemp(prefix="bob-verbs-")) / "verbs.json"
        stale.write_text(json.dumps({"commands": {}, "default": "python"}), encoding="utf-8")
        try:
            self.assertEqual(registry._check(stale), 1)
        finally:
            shutil.rmtree(stale.parent, ignore_errors=True)


class TestSwitchParity(unittest.TestCase):
    """NE1 — the registry is the true catalog: every top-level verb in the bob.ps1 dispatch switch
    must be registered (else it would be missing from help/catalog). Hidden entries still count."""

    def _switch_labels(self) -> set:
        text = (Path(registry.REPO) / "scripts" / "bob.ps1").read_text(encoding="utf-8")
        region = text[text.index("switch ($cmd)"):]  # main dispatch switch to EOF
        labels: set = set()
        for line in region.splitlines():
            # top-level case headers are 2-space-indented: `  'name' {` (nested switches are deeper)
            m = re.match(r"^  ('[a-z0-9-]+'(?:\s*,\s*'[a-z0-9-]+')*)\s*\{", line)
            if m:
                labels.update(re.findall(r"'([a-z0-9-]+)'", m.group(1)))
        return labels

    def test_every_switch_verb_registered(self):
        reg = {c["name"] for c in registry.commands()}
        labels = self._switch_labels()
        self.assertTrue(labels, "no switch labels parsed — regex/anchor drift in bob.ps1")
        missing = sorted(v for v in labels if v not in reg)
        self.assertEqual(missing, [], f"bob.ps1 verbs missing from the command registry: {missing}")

    def test_help_is_registry_driven_not_a_here_string(self):
        # WI-7: `help` is a registered python command (routes to `python -m bob help`), and the old
        # hand-maintained pwsh here-string catalog is retired from bob.ps1.
        self.assertEqual(registry.by_name()["help"]["runtime"], "python")
        self.assertEqual(registry.by_name()["help"]["handler"], "help")
        ps1 = (Path(registry.REPO) / "scripts" / "bob.ps1").read_text(encoding="utf-8")
        self.assertNotIn("personal AI assistant (endpoint", ps1)  # the retired here-string marker


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
