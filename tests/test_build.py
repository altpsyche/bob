"""The native build (osenv build seams + scripts/tools/build.py) and the `update` stack orchestration.
cmake/nvcc/go stay subprocess and are mocked here (a fake _run writes the staged binary), so the arg
construction, the atomic bin/ swap, and the guards are all exercised without a real compiler. Windows
branches are `# pragma: no cover`. build is CLI-only (long) — not an agent tool, not on --run."""
import io
import os
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


class TestPruneOrphanModels(unittest.TestCase):
    """update's opt-in reclaim of models/*.gguf a release dropped (e.g. the old coder). Keeps referenced
    GGUFs + their mmproj sidecars; TTY-gated; guarded against pruning while the new set is incomplete."""

    def setUp(self):
        import json
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.repo, True)
        self.models = self.repo / "models"
        self.models.mkdir()
        for name, blob in (("keep.gguf", b"x" * 10), ("old-coder.gguf", b"y" * 20),
                           ("mmproj-x.gguf", b"z" * 5)):
            (self.models / name).write_bytes(blob)
        (self.repo / "versions.lock").write_text(json.dumps(
            {"models": {"keep.gguf": {"repo": "r", "path": "keep.gguf", "mmproj": "mmproj-x.gguf"}}}))
        p = mock.patch.object(build_mod, "REPO", self.repo)
        p.start()
        self.addCleanup(p.stop)

    def _run(self, isatty=True, answer="y", current=None):
        import provision
        current = current if current is not None else [{"gguf": "keep.gguf"}]
        with mock.patch.object(provision, "resolve_fetch_set", return_value=("16gb", current)), \
             mock.patch("sys.stdin") as stdin, \
             mock.patch("builtins.input", return_value=answer):
            stdin.isatty.return_value = isatty
            build_mod._prune_orphan_models()

    def test_prunes_orphan_on_yes(self):
        self._run(isatty=True, answer="y")
        self.assertFalse((self.models / "old-coder.gguf").exists())   # orphan gone
        self.assertTrue((self.models / "keep.gguf").exists())          # referenced kept
        self.assertTrue((self.models / "mmproj-x.gguf").exists())      # referenced mmproj kept

    def test_keeps_on_no(self):
        self._run(isatty=True, answer="n")
        self.assertTrue((self.models / "old-coder.gguf").exists())

    def test_skips_when_non_interactive(self):
        self._run(isatty=False)
        self.assertTrue((self.models / "old-coder.gguf").exists())

    def test_skips_when_current_model_missing(self):
        # guard: don't prune the old coder while the new one hasn't downloaded yet
        self._run(isatty=True, answer="y", current=[{"gguf": "not-downloaded-yet.gguf"}])
        self.assertTrue((self.models / "old-coder.gguf").exists())


