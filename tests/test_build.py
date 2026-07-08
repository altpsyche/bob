"""The native build (osenv build seams + scripts/tools/build.py) and the `update` stack orchestration.
cmake/nvcc/go stay subprocess and are mocked here (a fake _run writes the staged binary), so the arg
construction, the atomic bin/ swap, and the guards are all exercised without a real compiler. Windows
branches are `# pragma: no cover`. build is CLI-only (long) — not an agent tool, not on --run."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _common  # noqa: F401 — puts scripts/ on sys.path
import osenv
from bob import cli

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "tools"))
import build as build_mod  # noqa: E402

CFG = {"litellmPort": 8081}


class TestBuildToolSurface(unittest.TestCase):
    def test_build_not_an_agent_tool(self):
        # build.py is CLI-only (long native builds): declared empty tool surface, no agent tools
        self.assertEqual(build_mod.TOOL_DEFS, [])
        self.assertEqual(build_mod.DISPATCH, {})


class TestCmakeFlags(unittest.TestCase):
    def test_gpu_linux_ninja_no_staging(self):
        f = osenv.resolve_build_cmake_flags(cpu=False, arch=120, os="linux")
        self.assertEqual(f, {"Cuda": True, "Generator": "Ninja", "StageDlls": False})

    def test_gpu_windows_vs_stages_dlls(self):
        f = osenv.resolve_build_cmake_flags(cpu=False, arch=120, os="windows")
        self.assertTrue(f["Cuda"])
        self.assertEqual(f["Generator"], "Visual Studio 17 2022")
        self.assertTrue(f["StageDlls"])

    def test_cpu_disables_cuda_both_os(self):
        for o in ("linux", "windows"):
            f = osenv.resolve_build_cmake_flags(cpu=True, os=o)
            self.assertFalse(f["Cuda"])
            self.assertFalse(f["StageDlls"])


class TestLinuxCmake3(unittest.TestCase):
    def test_system_cmake_3x_is_used(self):
        with mock.patch("osenv.shutil.which", return_value="/usr/bin/cmake"), \
             mock.patch("osenv.subprocess.run", return_value=mock.Mock(stdout="cmake version 3.31.7\n")):
            self.assertEqual(osenv.linux_cmake3("/repo"), "/usr/bin/cmake")


class _BuildTreeMixin:
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.repo, True)
        self.src = self.repo / "external" / "llama.cpp"
        self.src.mkdir(parents=True)
        (self.src / "CMakeLists.txt").write_text("# fake")
        self.bin = self.repo / "bin"
        self.patchers = [
            mock.patch.object(build_mod, "REPO", self.repo),
            mock.patch.object(build_mod, "SRC_LLAMA", self.src),
            mock.patch.object(build_mod, "BIN", self.bin),
        ]
        for p in self.patchers:
            p.start()
            self.addCleanup(p.stop)

    def _fake_run(self, captured):
        """A _run stand-in: records argv; when the cmake --build runs, create the staged binary so the
        real atomic-swap code path executes."""
        def run(argv, **kw):
            captured.append([str(a) for a in argv])
            if "--build" in argv:
                out = self.src / "build" / "bin"
                out.mkdir(parents=True, exist_ok=True)
                (out / "llama-server").write_text("ELF")
        return run


class TestBuildLlama(_BuildTreeMixin, unittest.TestCase):
    def test_already_built_short_circuits(self):
        self.bin.mkdir()
        (self.bin / osenv.exe_name("llama-server")).write_text("x")   # .exe on Windows
        out = build_mod.build_llama(cpu=True)
        self.assertIn("already built", out)

    def test_missing_submodule_raises(self):
        (self.src / "CMakeLists.txt").unlink()
        with self.assertRaises(RuntimeError):
            build_mod.build_llama(cpu=True)

    @unittest.skipIf(sys.platform == "win32",
                     "Linux CUDA build: cmake paths go through pathlib, which yields host separators "
                     "on a Windows runner (Windows uses the VS-generator path instead)")
    def test_linux_cuda_configure_args_and_swap(self):
        cap = []
        with mock.patch("osenv.os_name", return_value="linux"), \
             mock.patch("osenv.gpu_arch", return_value={"CudaArch": 120, "Gen": "Blackwell", "MinCudaMajor": 12}), \
             mock.patch("osenv.best_cuda_root", return_value="/opt/cuda"), \
             mock.patch("osenv.cuda_host_compiler", return_value="/usr/bin/g++-14"), \
             mock.patch("osenv.assert_cuda_host_compiler_ok"), \
             mock.patch.object(build_mod, "_resolve_cmake", return_value="cmake"), \
             mock.patch.object(build_mod, "_run", side_effect=self._fake_run(cap)):
            out = build_mod.build_llama(cpu=False, force=True)
        configure = next(c for c in cap if "-DGGML_CUDA=ON" in c)
        self.assertIn("-DCMAKE_CUDA_ARCHITECTURES=120", configure)
        self.assertIn("-DCMAKE_CUDA_COMPILER=/opt/cuda/bin/nvcc", configure)
        self.assertIn("-DCMAKE_CUDA_HOST_COMPILER=/usr/bin/g++-14", configure)
        self.assertIn("-G", configure)
        self.assertIn("Ninja", configure)
        self.assertTrue((self.bin / "llama-server").exists())  # atomic swap landed the binary
        self.assertIn("Built", out)

    def test_cpu_build_disables_cuda(self):
        cap = []
        with mock.patch("osenv.os_name", return_value="linux"), \
             mock.patch.object(build_mod, "_resolve_cmake", return_value="cmake"), \
             mock.patch.object(build_mod, "_run", side_effect=self._fake_run(cap)):
            build_mod.build_llama(cpu=True, force=True)
        configure = next(c for c in cap if any("GGML_CUDA" in a for a in c))
        self.assertIn("-DGGML_CUDA=OFF", configure)
        self.assertFalse(any("CUDA_ARCHITECTURES" in a for a in configure))

    def test_cuda_missing_root_raises(self):
        with mock.patch("osenv.os_name", return_value="linux"), \
             mock.patch("osenv.gpu_arch", return_value={"CudaArch": 120, "Gen": "Blackwell", "MinCudaMajor": 12}), \
             mock.patch("osenv.best_cuda_root", return_value=None):
            with self.assertRaises(RuntimeError) as cm:
                build_mod.build_llama(cpu=False, force=True)
            self.assertIn("12.8", str(cm.exception))


class TestBuildLlamaSwap(_BuildTreeMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        (self.repo / "external" / "llama-swap").mkdir(parents=True)
        mock.patch.object(build_mod, "SRC_SWAP", self.repo / "external" / "llama-swap").start()
        self.addCleanup(mock.patch.stopall)

    def test_missing_go_raises(self):
        (self.bin).mkdir(exist_ok=True)
        with mock.patch("build.shutil.which", return_value=None):
            with self.assertRaises(RuntimeError):
                build_mod.build_llama_swap(force=True)

    def test_happy_path_runs_go_build(self):
        cap = []
        with mock.patch("build.shutil.which", return_value="/usr/bin/go"), \
             mock.patch.object(build_mod, "_run", side_effect=lambda a, **k: cap.append([str(x) for x in a])):
            out = build_mod.build_llama_swap(force=True)
        self.assertTrue(any("go" in c[0] and "build" in c for c in cap))
        self.assertIn("Built", out)


class TestSetupFabric(_BuildTreeMixin, unittest.TestCase):
    def test_writes_env_and_configures(self):
        fabric_dir = self.repo / ".config" / "fabric"
        src_fabric = self.repo / "external" / "fabric"
        (src_fabric / "cmd" / "fabric").mkdir(parents=True)
        (src_fabric / "go.mod").write_text("module fabric")
        (src_fabric / "data" / "patterns").mkdir(parents=True)
        build_mod.configure(CFG)
        with mock.patch.object(build_mod, "SRC_FABRIC", src_fabric), \
             mock.patch("build.shutil.which", return_value="/usr/bin/go"), \
             mock.patch("osenv.home_config_dir", return_value=fabric_dir), \
             mock.patch("osenv.bin_exe", return_value=self.bin / "fabric"), \
             mock.patch.object(build_mod, "_run", side_effect=lambda a, **k: (self.bin.mkdir(exist_ok=True), (self.bin / "fabric").write_text("x"))):
            out = build_mod.setup_fabric(force=True)
        env = (fabric_dir / ".env").read_text()
        self.assertIn("OPENAI_API_BASE_URL=http://localhost:8081/v1", env)
        self.assertIn("DEFAULT_MODEL=coder", env)
        self.assertIn("Configured: coder", out)


class TestUpdateStack(unittest.TestCase):
    # update over git + build + lock + doctor with a bin/ rollback; every piece is mocked so no
    # git/network/compiler runs. CLI-only.
    def _run(self, before, after, verify=True, tag=None):
        """Run update_stack with everything mocked; return (rc, mocks-by-name, git-calls)."""
        import contextlib
        import health
        import tempfile
        git = []
        srv = Path(tempfile.mkdtemp()) / "llama-server"
        srv.write_text("ELF")  # so srv.exists() is True after a rebuild
        self.addCleanup(__import__("shutil").rmtree, srv.parent, True)
        specs = {
            "_git_head": mock.patch.object(build_mod, "_git_head", side_effect=[before, after]),
            "_run": mock.patch.object(build_mod, "_run", side_effect=lambda a, **k: git.append([str(x) for x in a])),
            "_reinstall_venv": mock.patch.object(build_mod, "_reinstall_venv"),
            "build_llama": mock.patch.object(build_mod, "build_llama", return_value="built"),
            "_verify_binary": mock.patch.object(build_mod, "_verify_binary", return_value=verify),
            "backup": mock.patch("osenv.backup_build_output", return_value=Path("/bin.bak")),
            "restore": mock.patch("osenv.restore_build_output", return_value=True),
            "remove_bak": mock.patch("osenv.remove_build_output_backup"),
            "bin_exe": mock.patch("osenv.bin_exe", return_value=srv),
            "gpu_info": mock.patch("osenv.gpu_info", return_value=None),
            "write_lock": mock.patch("bob.versions.write_lock"),
            "h_configure": mock.patch.object(health, "configure"),
            "health_check": mock.patch.object(health, "health_check", return_value="doctor-ok"),
        }
        build_mod.configure(CFG)
        with contextlib.ExitStack() as es:
            mocks = {k: es.enter_context(v) for k, v in specs.items()}
            rc = build_mod.update_stack(tag=tag)
        return rc, mocks, git

    def test_unchanged_skips_rebuild_but_relocks(self):
        rc, mocks, git = self._run("abc", "abc")
        self.assertEqual(rc, 0)
        mocks["build_llama"].assert_not_called()   # commit unchanged -> no rebuild
        mocks["write_lock"].assert_called_once()   # relock still happens
        mocks["health_check"].assert_called_once()
        self.assertTrue(any("pull" in c for c in git))

    def test_changed_rebuilds_and_discards_backup(self):
        rc, mocks, _ = self._run("aaa", "bbb", verify=True)
        self.assertEqual(rc, 0)
        mocks["build_llama"].assert_called_once()
        mocks["backup"].assert_called_once()
        mocks["remove_bak"].assert_called_once()   # backup discarded on verified success
        mocks["restore"].assert_not_called()

    def test_changed_verify_fails_rolls_back(self):
        rc, mocks, _ = self._run("aaa", "bbb", verify=False)
        self.assertEqual(rc, 1)                      # handled failure
        mocks["restore"].assert_called_once()        # rolled bin/ back
        mocks["remove_bak"].assert_not_called()

    def test_tag_triggers_checkout(self):
        rc, _, git = self._run("x", "x", tag="v0.2.0")
        self.assertEqual(rc, 0)
        self.assertTrue(any("checkout" in c and "v0.2.0" in c for c in git))


class TestCliArgParsing(unittest.TestCase):
    def test_tag_flag_parsed(self):
        seen = {}
        fake = mock.Mock()
        fake.update_stack = mock.Mock(side_effect=lambda tag=None: seen.update(tag=tag) or 0)
        with mock.patch.object(cli, "_build_mod", return_value=fake):
            cli._handle_update(["--tag", "v1.2.3"])
        self.assertEqual(seen["tag"], "v1.2.3")


if __name__ == "__main__":
    unittest.main()
