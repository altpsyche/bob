"""config/defaults.json is the neutral single source of truth for ports + roles, read by Python
(bob_core.load_defaults / _port / get_role). This proves the resolution and that a dropped key fails
loudly."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import _common  # noqa: F401 — puts scripts/ on sys.path
import bob_config
import bob_core

REPO = Path(bob_core.REPO)
DEFAULTS = REPO / "config" / "defaults.json"

# A fully-populated routing/vision config so the resolver lands on the routing *values*
# (the fallback literals only matter for sparse configs, which production never has).
_CFG = {
    "routing": {
        "defaultRole": "chat", "proRole": "chat-pro",
        "codeRole": "coder", "proCodeRole": "coder-pro",
        "ponderRole": "ponder", "proPonderRole": "ponder-pro",
        "agentRole": "agent",
    },
    "vision": {"visionRole": "vision", "visionProRole": "vision-pro"},
}
# Tasks the role resolver accepts (no 'agent' there).
_TASKS = ["chat", "code", "ponder", "vision", "voice"]


class TestDefaultsPythonSide(unittest.TestCase):
    def test_ports_load_and_resolve(self):
        ports = bob_core.load_defaults()["ports"]
        for name in ("port", "litellmPort", "agentPort", "searxngPort", "sttPort",
                     "ttsPort", "webuiPort", "langfusePort", "n8nPort"):
            self.assertIn(name, ports)
            self.assertEqual(bob_core._port({}, name), ports[name])

    def test_role_table_drives_get_role(self):
        for task in _TASKS:
            self.assertEqual(bob_core.get_role(_CFG, task),
                             bob_core.get_role(_CFG, task, pro=False))

    def test_missing_ports_section_raises_clearly(self):
        bad = Path(tempfile.mkdtemp(prefix="bob-def-")) / "defaults.json"
        bad.write_text(json.dumps({"roleTable": {}}), encoding="utf-8")
        orig_file, orig_cache = bob_core._DEFAULTS_FILE, bob_core._defaults_cache
        try:
            bob_core._DEFAULTS_FILE = bad
            bob_core._defaults_cache = None
            with self.assertRaises(RuntimeError) as ctx:
                bob_core.load_defaults()
            self.assertIn("ports", str(ctx.exception))
        finally:
            bob_core._DEFAULTS_FILE, bob_core._defaults_cache = orig_file, orig_cache
            shutil.rmtree(bad.parent, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