class TestCmakeFlags(unittest.TestCase):
    def test_gpu_linux_ninja_no_staging(self):
        f = osenv.resolve_build_cmake_flags(cpu=False, arch=120, os="linux")
        self.assertEqual(f, {"Cuda": True, "Generator": "Ninja", "StageDlls": False})

    def test_gpu_windows_ninja_stages_dlls(self):
        # Windows uses Ninja too (so ggml's ccache launcher applies); build_llama calls ensure_msvc_env to
        # put cl.exe on PATH. CUDA DLLs are still staged next to the .exe.
        f = osenv.resolve_build_cmake_flags(cpu=False, arch=120, os="windows")
        self.assertTrue(f["Cuda"])
        self.assertEqual(f["Generator"], "Ninja")
        self.assertTrue(f["StageDlls"])

    def test_ensure_msvc_env_is_noop_off_windows(self):
        with mock.patch("osenv.os_name", return_value="linux"):
            self.assertTrue(osenv.ensure_msvc_env())   # never blocks a non-Windows build

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

    def test_too_old_cmake_triggers_pinned_build(self):
        # Ubuntu 20.04 ships cmake 3.16, but llama.cpp needs >= 3.18: reject it and use the pinned build.
        repo = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, repo, True)
        machine = __import__("platform").machine() or "x86_64"
        exe = repo / "tools" / f"cmake-3.31.7-linux-{machine}" / "bin" / "cmake"
        exe.parent.mkdir(parents=True)
        exe.write_text("x")   # pre-place so no real download happens
        with mock.patch("osenv.shutil.which", return_value="/usr/bin/cmake"), \
             mock.patch("osenv.subprocess.run", return_value=mock.Mock(stdout="cmake version 3.16.3\n")):
            self.assertEqual(osenv.linux_cmake3(str(repo)), str(exe))   # 3.16 rejected -> pinned


    def test_pinned_download_is_sha_verified(self):
        repo = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, repo, True)
        got = {}

        def fake_download(url, dest, sha256=None, timeout=None, require_sha=False):
            got.update(url=url, dest=Path(dest), sha256=sha256, require_sha=require_sha)
            raise RuntimeError("stop before extracting")

        with mock.patch("osenv.shutil.which", return_value=None), \
             mock.patch("osenv.platform.machine", return_value="x86_64"), \
             mock.patch.object(osenv, "download", side_effect=fake_download):
            with self.assertRaises(RuntimeError):
                osenv.linux_cmake3(str(repo))
        self.assertEqual(got["sha256"], osenv.CMAKE_PIN_SHA256["x86_64"])
        self.assertTrue(got["require_sha"])
        self.assertIn(osenv.CMAKE_PIN, got["url"])
        self.assertNotEqual(got["dest"].parent, Path(tempfile.gettempdir()))   # private temp dir, not /tmp/<name>

    def test_range_is_the_one_source(self):
        self.assertTrue(osenv.cmake_in_range("cmake version 3.18.0"))
        self.assertTrue(osenv.cmake_in_range(f"cmake version {osenv.CMAKE_PIN}"))
        self.assertFalse(osenv.cmake_in_range("cmake version 3.16.3"))
        self.assertFalse(osenv.cmake_in_range("cmake version 4.0.1"))


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
                if sys.platform != "win32":
                    (out / "libggml.so.0.9.4").write_text("L" * 100)
                    os.symlink("libggml.so.0.9.4", out / "libggml.so.0")
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
        marker = osenv.build_tier_marker(bin_dir=self.bin)   # tier marker written into THIS bin/ (hermetic)
        self.assertEqual(marker["tier"], "gpu")
        self.assertEqual(marker["arch"], 120)

    @unittest.skipIf(sys.platform == "win32", "symlinks need privilege on Windows")
    def test_install_keeps_symlinks_and_prunes_stale_libs(self):
        self.bin.mkdir()
        (self.bin / "libggml.so.0.9.3").write_text("stale")
        with mock.patch("osenv.os_name", return_value="linux"), \
             mock.patch.object(build_mod, "_resolve_cmake", return_value="cmake"), \
             mock.patch.object(build_mod, "_run", side_effect=self._fake_run([])):
            build_mod.build_llama(cpu=True, force=True)
        self.assertTrue((self.bin / "libggml.so.0").is_symlink())      # not a second full copy
        self.assertFalse((self.bin / "libggml.so.0.9.3").exists())      # old version pruned

    def test_cpu_build_disables_cuda(self):
        cap = []
        with mock.patch("osenv.os_name", return_value="linux"), \
             mock.patch.object(build_mod, "_resolve_cmake", return_value="cmake"), \
             mock.patch.object(build_mod, "_run", side_effect=self._fake_run(cap)):
            build_mod.build_llama(cpu=True, force=True)
        configure = next(c for c in cap if any("GGML_CUDA" in a for a in c))
        self.assertIn("-DGGML_CUDA=OFF", configure)
        self.assertFalse(any("CUDA_ARCHITECTURES" in a for a in configure))
        self.assertEqual(osenv.build_tier_marker(bin_dir=self.bin)["tier"], "cpu")   # marker records CPU tier

    def _configure(self, **kw):
        cap = []
        with mock.patch("osenv.os_name", return_value="linux"), \
             mock.patch.object(build_mod, "_resolve_cmake", return_value="cmake"), \
             mock.patch.object(build_mod, "_run", side_effect=self._fake_run(cap)):
            build_mod.build_llama(cpu=True, force=True, **kw)
        return next(c for c in cap if any("GGML_CUDA" in a for a in c))

    def test_portable_build_targets_a_fixed_baseline(self):
        """A published engine runs on other CPUs: a native build crashes there with an illegal instruction."""
        self.assertIn("-DGGML_NATIVE=OFF", self._configure(portable=True))

    def test_local_build_stays_native(self):
        self.assertFalse(any("GGML_NATIVE" in a for a in self._configure()))

    def test_cuda_missing_root_raises(self):
        with mock.patch("osenv.os_name", return_value="linux"), \
             mock.patch("osenv.gpu_arch", return_value={"CudaArch": 120, "Gen": "Blackwell", "MinCudaMajor": 12}), \
             mock.patch("osenv.best_cuda_root", return_value=None):
            with self.assertRaises(RuntimeError) as cm:
                build_mod.build_llama(cpu=False, force=True)
            self.assertIn("12.8", str(cm.exception))

    def test_dist_multiarch_cuda_build_needs_no_gpu(self):
        # The CI publish path: a fat multi-arch CUDA binary built with NO local GPU (only the toolkit).
        cap = []
        with mock.patch("osenv.os_name", return_value="linux"), \
             mock.patch("osenv.gpu_arch", return_value=None), \
             mock.patch("osenv.best_cuda_root", return_value="/opt/cuda"), \
             mock.patch("osenv.cuda_host_compiler", return_value=None), \
             mock.patch("osenv.assert_cuda_host_compiler_ok"), \
             mock.patch.object(build_mod, "_resolve_cmake", return_value="cmake"), \
             mock.patch.object(build_mod, "_run", side_effect=self._fake_run(cap)):
            build_mod.build_llama(cuda_archs="75;80;89;120", force=True)
        configure = next(c for c in cap if "-DGGML_CUDA=ON" in c)
        self.assertIn("-DCMAKE_CUDA_ARCHITECTURES=75;80;89;120", configure)   # fat binary, no nvidia-smi
        self.assertEqual(osenv.build_tier_marker(bin_dir=self.bin)["tier"], "gpu")


