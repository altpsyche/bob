"""ONE-C C0c — config/models.json is the neutral single source for model selection, read identically by
Python (bob_models) and PowerShell (Get-ModelsConfig via ConvertFrom-Json -AsHashtable). This proves the
two sides resolve the registry + activeProfile to the same values, and that the writable activeProfile
(data/active-profile.json, D4) is shared bidirectionally. The pwsh half is skipped where pwsh is absent."""
import json
import os
import shutil
import subprocess
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


@unittest.skipUnless(shutil.which("pwsh"), "pwsh not available — Python/PowerShell parity skipped")
class TestModelsParityWithPowerShell(unittest.TestCase):
    def _pwsh(self, script: str, env: dict = None) -> str:
        full = dict(os.environ)
        if env:
            full.update(env)
        r = subprocess.run(["pwsh", "-NoProfile", "-Command", script],
                           capture_output=True, text=True, cwd=str(REPO), timeout=90, env=full)
        self.assertEqual(r.returncode, 0, f"pwsh failed:\n{r.stdout}\n{r.stderr}")
        return r.stdout.strip()

    def test_registry_reads_to_same_values(self):
        # PowerShell resolves the same activeProfile, profile set, and role data Python does.
        out = self._pwsh(". ./scripts/_models.ps1; $c = Get-ModelsConfig; "
                         "@{ active = $c.activeProfile; "
                         "profiles = @($c.profiles.Keys | Sort-Object); "
                         "plannerGguf = $c.profiles['16gb'].planner.gguf; "
                         "plannerCtx = $c.profiles['16gb'].planner.ctx; "
                         "plannerFlags = $c.profiles['16gb'].planner.flags } | ConvertTo-Json -Compress")
        ps = json.loads(out)
        c = bob_models.load_models_config()
        self.assertEqual(ps["active"], c["activeProfile"])
        self.assertEqual(sorted(ps["profiles"]), sorted(c["profiles"]))
        planner = c["profiles"]["16gb"]["planner"]
        self.assertEqual(ps["plannerGguf"], planner["gguf"])
        self.assertEqual(ps["plannerCtx"], planner["ctx"])
        self.assertEqual(list(ps["plannerFlags"]), planner["flags"])

    def test_resolve_profile_name_parity(self):
        script = (". ./scripts/_models.ps1; $c = Get-ModelsConfig; "
                  "'arg|' + (Resolve-ProfileName -Profile '24gb' -Config $c); "
                  "'default|' + (Resolve-ProfileName -Config $c)")
        ps = dict(line.split("|", 1) for line in self._pwsh(script).splitlines())
        c = bob_models.load_models_config()
        self.assertEqual(ps["arg"], bob_models.resolve_profile_name("24gb", c))
        self.assertEqual(ps["default"], bob_models.resolve_profile_name(config=c))

    def test_writable_activeprofile_shared_bidirectionally(self):
        # The data/active-profile.json split (D4) is the same file both languages write and read.
        d = Path(tempfile.mkdtemp(prefix="bob-datadir-"))
        (d / ".migrated").write_text("")  # skip osenv's one-time data/ migration copy
        env = {"BOB_DATA_DIR": str(d)}
        try:
            # pwsh writes -> Python reads
            self._pwsh(". ./scripts/_models.ps1; Set-ActiveProfile -Name '24gb'", env=env)
            with mock.patch.dict(os.environ, env):
                self.assertEqual(bob_models.load_models_config()["activeProfile"], "24gb")
                # Python writes -> pwsh reads
                bob_models.set_active_profile("8gb")
            active = self._pwsh(". ./scripts/_models.ps1; (Get-ModelsConfig).activeProfile", env=env)
            self.assertEqual(active, "8gb")
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
