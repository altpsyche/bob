"""ONE-C Slice 4 — model-registry capabilities (scripts/tools/models.py) built on the neutral
config/models.json (C0c). Hermetic: the endpoint query, HF HEAD checks, nvidia-smi, profile write, and
config regen are all mocked, so nothing hits the network, a GPU, or real state."""
import sys
import unittest
from pathlib import Path
from unittest import mock

import _common  # noqa: F401 — puts scripts/ on sys.path
from bob import cli, registry

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "tools"))
import models as models_mod  # noqa: E402
import bob_models  # noqa: E402

CFG = {"port": 8080}


class TestRegistryWiring(unittest.TestCase):
    VERBS = ["models", "show", "profiles", "profile", "verify-urls", "bench"]

    def test_flipped_to_python_with_handlers(self):
        by_name = registry.by_name()
        for verb in self.VERBS:
            self.assertTrue(by_name[verb].get("handler"), verb)
            self.assertIn(by_name[verb]["handler"], cli._HANDLERS, verb)

    def test_eval_ported_in_one_d(self):
        # eval stayed pwsh through ONE-C; ONE-D Slice D4 ported it to Python (models.py:eval_model,
        # CLI-only — not an agent tool). See test_slice_d4_eval for its behaviour.
        self.assertTrue(registry.by_name()["eval"].get("handler"))

    def test_tools_registered_and_only_profile_mutates(self):
        self.assertEqual(set(models_mod.DISPATCH), {
            "models_list", "model_show", "profiles_list", "profile_switch", "verify_urls", "bench"})
        self.assertEqual(models_mod.MUTATING_TOOLS, {"profile_switch"})


class TestModelsList(unittest.TestCase):
    def _resp(self, ids):
        r = mock.Mock()
        r.json.return_value = {"data": [{"id": i} for i in ids]}
        return r

    def test_endpoint_up_marks_loaded(self):
        with mock.patch("requests.get", return_value=self._resp(["planner"])):
            out = models_mod.models_list(CFG)
        self.assertIn("Profile: 16gb", out)
        self.assertRegex(out, r"planner\s+.*loaded")
        self.assertRegex(out, r"coder\s+.*unloaded")

    def test_endpoint_down_shows_unknown_state(self):
        import requests
        with mock.patch("requests.get", side_effect=requests.RequestException("down")):
            out = models_mod.models_list(CFG)
        self.assertIn("(endpoint down)", out)
        self.assertIn("Endpoint not running", out)


class TestModelShow(unittest.TestCase):
    def test_known_role_fields(self):
        out = models_mod.model_show("coder", CFG)
        self.assertIn("Role:     coder", out)
        self.assertIn("qwen-coder-14b-q4_k_m.gguf", out)
        self.assertIn("bartowski/Qwen2.5-Coder-14B-Instruct-GGUF", out)

    def test_unknown_role(self):
        out = models_mod.model_show("bogus", CFG)
        self.assertIn("Unknown role 'bogus'", out)


class TestProfilesList(unittest.TestCase):
    def test_marks_active_and_suggests(self):
        with mock.patch.object(models_mod, "gpu_vram_gb", return_value=16):
            out = models_mod.profiles_list(CFG)
        self.assertRegex(out, r"\*\s*16gb")           # active mark
        self.assertIn("suggested '16gb'", out)
        self.assertIn("on disk", out)


class TestProfileSwitch(unittest.TestCase):
    def test_explicit_name(self):
        with mock.patch.object(bob_models, "set_active_profile") as setp, \
             mock.patch.object(bob_models, "regenerate_configs", return_value=False):
            out = models_mod.profile_switch("24gb", CFG)
        setp.assert_called_once()
        self.assertEqual(setp.call_args[0][0], "24gb")
        self.assertIn("activeProfile -> '24gb'", out)

    def test_auto_uses_suggestion(self):
        with mock.patch.object(models_mod, "gpu_vram_gb", return_value=12), \
             mock.patch.object(models_mod, "suggested_profile", return_value="12gb"), \
             mock.patch.object(bob_models, "set_active_profile") as setp, \
             mock.patch.object(bob_models, "regenerate_configs", return_value=False):
            out = models_mod.profile_switch("auto", CFG)
        self.assertEqual(setp.call_args[0][0], "12gb")
        self.assertIn("Detected 12 GB VRAM", out)

    def test_auto_no_gpu_falls_back_to_cpu(self):
        with mock.patch.object(models_mod, "gpu_vram_gb", return_value=None), \
             mock.patch.object(bob_models, "set_active_profile") as setp, \
             mock.patch.object(bob_models, "regenerate_configs", return_value=False):
            out = models_mod.profile_switch("auto", CFG)
        self.assertEqual(setp.call_args[0][0], "cpu")
        self.assertIn("No GPU detected", out)

    def test_unknown_profile_returns_error(self):
        with mock.patch.object(bob_models, "set_active_profile",
                               side_effect=ValueError("unknown profile 'zzz'. Valid: 16gb")), \
             mock.patch.object(bob_models, "regenerate_configs", return_value=False):
            out = models_mod.profile_switch("zzz", CFG)
        self.assertIn("unknown profile 'zzz'", out)


class TestVerifyUrls(unittest.TestCase):
    def test_status_classification(self):
        codes = iter([200, 404, 403])  # first three roles: OK, MISSING, GATED

        def fake_head(url, **kw):
            r = mock.Mock()
            r.status_code = next(codes, 301)  # remaining roles: REDIRECT
            return r

        with mock.patch("requests.head", side_effect=fake_head):
            out = models_mod.verify_urls("16gb", CFG)
        self.assertIn("OK", out)
        self.assertIn("MISSING", out)
        self.assertIn("GATED", out)
        self.assertIn("MISSING/ERROR", out)  # any_bad summary (404 present)

    def test_network_error_is_error_status(self):
        import requests
        with mock.patch("requests.head", side_effect=requests.RequestException("boom")):
            out = models_mod.verify_urls("16gb", CFG)
        self.assertIn("ERROR", out)


class TestGpuHelpers(unittest.TestCase):
    def test_gpu_vram_gb_parses_nvidia_smi(self):
        proc = mock.Mock(stdout="16384\n")
        with mock.patch("shutil.which", return_value="/usr/bin/nvidia-smi"), \
             mock.patch("subprocess.run", return_value=proc):
            self.assertEqual(models_mod.gpu_vram_gb(), 16)

    def test_gpu_vram_gb_none_without_nvidia_smi(self):
        with mock.patch("shutil.which", return_value=None):
            self.assertIsNone(models_mod.gpu_vram_gb())

    def test_suggested_profile_picks_largest_fit(self):
        cfg = bob_models.load_models_config()
        self.assertEqual(models_mod.suggested_profile(16, cfg), "16gb")
        self.assertEqual(models_mod.suggested_profile(24, cfg), "24gb")
        self.assertEqual(models_mod.suggested_profile(10, cfg), "8gb")   # below 12 -> largest fit 8
        self.assertEqual(models_mod.suggested_profile(4, cfg), "8gb")    # below all -> smallest sized


class TestRegenBridgeSingleSourced(unittest.TestCase):
    def test_stack_regen_delegates_to_bob_models(self):
        # The interim pwsh regen bridge is single-sourced in bob_models (shared by stack + models).
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "tools"))
        import stack
        with mock.patch.object(bob_models, "regenerate_configs", return_value=True) as regen:
            self.assertTrue(stack._regen_configs())
        regen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