class TestBuildLlamaSwap(_BuildTreeMixin, unittest.TestCase):
    """llama-swap comes from the sha-verified release pinned in versions.lock by default (no Go); Go is only
    needed for --from-source or when no usable pin applies."""

    def setUp(self):
        super().setUp()
        import hashlib
        import tarfile
        (self.repo / "external" / "llama-swap").mkdir(parents=True)
        mock.patch.object(build_mod, "SRC_SWAP", self.repo / "external" / "llama-swap").start()
        self.addCleanup(mock.patch.stopall)
        payload = self.repo / "payload"
        payload.mkdir()
        (payload / osenv.exe_name("llama-swap")).write_text("release-binary")
        (payload / "README.md").write_text("docs")
        self.archive = self.repo / "llama-swap_255_linux_amd64.tar.gz"
        with tarfile.open(self.archive, "w:gz") as t:
            for f in payload.iterdir():
                t.add(f, arcname=f.name)
        self.sha = hashlib.sha256(self.archive.read_bytes()).hexdigest()

    def _pin(self, sha=None, built="c0ffee"):
        return {"version": "v255", "submodule": "external/llama-swap", "builtFromCommit": built,
                "url": self.archive.as_uri(), "sha256": self.sha if sha is None else sha}

    def _patches(self, pin, pinned="c0ffee", go=None):
        from bob import versions
        return [mock.patch.object(versions, "pinned_binary", return_value=pin),
                mock.patch.object(versions, "submodule_commits",
                                  return_value={"external/llama-swap": pinned}),
                mock.patch("build.shutil.which", return_value=go)]

    def _call(self, pin, pinned="c0ffee", go=None, **kw):
        import contextlib
        cap = []

        def fake_run(argv, **k):
            cap.append([str(a) for a in argv])
            Path(argv[argv.index("-o") + 1]).write_text("go-built")

        with contextlib.ExitStack() as es:
            for p in self._patches(pin, pinned, go):
                es.enter_context(p)
            es.enter_context(mock.patch.object(build_mod, "_run", side_effect=fake_run))
            out = build_mod.build_llama_swap(force=True, **kw)
        return out, cap

    def _installed(self):
        return (self.bin / osenv.exe_name("llama-swap")).read_text()

    def test_release_binary_installed_without_go(self):
        out, cap = self._call(self._pin())
        self.assertEqual(self._installed(), "release-binary")
        self.assertEqual(cap, [])                                 # no Go build
        self.assertIn("release binary", out)

    def test_empty_sha_refuses_download_and_needs_go(self):
        with self.assertRaises(RuntimeError) as cm:
            self._call(self._pin(sha=""))
        self.assertIn("Go not found", str(cm.exception))
        self.assertFalse((self.bin / osenv.exe_name("llama-swap")).exists())

    def test_sha_mismatch_falls_back_to_source(self):
        out, cap = self._call(self._pin(sha="0" * 64), go="/usr/bin/go")
        self.assertEqual(self._installed(), "go-built")
        self.assertTrue(any(c[:2] == ["go", "build"] for c in cap))

    def test_submodule_moved_past_release_builds_from_source(self):
        out, cap = self._call(self._pin(), pinned="deadbeef", go="/usr/bin/go")
        self.assertEqual(self._installed(), "go-built")

    def test_from_source_skips_release(self):
        out, cap = self._call(self._pin(), go="/usr/bin/go", from_source=True)
        self.assertEqual(self._installed(), "go-built")
        self.assertIn("Built", out)

    def test_missing_go_raises_from_source(self):
        with self.assertRaises(RuntimeError):
            self._call(self._pin(), go=None, from_source=True)

    def test_committed_lock_pins_this_submodule_with_real_shas(self):
        from bob import versions
        lock = versions.load_lock()
        entry = lock["binaries"]["llama-swap"]
        self.assertEqual(entry["builtFromCommit"], lock["submodules"]["external/llama-swap"])
        for key, asset in entry["assets"].items():
            self.assertRegex(asset["sha256"], r"^[0-9a-f]{64}$", key)
            self.assertTrue(asset["url"].startswith("https://github.com/mostlygeek/llama-swap/releases/"))


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
             mock.patch("bob_core._litellm_key", return_value="sk-generated"), \
             mock.patch.object(build_mod, "_run", side_effect=lambda a, **k: (self.bin.mkdir(exist_ok=True), (self.bin / "fabric").write_text("x"))):
            out = build_mod.setup_fabric(force=True)
        env = (fabric_dir / ".env").read_text()
        self.assertIn("LITELLM_API_BASE_URL=http://localhost:8081/v1", env)
        self.assertIn("LITELLM_API_KEY=sk-generated", env)     # the generated key, never sk-local
        self.assertIn("DEFAULT_MODEL=coder", env)
        self.assertIn("Configured: LiteLLM", out)

    def test_builds_once(self):
        # An existing bin/fabric is not rebuilt without force (setup + fabric-setup never double-build).
        fabric_dir = self.repo / ".config" / "fabric"
        src_fabric = self.repo / "external" / "fabric"
        (src_fabric / "data" / "patterns").mkdir(parents=True)
        (src_fabric / "go.mod").write_text("module fabric")
        self.bin.mkdir(exist_ok=True)
        (self.bin / "fabric").write_text("x")
        build_mod.configure(CFG)
        with mock.patch.object(build_mod, "SRC_FABRIC", src_fabric), \
             mock.patch("osenv.home_config_dir", return_value=fabric_dir), \
             mock.patch("osenv.bin_exe", return_value=self.bin / "fabric"), \
             mock.patch("bob_core._litellm_key", return_value="k"), \
             mock.patch.object(build_mod, "_run") as run:
            build_mod.setup_fabric()
        run.assert_not_called()


