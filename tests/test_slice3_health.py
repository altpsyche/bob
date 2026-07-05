"""ONE-C Slice 3 — health / diagnostics capabilities (scripts/tools/health.py). Hermetic: nvidia-smi,
port probes, HTTP, subprocess (binary --version / git / package import), and the tool-registry build are
all mocked, so nothing hits the network, a GPU, real ports, or the real toolchain.

Slice-3 scope: diagnose is the SPLIT port (light discovery only, deep OS discovery stays pwsh -> ONE-D).
ONE-D Slice D0 wired the two formerly-degraded health_check rows to their real readers — the BobAgent
task row reads osenv.agent_task_status(), the versions.lock row reads bob.versions — asserted in
TestHealthCheckWiredRows (not-registered / missing-lock stay informational, never a failure)."""
import sys
import unittest
from pathlib import Path
from unittest import mock

import _common  # noqa: F401 — puts scripts/ on sys.path
from bob import cli, registry

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "tools"))
import health as health_mod  # noqa: E402

CFG = {"port": 8080}


def _smi(stdout):
    """A mock subprocess.run result carrying nvidia-smi stdout."""
    r = mock.Mock()
    r.stdout = stdout
    r.stderr = ""
    r.returncode = 0
    return r


class TestRegistryWiring(unittest.TestCase):
    VERBS = ["setup", "doctor", "version", "diagnose"]

    def test_flipped_to_python_with_handlers(self):
        by_name = registry.by_name()
        for verb in self.VERBS:
            self.assertEqual(by_name[verb]["runtime"], "python", verb)
            self.assertIn(by_name[verb]["handler"], cli._HANDLERS, verb)

    def test_tools_registered_none_mutating(self):
        self.assertEqual(set(health_mod.DISPATCH), {"doctor", "diagnose", "version_info"})
        # All health verbs are read-only — none should declare mutation.
        self.assertFalse(getattr(health_mod, "MUTATING_TOOLS", set()))


class TestGpuArch(unittest.TestCase):
    def test_ada_lovelace(self):
        with mock.patch("shutil.which", return_value="/usr/bin/nvidia-smi"), \
             mock.patch("subprocess.run", return_value=_smi("8.9\n")):
            g = health_mod.gpu_arch()
        self.assertEqual(g["CudaArch"], 89)
        self.assertEqual(g["Gen"], "Ada Lovelace")
        self.assertEqual(g["MinCudaMajor"], 11)

    def test_blackwell(self):
        with mock.patch("shutil.which", return_value="/usr/bin/nvidia-smi"), \
             mock.patch("subprocess.run", return_value=_smi("12.0\n")):
            g = health_mod.gpu_arch()
        self.assertEqual(g["CudaArch"], 120)
        self.assertEqual(g["Gen"], "Blackwell")
        self.assertEqual(g["MinCudaMajor"], 12)

    def test_no_nvidia_smi(self):
        with mock.patch("shutil.which", return_value=None):
            self.assertIsNone(health_mod.gpu_arch())

    def test_unparseable(self):
        with mock.patch("shutil.which", return_value="/usr/bin/nvidia-smi"), \
             mock.patch("subprocess.run", return_value=_smi("N/A\n")):
            self.assertIsNone(health_mod.gpu_arch())


class TestVersionInfo(unittest.TestCase):
    def test_reports_release_and_binaries(self):
        # git commit + binary --version mocked; VERSION/versions.lock read from the real repo files.
        def fake_run(argv, **kw):
            if argv[0] == "git":
                return _smi("deadbee\n")
            return _smi("version: 42 (abc)\n")  # binary --version
        with mock.patch("osenv.bin_exe", side_effect=lambda b: Path("/does/not/exist") / b), \
             mock.patch("subprocess.run", side_effect=fake_run):
            out = health_mod.version_info(CFG)
        self.assertRegex(out, r"^Bob \d")
        # binaries not on disk -> "(not built)", commit still queried
        self.assertIn("llama-swap:", out)
        self.assertIn("llama-server:", out)
        self.assertIn("(not built)", out)


