"""Health / diagnostics capabilities (scripts/tools/health.py). Hermetic: nvidia-smi,
port probes, HTTP, subprocess (binary --version / git / package import), and the tool-registry build are
all mocked, so nothing hits the network, a GPU, real ports, or the real toolchain.

The scheduler + reproducibility health_check rows read their real sources — the BobAgent task row reads
osenv.agent_task_status(), the versions.lock row reads bob.versions — asserted in
TestHealthCheckWiredRows (not-registered / missing-lock stay informational, never a failure)."""
import sys
import unittest
from pathlib import Path
from unittest import mock

import _common  # noqa: F401 — puts scripts/ on sys.path

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


class TestHealthToolSurface(unittest.TestCase):
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


class TestDiagnoseLightRows(unittest.TestCase):
    """diagnose's light rows (GPU/VRAM/Profile/Endpoint). The deep rows (CUDA/RAM/NUMA/mlock/Package) are
    covered hermetically in test_health.TestDiagnoseDeep; here we mock the deep osenv seams so
    these stay hermetic (no real nvidia-smi / secedit / /proc reads)."""

    def _run(self, gpu, vram):
        import contextlib
        import models as models_mod
        patchers = [
            mock.patch.object(health_mod, "gpu_arch", return_value=gpu),
            mock.patch.object(models_mod, "gpu_vram_gb", return_value=vram),
            mock.patch.multiple("osenv",
                                is_port_in_use=mock.Mock(return_value=False),
                                os_name=mock.Mock(return_value="linux"),
                                system_ram_gb=mock.Mock(return_value={"TotalGB": 62, "FreeGB": 52}),
                                linux_package_manager=mock.Mock(return_value="apt"),
                                linux_os_family=mock.Mock(return_value="debian"),
                                best_cuda_root=mock.Mock(return_value="/usr/local/cuda-12.8"),
                                mlock_status=mock.Mock(return_value={"granted": False, "detail": "d"}),
                                numa_node_count=mock.Mock(return_value=1)),
        ]
        with contextlib.ExitStack() as es:
            for p in patchers:
                es.enter_context(p)
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

    def test_deep_rows_present(self):
        # diagnose renders the deep OS-discovery rows alongside the light rows.
        out = self._run({"CudaArch": 89, "Gen": "Ada Lovelace", "MinCudaMajor": 11}, 24)
        for label in ("RAM", "CUDA", "NUMA", "mlock", "Package"):
            self.assertIn(f"  {label:<10}", out, label)


class TestHealthCheckWiredRows(unittest.TestCase):
    """The scheduler + reproducibility rows are wired to the real readers
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

    def test_doctor_reports_docker_absent_for_compose_services(self):
        # docker missing: the compose services (SearXNG/n8n) are reported unavailable with the
        # real reason + install hint, not a misleading "bob services start" that would just fail.
        out = self._docker(present=False)
        self.assertIn("not installed", out)
        self.assertIn("SearXNG (:8888)", out)
        self.assertIn("unavailable (needs Docker, not installed)", out)
        self.assertNotIn("SearXNG reachable", out)   # the reachability check is skipped when no docker

    def test_doctor_checks_reachability_when_docker_present(self):
        out = self._docker(present=True)
        self.assertIn("SearXNG reachable (:8888)", out)   # normal reachability check restored
        self.assertNotIn("needs Docker", out)

    def _docker(self, present):
        import requests
        with mock.patch.object(health_mod, "_has_module", return_value=True), \
             mock.patch.object(health_mod, "_tool_load_errors", return_value=[]), \
             mock.patch("osenv.docker_present", return_value=present), \
             mock.patch("osenv.is_port_in_use", return_value=False), \
             mock.patch("osenv.agent_task_status", return_value={"registered": False}), \
             mock.patch("requests.get", side_effect=requests.RequestException("down")), \
             mock.patch("bob_models.profile_roles", return_value={"agent": {"gguf": "x.gguf"}}), \
             mock.patch("bob.versions.load_lock", return_value={"release": "1", "submodules": {}, "models": {}}), \
             mock.patch("bob.versions.check_reproducibility", return_value=[]):
            return health_mod.health_check(CFG, doctor=True)


class TestDiagnoseDeep(unittest.TestCase):
    """The deep diagnose rows: RAM / Package / CUDA / mlock / NUMA render alongside the light rows."""

    def _diag(self, pkg_mgr="pacman", best_cuda="/opt/cuda", mlock_granted=False,
              numa=1, numa_cfg="", mlock_big=False):
        import contextlib
        import models as models_mod
        mcfg = {"activeProfile": "16gb", "defaults": {"mlockBig": mlock_big, "numa": numa_cfg},
                "profiles": {"16gb": {"_targetVRAM": "16GB", "coder": {"gguf": "c.gguf", "sizeGB": 8}}}}
        gpu = {"CudaArch": 120, "Gen": "Blackwell", "MinCudaMajor": 12}
        patchers = [
            mock.patch.multiple(
                "osenv",
                system_ram_gb=mock.Mock(return_value={"TotalGB": 62, "FreeGB": 52}),
                os_name=mock.Mock(return_value="linux"),
                linux_package_manager=mock.Mock(return_value=pkg_mgr),
                linux_os_family=mock.Mock(return_value="arch"),
                best_cuda_root=mock.Mock(return_value=best_cuda),
                mlock_status=mock.Mock(return_value={"granted": mlock_granted, "detail": "detail"}),
                numa_node_count=mock.Mock(return_value=numa),
                is_port_in_use=mock.Mock(return_value=False),
                resolve_cuda_root_candidates=mock.Mock(
                    return_value={"Base": "/nonexistent", "DirPrefix": "cuda-", "Fixed": []})),
            mock.patch.object(health_mod, "gpu_arch", return_value=gpu),
            mock.patch.object(models_mod, "gpu_vram_gb", return_value=16),
            mock.patch("bob_models.load_models_config", return_value=mcfg),
            mock.patch("bob_models.resolve_profile_name", return_value="16gb"),
            mock.patch("bob_models.profile_roles", return_value=mcfg["profiles"]["16gb"]),
        ]
        with contextlib.ExitStack() as es:
            for p in patchers:
                es.enter_context(p)
            return health_mod.diagnose(CFG)

    def test_deep_rows_present(self):
        out = self._diag()
        for label in ("RAM", "Package", "CUDA", "mlock", "NUMA"):
            self.assertIn(f"  {label:<10}", out, label)
        self.assertIn("62 GB total  (52 GB free)", out)
        self.assertIn("pacman  (family: arch)", out)
        self.assertIn("cuda  ok", out)  # Path('/opt/cuda').name

    def test_missing_package_manager_is_an_issue(self):
        out = self._diag(pkg_mgr=None)
        self.assertIn("no supported manager", out)
        self.assertIn("issue(s) noted", out)

    def test_cuda_missing_for_gpu_is_an_issue(self):
        out = self._diag(best_cuda=None)
        self.assertIn("needs 12.8 (required for Blackwell)", out)

    def test_numa_config_mismatch_is_an_issue(self):
        out = self._diag(numa=1, numa_cfg="isolate")
        self.assertIn("flag is a no-op", out)


if __name__ == "__main__":
    unittest.main()