class TestReinstallVenv(unittest.TestCase):
    def test_aider_venv_refreshed_only_where_installed(self):
        for present, want in ((False, ["venv-litellm"]), (True, ["venv-litellm", "venv-aider"])):
            with mock.patch("osenv.venv_exe", return_value=mock.Mock(exists=lambda: present)), \
                 mock.patch("osenv.new_bob_venv") as nbv:
                build_mod._reinstall_venv()
            self.assertEqual([c.args[0] for c in nbv.call_args_list], want)


class TestUpdateStack(unittest.TestCase):
    # update over git + build + lock + doctor with a bin/ rollback; every piece is mocked so no
    # git/network/compiler runs. CLI-only.
    def _run(self, before, after, verify=True, tag=None, changed="llama.cpp", cfg=None,
             gpu=None, cuda_ok=True, on_branch=True, prebuilt=False, from_source=False, pending_path=None,
             channel=None, on_tag=False, latest_tag="", stable_target="", swap_effect=None,
             fabric_installed=True):
        """Run update_stack with everything mocked; return (rc, mocks-by-name, git-calls). `changed`
        picks which submodule moves (before -> after); every other submodule stays put, so the test
        controls exactly which component the update should rebuild. `prebuilt` = whether a GPU prebuilt
        engine is available (lifecycle.prebuilt_available); the engine manifest resolver is stubbed to
        None so the source path (build_llama, mocked) runs hermetically regardless — no network."""
        import contextlib
        import health
        import tempfile
        git = []
        exe = Path(tempfile.mkdtemp()) / "bin-artifact"
        exe.write_text("ELF")  # so bin_exe(...).exists() is True after a rebuild
        self.addCleanup(__import__("shutil").rmtree, exe.parent, True)
        # Isolate the pending-rebuild marker to a temp path (never the real data/ dir). Callers can pass a
        # shared pending_path to chain two update_stack runs (fail -> re-run) in one test.
        pend = pending_path or (exe.parent / "update-pending.json")
        src_of = {"llama.cpp": build_mod.SRC_LLAMA, "llama-swap": build_mod.SRC_SWAP,
                  "fabric": build_mod.SRC_FABRIC}
        changed_src = src_of[changed]
        phase = {"after": False}   # flipped by the _reinstall_venv mock, which runs after the submodule sync
        def head(p):
            if p == changed_src:
                return after if phase["after"] else before
            return "same"          # every other submodule is unchanged across the update
        specs = {
            "_git_head": mock.patch.object(build_mod, "_git_head", side_effect=head),
            "_run": mock.patch.object(build_mod, "_run", side_effect=lambda a, **k: git.append([str(x) for x in a])),
            "_reinstall_venv": mock.patch.object(build_mod, "_reinstall_venv",
                                                 side_effect=lambda *a, **k: phase.update(after=True)),
            "build_llama": mock.patch.object(build_mod, "build_llama", return_value="built"),
            "build_llama_swap": mock.patch.object(build_mod, "build_llama_swap", return_value="built",
                                                  side_effect=swap_effect),
            "setup_fabric": mock.patch.object(build_mod, "setup_fabric", return_value="built"),
            "_verify_binary": mock.patch.object(build_mod, "_verify_binary", return_value=verify),
            "backup": mock.patch("osenv.backup_build_output", return_value=Path("/bin.bak")),
            "restore": mock.patch("osenv.restore_build_output", return_value=True),
            "remove_bak": mock.patch("osenv.remove_build_output_backup"),
            "bin_exe": mock.patch("osenv.bin_exe", side_effect=lambda name: exe if (fabric_installed or name != "fabric")
                                  else exe.parent / "no-such-fabric"),
            "on_branch": mock.patch.object(build_mod, "_on_branch", return_value=on_branch),
            # Git-state helpers for channel logic — mocked so the update tests never depend on the real repo's
            # tags/branch. Defaults (on a branch, no tags) reproduce a dev-on-main / latest checkout.
            "head_is_tag": mock.patch.object(build_mod, "_head_is_release_tag", return_value=on_tag),
            "latest_tag": mock.patch.object(build_mod, "_latest_release_tag", return_value=latest_tag),
            "stable_target": mock.patch.object(build_mod, "_stable_target_tag", return_value=stable_target),
            "tracking_branch": mock.patch.object(build_mod, "_tracking_branch", return_value="main"),
            "gpu_info": mock.patch("osenv.gpu_info", return_value=gpu),
            # Tier-0 CUDA ensure that a GPU rebuild gates on (mutable install path is exercised in
            # install_prereqs tests): return a root when CUDA is available, else raise the guidance.
            "ensure_cuda": mock.patch("bob.install_prereqs.ensure_cuda_toolkit",
                                      side_effect=lambda cpu=False: "/usr/local/cuda" if cuda_ok else None),
            # Keep the llama-server rebuild hermetic: ensure_engine runs for real, but with no prebuilt row it
            # falls straight to the (mocked) source build_llama — so no manifest fetch / binary download.
            "select_row": mock.patch("bob.lifecycle._select_engine_row", return_value=None),
            # Whether a GPU prebuilt is available for this host (drives the update tier split for llama-server).
            "prebuilt_avail": mock.patch("bob.lifecycle.prebuilt_available", return_value=prebuilt),
            # Isolate the owed-rebuild marker so tests never write the real data/ dir.
            "pending_path": mock.patch.object(build_mod, "_pending_rebuild_path", return_value=pend),
            "write_lock": mock.patch("bob.versions.write_lock"),   # asserted NOT called: update never relocks
            # update_stack fetches any newly-added models (best-effort). Keep the unit hermetic — never
            # touch the network / attempt a real GGUF download.
            "fetch_models": mock.patch("provision.fetch_models", return_value="models: all present"),
            # voice provisioning on update (setup_voice) — mocked so an enabled cfg never hits the network.
            "setup_voice": mock.patch("provision.setup_voice", return_value="voice ok"),
            "prov_configure": mock.patch("provision.configure"),
            "h_configure": mock.patch.object(health, "configure"),
            "health_check": mock.patch.object(health, "health_check", return_value="doctor-ok"),
        }
        build_mod.configure(cfg or CFG)
        with contextlib.ExitStack() as es:
            mocks = {k: es.enter_context(v) for k, v in specs.items()}
            rc = build_mod.update_stack(tag=tag, from_source=from_source, channel=channel)
        return rc, mocks, git

    def test_unchanged_skips_rebuild_and_never_relocks(self):
        rc, mocks, git = self._run("abc", "abc")
        self.assertEqual(rc, 0)
        for m in ("build_llama", "build_llama_swap", "setup_fabric"):
            mocks[m].assert_not_called()            # nothing moved -> no rebuild
        # versions.lock is tracked and arrived with the checkout: rewriting it here would bake this machine's
        # state into it and dirty the tree, blocking the next update's checkout.
        mocks["write_lock"].assert_not_called()
        mocks["health_check"].assert_called_once()
        self.assertTrue(any("pull" in c for c in git))

    def test_changed_rebuilds_and_discards_backup(self):
        rc, mocks, _ = self._run("aaa", "bbb", verify=True)
        self.assertEqual(rc, 0)
        mocks["build_llama"].assert_called_once()
        for m in ("build_llama_swap", "setup_fabric"):
            mocks[m].assert_not_called()            # only the moved submodule is rebuilt
        mocks["backup"].assert_called_once()
        mocks["remove_bak"].assert_called_once()    # backup discarded on verified success
        mocks["restore"].assert_not_called()

    def test_fabric_rebuilds_on_move_only_where_installed(self):
        # fabric is opt-in: a submodule move must not install it where fabric-setup never ran.
        rc, mocks, _ = self._run("f1", "f2", changed="fabric", fabric_installed=False)
        self.assertEqual(rc, 0)
        mocks["setup_fabric"].assert_not_called()
        rc, mocks, _ = self._run("f1", "f2", changed="fabric", fabric_installed=True)
        mocks["setup_fabric"].assert_called_once()

    def test_nonengine_submodule_rebuilds_when_moved(self):
        # A llama-swap bump (engine unchanged) must still rebuild llama-swap — the regression this guards.
        rc, mocks, _ = self._run("v230", "v239", changed="llama-swap")
        self.assertEqual(rc, 0)
        mocks["build_llama_swap"].assert_called_once()
        mocks["build_llama"].assert_not_called()
        mocks["remove_bak"].assert_called_once()

    def test_changed_verify_fails_rolls_back(self):
        rc, mocks, _ = self._run("aaa", "bbb", verify=False)
        self.assertEqual(rc, 1)                      # handled failure
        mocks["restore"].assert_called_once()        # rolled bin/ back
        mocks["remove_bak"].assert_not_called()

    def test_detached_head_no_newer_skips_pull(self):
        # Stable user on a detached release tag with nothing newer: must NOT `git pull` (no upstream), just
        # no-op. Regression for the detached-HEAD update error.
        rc, _, git = self._run("abc", "abc", on_branch=False)
        self.assertEqual(rc, 0)
        self.assertFalse(any("pull" in c for c in git))   # no pull attempted on a detached HEAD
        self.assertTrue(any("fetch" in c for c in git))   # fetch still happens

    def test_tag_triggers_checkout(self):
        rc, _, git = self._run("x", "x", tag="v0.2.0")
        self.assertEqual(rc, 0)
        self.assertTrue(any("checkout" in c and "v0.2.0" in c for c in git))

    def test_gpu_rebuild_falls_back_to_cpu_when_cuda_missing(self):
        # no toolkit (e.g. atomic host): update rebuilds CPU-tier like setup does, so it still completes.
        rc, mocks, _ = self._run("aaa", "bbb", changed="llama.cpp", gpu={"CudaArch": 120}, cuda_ok=False)
        self.assertEqual(rc, 0)
        mocks["ensure_cuda"].assert_called_once()
        self.assertTrue(mocks["build_llama"].call_args.kwargs["cpu"])    # fell back to a CPU rebuild

    def test_gpu_rebuild_uses_gpu_when_cuda_present(self):
        rc, mocks, _ = self._run("aaa", "bbb", changed="llama.cpp", gpu={"CudaArch": 120}, cuda_ok=True)
        self.assertEqual(rc, 0)
        mocks["ensure_cuda"].assert_called_once()
        self.assertFalse(mocks["build_llama"].call_args.kwargs["cpu"])   # GPU rebuild

    def test_cpu_rebuild_skips_cuda_ensure(self):
        rc, mocks, _ = self._run("aaa", "bbb", changed="llama.cpp", gpu=None)   # cpu tier
        self.assertEqual(rc, 0)
        mocks["ensure_cuda"].assert_not_called()

    def test_gpu_prebuilt_keeps_gpu_when_toolkit_missing(self):
        # THE update-path fix: a GPU box with no toolkit (atomic host) but a matching GPU prebuilt must NOT be
        # downgraded to a CPU engine — setup gets the GPU prebuilt, so update must too. llama-server stays GPU.
        rc, mocks, _ = self._run("aaa", "bbb", changed="llama.cpp",
                                 gpu={"CudaArch": 120}, cuda_ok=False, prebuilt=True)
        self.assertEqual(rc, 0)
        self.assertFalse(mocks["build_llama"].call_args.kwargs["cpu"])   # GPU tier kept, not downgraded

    def test_non_runtime_error_still_rolls_back(self):
        # A download dying with OSError / IncompleteRead / a tarfile error (not a RuntimeError) must still
        # reach the rollback, never escape with bin/ half-replaced and the snapshot left behind.
        import http.client
        for exc in (OSError("disk full"), http.client.IncompleteRead(b"x"), __import__("tarfile").ReadError()):
            rc, mocks, _ = self._run("v230", "v239", changed="llama-swap", swap_effect=exc)
            self.assertEqual(rc, 1, exc)
            mocks["restore"].assert_called_once()
            mocks["remove_bak"].assert_not_called()

    def test_llama_swap_rebuild_honors_from_source(self):
        rc, mocks, _ = self._run("v230", "v239", changed="llama-swap", from_source=True)
        self.assertEqual(rc, 0)
        self.assertTrue(mocks["build_llama_swap"].call_args.kwargs["from_source"])

    def test_failed_rebuild_is_finished_on_rerun(self):
        # H1: a rebuild that fails rolls bin/ back, but the tree/venv already advanced. The owed-rebuild
        # marker must make the NEXT run rebuild even though git now shows the submodule unchanged (else the
        # box is stranded on a stale engine with the tree ahead — the "re-run to finish" message would lie).
        import tempfile
        pend = Path(tempfile.mkdtemp()) / "update-pending.json"
        self.addCleanup(__import__("shutil").rmtree, pend.parent, True)
        rc1, _, _ = self._run("aaa", "bbb", changed="llama.cpp", verify=False, pending_path=pend)
        self.assertEqual(rc1, 1)
        self.assertTrue(pend.exists())                       # owed rebuild recorded, kept across the failure
        # Re-run: git shows nothing moved (tree already at bbb), but the marker forces the rebuild.
        rc2, mocks2, _ = self._run("bbb", "bbb", changed="llama.cpp", verify=True, pending_path=pend)
        self.assertEqual(rc2, 0)
        mocks2["build_llama"].assert_called_once()           # rebuilt despite an unchanged tree
        self.assertFalse(pend.exists())                      # marker cleared once the rebuild verified

    def test_from_source_no_toolkit_falls_back_to_cpu_not_crash(self):
        # --from-source ignores prebuilts, so a toolkit-less GPU box must do a CPU source fallback (warn
        # policy) rather than take the GPU source path and crash — even when a GPU prebuilt is published.
        rc, mocks, _ = self._run("aaa", "bbb", changed="llama.cpp",
                                 gpu={"CudaArch": 120}, cuda_ok=False, prebuilt=True, from_source=True)
        self.assertEqual(rc, 0)
        self.assertTrue(mocks["build_llama"].call_args.kwargs["cpu"])   # CPU fallback, no crash

    def test_provisions_voice_when_enabled(self):
        # update must leave a fully working default: provision voice (STT model + piper + audio deps)
        # the same as a fresh setup, so voice works post-update without a manual `bob setup-voice`.
        rc, mocks, _ = self._run("abc", "abc", cfg={"litellmPort": 8081, "voice": {"enabled": True}})
        self.assertEqual(rc, 0)
        mocks["setup_voice"].assert_called_once()

    def test_skips_voice_when_disabled(self):
        rc, mocks, _ = self._run("abc", "abc")   # CFG has no voice block
        mocks["setup_voice"].assert_not_called()

    def test_channel_latest_from_tag_switches_to_branch(self):
        # Explicit --channel latest while on a detached release tag must leave the tag for the tracking branch
        # (previously a no-op: it printed "nothing newer" and stayed on the tag).
        rc, _, git = self._run("x", "x", channel="latest", on_tag=True, on_branch=False)
        self.assertEqual(rc, 0)
        self.assertTrue(any("checkout" in c and "main" in c for c in git))

    def test_channel_stable_from_branch_switches_to_newest_tag(self):
        # Explicit --channel stable while on a branch must jump to the newest release tag (previously it just
        # fast-forwarded the branch and stayed on latest).
        rc, _, git = self._run("x", "x", channel="stable", on_tag=False, latest_tag="v2.0.0")
        self.assertEqual(rc, 0)
        self.assertTrue(any("checkout" in c and "v2.0.0" in c for c in git))