class TestDiagnoseSplit(unittest.TestCase):
    """diagnose ports the light half only; the deep OS-discovery rows must be ABSENT (they stay pwsh)."""

    def _run(self, gpu, vram):
        import models as models_mod
        with mock.patch.object(health_mod, "gpu_arch", return_value=gpu), \
             mock.patch.object(models_mod, "gpu_vram_gb", return_value=vram), \
             mock.patch("osenv.is_port_in_use", return_value=False):
            return health_mod.diagnose(CFG)

    def test_gpu_and_profile_rows(self):
        out = self._run({"CudaArch": 120, "Gen": "Blackwell", "MinCudaMajor": 12}, 16)
        self.assertIn("Blackwell  (sm_120)", out)
        self.assertIn("VRAM        16 GB", out)
        self.assertIn("Profile", out)
        self.assertIn("Endpoint", out)
        self.assertIn("not running", out)

    def test_no_gpu(self):
        out = self._run(None, None)
        self.assertIn("not detected", out)
        self.assertIn("VRAM        unknown", out)

    def test_deep_discovery_rows_deferred(self):
        out = self._run({"CudaArch": 89, "Gen": "Ada Lovelace", "MinCudaMajor": 11}, 24)
        # The deep build-time rows scripts/diagnose.ps1 shows must NOT be produced by the Python port.
        # Rows are "  <label:<10>  value"; check the rows section only (the deferral note NAMES these
        # subsystems by design, so scan above it).
        rows = out.split("Deep machine-readiness")[0]
        for label in ("CUDA", "NUMA", "mlock", "Package", "RAM"):
            self.assertNotIn(f"  {label:<10}  ", rows,
                             f"deep-discovery row '{label}' leaked into the split port")
        # ...and the honest deferral note must be present.
        self.assertIn("Deep machine-readiness", out)
        self.assertIn("ONE-D", out)


class TestHealthCheckWiredRows(unittest.TestCase):
    """ONE-D Slice D0: the scheduler + reproducibility rows are wired to the real readers
    (osenv.agent_task_status, bob.versions). Not-registered / missing-lock are informational
    ('○'), never a failure ('✗')."""

    def _run(self, doctor, task_status=None, lock=None, drift=None, lock_error=None):
        import requests
        task_status = task_status or {"registered": False, "state": None, "next_run": None}
        lock = lock if lock is not None else {"release": "1.2.3",
                                              "submodules": {"a": "x", "b": "y"},
                                              "models": {"m.gguf": {}}}

        def _load_lock(*a, **k):
            if lock_error:
                raise lock_error
            return lock

        with mock.patch.object(health_mod, "_has_module", return_value=True), \
             mock.patch.object(health_mod, "_tool_load_errors", return_value=[]), \
             mock.patch("osenv.is_port_in_use", return_value=False), \
             mock.patch("osenv.agent_task_status", return_value=task_status), \
             mock.patch("requests.get", side_effect=requests.RequestException("down")), \
             mock.patch("bob_models.profile_roles", return_value={"agent": {"gguf": "x.gguf"}}), \
             mock.patch("bob.versions.load_lock", side_effect=_load_lock), \
             mock.patch("bob.versions.check_reproducibility", return_value=(drift or [])):
            return health_mod.health_check(CFG, doctor=doctor)

    def test_setup_check_scheduler_row_not_registered(self):
        out = self._run(doctor=False)
        self.assertIn("Bob agent setup check", out)
        self.assertIn("○  BobAgent task not registered", out)
        self.assertNotIn("── runtime ──", out)  # setup(check) has no runtime section

    def test_scheduler_row_registered(self):
        out = self._run(doctor=False,
                        task_status={"registered": True, "state": "Ready", "next_run": "2026-07-06 09:00"})
        self.assertIn("✓  BobAgent task registered (Ready, next 2026-07-06 09:00)", out)

    def test_doctor_reproducibility_clean(self):
        out = self._run(doctor=True)
        self.assertIn("── reproducibility ──", out)
        self.assertIn("✓  versions.lock reproducible (release 1.2.3, 2 submodules, 1 models)", out)

    def test_doctor_reproducibility_drift_fails(self):
        drift = [{"kind": "submodule", "name": "external/llama.cpp",
                  "expected": "abcdef123456", "actual": "999999999999"}]
        out = self._run(doctor=True, drift=drift)
        self.assertIn("✗  submodule external/llama.cpp: locked abcdef123456 != actual 999999999999", out)

    def test_doctor_missing_lock_is_pending_not_failure(self):
        out = self._run(doctor=True, lock_error=RuntimeError("versions.lock not found"))
        for line in out.splitlines():
            if "versions.lock reproducibility" in line:
                self.assertIn("○", line)
                self.assertNotIn("✗", line)


if __name__ == "__main__":
    unittest.main()
