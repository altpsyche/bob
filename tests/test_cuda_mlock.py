"""CUDA toolkit discovery + mlock privilege (osenv build-time seams). Hermetic: nvidia-smi,
secedit/ulimit, and CUDA disk layout are mocked or pointed at temp trees. The Windows mlock
grant/status paths are `# pragma: no cover` (secedit + UAC), verified only on Windows; here we
cover the Linux paths, the pure CUDA descriptor, and the ranking."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _common  # noqa: F401 — puts scripts/ on sys.path
import osenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "tools"))
import provision as prov  # noqa: E402


class TestMlockToolSurface(unittest.TestCase):
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


class TestCudaMissingMessage(unittest.TestCase):
    def test_atomic_host_recommends_distrobox(self):
        with mock.patch("osenv.is_atomic_linux", return_value=True):
            msg = osenv.cuda_missing_message()
        self.assertIn("distrobox", msg)
        self.assertIn("atomic", msg.lower())

    def test_mutable_host_points_at_install_prereqs(self):
        with mock.patch("osenv.is_atomic_linux", return_value=False):
            msg = osenv.cuda_missing_message()
        self.assertIn("install_prereqs", msg)
        self.assertNotIn("distrobox", msg)


class TestEnsureCudaToolkit(unittest.TestCase):
    """The Tier-0 CUDA-toolkit seam shared by install_prereqs and `bob update`."""

    def _mod(self):
        from bob import install_prereqs
        return install_prereqs

    def test_cpu_build_needs_nothing(self):
        self.assertIsNone(self._mod().ensure_cuda_toolkit(cpu=True))

    def test_returns_present_toolkit(self):
        with mock.patch("osenv.gpu_arch", return_value={"CudaArch": 120}), \
             mock.patch("osenv.best_cuda_root", return_value="/usr/local/cuda-12.8"):
            self.assertEqual(self._mod().ensure_cuda_toolkit(cpu=False), "/usr/local/cuda-12.8")

    def test_atomic_host_returns_none(self):
        # atomic host can't install/build a toolkit -> None (callers fall back to a CPU build, like setup).
        with mock.patch("osenv.gpu_arch", return_value={"CudaArch": 120}), \
             mock.patch("osenv.best_cuda_root", return_value=None), \
             mock.patch("osenv.os_name", return_value="linux"), \
             mock.patch("osenv.is_atomic_linux", return_value=True):
            self.assertIsNone(self._mod().ensure_cuda_toolkit(cpu=False))

    def test_mutable_installs_then_resolves(self):
        seq = [None, "/usr/local/cuda-12.9"]   # absent, then present after the package install
        with mock.patch("osenv.gpu_arch", return_value={"CudaArch": 120}), \
             mock.patch("osenv.best_cuda_root", side_effect=lambda a: seq.pop(0)), \
             mock.patch("osenv.os_name", return_value="linux"), \
             mock.patch("osenv.is_atomic_linux", return_value=False), \
             mock.patch("osenv.linux_package_manager", return_value="dnf"), \
             mock.patch("osenv.resolve_package_name", return_value="cuda-toolkit"), \
             mock.patch("osenv.install_package") as ip:
            root = self._mod().ensure_cuda_toolkit(cpu=False)
        ip.assert_called_once()
        self.assertEqual(root, "/usr/local/cuda-12.9")


if __name__ == "__main__":
    unittest.main()