class TestUpdateChannel(unittest.TestCase):
    """Channel is explicit-or-inferred-from-checkout; stable never downgrades a checkout already at/ahead."""

    def test_explicit_channel_wins(self):
        self.assertEqual(build_mod.resolve_update_channel("stable"), "stable")
        self.assertEqual(build_mod.resolve_update_channel("latest"), "latest")

    def test_infers_stable_on_a_release_tag(self):
        with mock.patch.object(build_mod, "_head_is_release_tag", return_value=True):
            self.assertEqual(build_mod.resolve_update_channel(None), "stable")

    def test_infers_latest_on_a_branch(self):
        with mock.patch.object(build_mod, "_head_is_release_tag", return_value=False):
            self.assertEqual(build_mod.resolve_update_channel(None), "latest")

    def test_stable_target_stays_when_head_already_ahead(self):
        with mock.patch("bob.lifecycle.latest_ready_release_tag", return_value="v1.1.0"), \
             mock.patch.object(build_mod, "subprocess") as sp:
            sp.run.return_value = mock.Mock(returncode=0)   # tag is an ancestor of HEAD -> no downgrade
            self.assertEqual(build_mod._stable_target_tag(), "")

    def test_stable_target_moves_forward_to_newer_tag(self):
        with mock.patch("bob.lifecycle.latest_ready_release_tag", return_value="v2.0.0"), \
             mock.patch.object(build_mod, "subprocess") as sp:
            sp.run.return_value = mock.Mock(returncode=1)   # tag not an ancestor -> move to it
            self.assertEqual(build_mod._stable_target_tag(), "v2.0.0")

    def test_stable_target_empty_when_no_tags(self):
        with mock.patch("bob.lifecycle.latest_ready_release_tag", return_value=None), \
             mock.patch.object(build_mod, "_latest_release_tag", return_value=""):
            self.assertEqual(build_mod._stable_target_tag(), "")

    def test_stable_target_prefers_ready_over_newest(self):
        # Newest tag (v1.2.5) is mid-publish; the readiness probe returns the newest READY release (v1.2.4).
        # A stable user moves to v1.2.4, not the half-published v1.2.5.
        with mock.patch("bob.lifecycle.latest_ready_release_tag", return_value="v1.2.4"), \
             mock.patch.object(build_mod, "subprocess") as sp:
            sp.run.return_value = mock.Mock(returncode=1)   # not an ancestor -> move to it
            self.assertEqual(build_mod._stable_target_tag(), "v1.2.4")

    def test_stable_target_falls_back_to_newest_when_probe_unavailable(self):
        # Readiness probe returns None (offline / no origin) -> fall back to the newest tag (old behavior).
        with mock.patch("bob.lifecycle.latest_ready_release_tag", return_value=None), \
             mock.patch.object(build_mod, "_latest_release_tag", return_value="v2.0.0"), \
             mock.patch.object(build_mod, "subprocess") as sp:
            sp.run.return_value = mock.Mock(returncode=1)
            self.assertEqual(build_mod._stable_target_tag(), "v2.0.0")


