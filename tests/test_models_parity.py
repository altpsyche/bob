"""ONE-C C0c — config/models.json is the neutral single source for model selection, read by Python (bob_models). This proves the
two sides resolve the registry + activeProfile to the same values, and that the writable activeProfile
(data/active-profile.json, D4) is shared bidirectionally. (The PowerShell half was retired in ONE-E — Python is now the only side.)"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _common  # noqa: F401 — puts scripts/ on sys.path
import bob_models

REPO = Path(bob_models.REPO)


class TestModelsPythonSide(unittest.TestCase):
    def test_loads_profiles_and_default_active(self):
        c = bob_models.load_models_config()
        self.assertIn("16gb", c["profiles"])
        self.assertEqual(c["activeProfile"], "16gb")  # shipped default in models.json

    def test_resolution_precedence(self):
        c = bob_models.load_models_config()
        self.assertEqual(bob_models.resolve_profile_name("24gb", c), "24gb")   # explicit arg
        with mock.patch.dict(os.environ, {"BOB_PROFILE": "8gb"}):
            self.assertEqual(bob_models.resolve_profile_name(config=c), "8gb")  # env
        with mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if k != "BOB_PROFILE"},
                             clear=True):
            self.assertEqual(bob_models.resolve_profile_name(config=c), "16gb")  # active default

    def test_unknown_profile_raises(self):
        with self.assertRaises(ValueError):
            bob_models.resolve_profile_name("999gb", bob_models.load_models_config())

    def test_profile_roles_skip_metadata(self):
        roles = bob_models.profile_roles("16gb")
        self.assertIn("planner", roles)
        self.assertNotIn("_targetVRAM", roles)
        self.assertEqual(roles["planner"]["gguf"], "qwen3-30b-a3b-q4.gguf")

    def test_set_active_profile_writes_override(self):
        with tempfile.TemporaryDirectory() as d:
            apf = Path(d) / "active-profile.json"
            with mock.patch.object(bob_models, "_active_profile_file", return_value=apf):
                bob_models.set_active_profile("12gb")
                self.assertEqual(json.loads(apf.read_text())["activeProfile"], "12gb")
                self.assertEqual(bob_models.load_models_config()["activeProfile"], "12gb")

    def test_set_unknown_profile_raises(self):
        with self.assertRaises(ValueError):
            bob_models.set_active_profile("nope")


if __name__ == "__main__":
    unittest.main()
