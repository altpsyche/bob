"""ONE-D Slice D3 — the CUDA-resolution cluster + mlock seams (osenv) and the healed deep `diagnose`
(scripts/tools/health.py). Hermetic: nvidia-smi, secedit/ulimit, CUDA disk layout, and the registry are
mocked or pointed at temp trees. The Windows mlock grant/status paths are `# pragma: no cover` (secedit +
UAC), verified only on Windows; here we cover the Linux paths, the pure CUDA descriptor, and the ranking."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _common  # noqa: F401 — puts scripts/ on sys.path
import osenv
from bob import cli, registry

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "tools"))
import health as health_mod  # noqa: E402
import provision as prov  # noqa: E402

CFG = {"port": 8080}


class TestRegistryAndToolWiring(unittest.TestCase):
    def test_mlock_flipped_to_python(self):
        entry = registry.by_name()["mlock"]
        self.assertEqual(entry["runtime"], "python")
        self.assertEqual(entry["handler"], "mlock")
        self.assertIn("mlock", cli._HANDLERS)

    def test_mlock_status_tool_read_only(self):
        self.assertIn("mlock_status", prov.DISPATCH)
        self.assertNotIn("mlock_status", prov.MUTATING_TOOLS)


class TestCudaCandidates(unittest.TestCase):
    def test_windows_blackwell_pins_v128(self):
        c = osenv.resolve_cuda_root_candidates(120, os="windows")
        self.assertEqual(c["Pin"], "v12.8")
        self.assertEqual(c["DirPrefix"], "v")
        self.assertEqual(c["MinVer"], "12.8")
        self.assertEqual(c["Fixed"], [])

    def test_windows_older_arch_no_pin(self):
        c = osenv.resolve_cuda_root_candidates(89, os="windows")
        self.assertIsNone(c["Pin"])
        self.assertEqual(c["MinVer"], "11.0")

    def test_linux_has_canonical_fixed_roots_and_pin(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CUDA_HOME", None)
            os.environ.pop("CUDA_PATH", None)
            c = osenv.resolve_cuda_root_candidates(120, os="linux")
        self.assertEqual(c["DirPrefix"], "cuda-")
        self.assertIn("/usr/local/cuda", c["Fixed"])
        self.assertIn("/opt/cuda", c["Fixed"])
        self.assertEqual(c["Pin"], "/usr/local/cuda-12.8")

    def test_linux_appends_env_roots(self):
        with mock.patch.dict(os.environ, {"CUDA_HOME": "/custom/cuda"}):
            c = osenv.resolve_cuda_root_candidates(0, os="linux")
        self.assertIn("/custom/cuda", c["Fixed"])


class TestCudaToolkitVersion(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.root, True)

    def test_version_json(self):
        (self.root / "version.json").write_text('{"cuda": {"version": "12.8.1"}}')
        self.assertEqual(osenv.cuda_toolkit_version(self.root), (12, 8))

    def test_version_txt_fallback(self):
        (self.root / "version.txt").write_text("CUDA Version 11.4.2")
        self.assertEqual(osenv.cuda_toolkit_version(self.root), (11, 4))

    def test_none_when_absent(self):
        self.assertIsNone(osenv.cuda_toolkit_version(self.root))
        self.assertIsNone(osenv.cuda_toolkit_version(None))


class TestBestCudaRoot(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.base, True)

    def _candidates(self, min_ver, prefix="cuda-"):
        return {"Base": str(self.base), "DirPrefix": prefix, "Fixed": [], "Pin": None, "MinVer": min_ver}

    def test_picks_newest_meeting_floor(self):
        for name in ("cuda-11.8", "cuda-12.8", "cuda-13.3", "not-cuda"):
            (self.base / name).mkdir()
        with mock.patch("osenv.resolve_cuda_root_candidates", return_value=self._candidates("12.8")):
            # 13.3 is newest and >= 12.8 floor (the pin is a FLOOR, 13.x qualifies for sm_120)
            self.assertEqual(Path(osenv.best_cuda_root(120)).name, "cuda-13.3")

    def test_none_when_nothing_meets_floor(self):
        (self.base / "cuda-11.8").mkdir()
        with mock.patch("osenv.resolve_cuda_root_candidates", return_value=self._candidates("12.8")):
            self.assertIsNone(osenv.best_cuda_root(120))

    def test_fixed_root_version_from_disk(self):
        fixed = self.base / "opt-cuda"
        fixed.mkdir()
        (fixed / "version.json").write_text('{"cuda": {"version": "13.0"}}')
        cands = {"Base": str(self.base / "none"), "DirPrefix": "cuda-", "Fixed": [str(fixed)],
                 "Pin": None, "MinVer": "12.8"}
        with mock.patch("osenv.resolve_cuda_root_candidates", return_value=cands):
            self.assertEqual(osenv.best_cuda_root(120), str(fixed.resolve()))


class TestCudaHostCompiler(unittest.TestCase):
    def test_windows_is_none(self):
        with mock.patch("osenv.os_name", return_value="windows"):
            self.assertIsNone(osenv.cuda_host_compiler())

    def test_honors_nvcc_ccbin(self):
        with mock.patch("osenv.os_name", return_value="linux"), \
             mock.patch.dict(os.environ, {"NVCC_CCBIN": "g++-14"}), \
             mock.patch("osenv.shutil.which", side_effect=lambda c: "/usr/bin/g++-14" if c == "g++-14" else None):
            self.assertEqual(osenv.cuda_host_compiler(), "/usr/bin/g++-14")


class TestMlockLinux(unittest.TestCase):
    def test_status_unlimited_granted(self):
        with mock.patch("osenv.os_name", return_value="linux"), \
             mock.patch("osenv.subprocess.run", return_value=mock.Mock(stdout="unlimited\n")):
            st = osenv.mlock_status()
            self.assertTrue(st["granted"])

    def test_status_limited_not_granted(self):
        with mock.patch("osenv.os_name", return_value="linux"), \
             mock.patch("osenv.subprocess.run", return_value=mock.Mock(stdout="8192\n")):
            st = osenv.mlock_status()
            self.assertFalse(st["granted"])
            self.assertIn("8192", st["detail"])

    def test_grant_linux_prints_guidance(self):
        with mock.patch("osenv.os_name", return_value="linux"):
            out = osenv.mlock_grant()
            self.assertIn("ulimit -l unlimited", out)
            self.assertIn("limits.conf", out)


class TestDiagnoseDeep(unittest.TestCase):
    """The healed diagnose: RAM / Package / CUDA / mlock / NUMA rows now render (Slice-3 split gone)."""

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
        self.assertNotIn("ports to Python in ONE-D", out)  # split note gone

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