class TestRestartAfterUpdate(unittest.TestCase):
    """The endpoint restart that makes one `bob update` the whole move (build.py)."""

    def _stack(self, up, tracked_pid=4242):
        fake = mock.Mock()
        fake.service_snapshot.return_value = [{"core": True, "up": up},
                                              {"core": False, "up": False}]
        fake.stack_restart.return_value = "Restarting endpoint..."
        fake.endpoint_tracked_pid.return_value = tracked_pid
        return fake

    def test_restarts_a_running_endpoint(self):
        fake = self._stack(up=True)
        with mock.patch.dict(sys.modules, {"stack": fake}):
            build_mod._restart_running_endpoint()
        fake.stack_restart.assert_called_once()

    def test_leaves_a_stopped_stack_down(self):
        fake = self._stack(up=False)
        with mock.patch.dict(sys.modules, {"stack": fake}):
            build_mod._restart_running_endpoint()
        fake.stack_restart.assert_not_called()

    def test_untracked_endpoint_is_reported_not_killed(self):
        # A foreground `bob serve` (or an orphan) writes no pidfile. The update must not kill a process
        # it does not own; it says so and leaves the endpoint serving.
        fake = self._stack(up=True, tracked_pid=None)
        with mock.patch.dict(sys.modules, {"stack": fake}), \
             mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            build_mod._restart_running_endpoint()
        fake.stack_restart.assert_not_called()
        self.assertIn("untracked", err.getvalue())

    def test_restart_failure_is_advisory(self):
        fake = self._stack(up=True)
        fake.stack_restart.side_effect = RuntimeError("port busy")
        with mock.patch.dict(sys.modules, {"stack": fake}):
            build_mod._restart_running_endpoint()   # must not raise — the update is already verified


class TestCliArgParsing(unittest.TestCase):
    def _update(self, argv):
        seen = {}
        fake = mock.Mock()
        fake.update_stack = mock.Mock(
            side_effect=lambda tag=None, from_source=False, channel=None, restart=True: seen.update(
                tag=tag, from_source=from_source, channel=channel, restart=restart) or 0)
        with mock.patch.object(cli, "_build_mod", return_value=fake):
            cli._handle_update(argv)
        return seen

    def test_tag_flag_parsed(self):
        seen = self._update(["--tag", "v1.2.3", "--from-source", "--channel", "stable"])
        self.assertEqual(seen["tag"], "v1.2.3")
        self.assertTrue(seen["from_source"])
        self.assertEqual(seen["channel"], "stable")

    def test_restart_defaults_on(self):
        # `bob update` alone finishes the move: a running endpoint is restarted onto the new build.
        self.assertTrue(self._update([])["restart"])

    def test_no_restart_flag_opts_out(self):
        self.assertFalse(self._update(["--no-restart"])["restart"])


if __name__ == "__main__":
    unittest.main()


class TestDistBuildIsPortable(unittest.TestCase):
    """`bob build --dist` produces the published engines, so both tiers compile for the portable baseline."""

    def _handle(self, argv):
        from bob import cli
        fake = mock.Mock()
        fake.build_llama.return_value = "built"
        with mock.patch.object(cli, "_build_mod", return_value=fake):
            self.assertEqual(cli._handle_build(argv), 0)
        return fake.build_llama.call_args.kwargs

    def test_cpu_dist_is_portable(self):
        self.assertTrue(self._handle(["--dist", "--cpu"])["portable"])

    def test_cuda_dist_is_portable(self):
        kw = self._handle(["--dist", "--cuda-archs", "75;89"])
        self.assertTrue(kw["portable"])
        self.assertEqual(kw["cuda_archs"], "75;89")
