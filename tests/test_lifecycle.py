"""The single install/update lifecycle seam (scripts/bob/lifecycle.py) + the build-tier marker + the loud
CPU-on-GPU diagnose safety net. Hermetic: nvidia-smi / CUDA disk / the source build are all mocked.

These tests are the executable proof of the core guarantee: a GPU-capable box with no reachable CUDA toolkit
and no --cpu consent is BLOCKED, never a silent CPU build; and no build path derives the tier outside the one
seam. Follows the mock.patch("osenv.<fn>") idiom of test_cuda_mlock.py / test_build.py."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _common  # noqa: F401 — puts scripts/ on sys.path
import osenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "tools"))
from bob import lifecycle  # noqa: E402

_BLACKWELL = {"CudaArch": 120, "Gen": "Blackwell", "MinCudaMajor": 12}


class TestResolveBuildTier(unittest.TestCase):
    """The ONE tier decision. Rules: consent -> cpu; no GPU -> cpu; GPU+toolkit -> gpu; GPU+no-toolkit -> blocked."""

    def test_cpu_consent_is_cpu_never_blocked(self):
        with mock.patch("osenv.gpu_arch", return_value=_BLACKWELL):
            d = lifecycle.resolve_build_tier(cpu=True)
        self.assertEqual(d["tier"], "cpu")
        self.assertFalse(d["blocked"])

    def test_no_gpu_is_cpu(self):
        with mock.patch("osenv.gpu_info", return_value=None), mock.patch("osenv.gpu_arch", return_value=None):
            d = lifecycle.resolve_build_tier()
        self.assertEqual(d["tier"], "cpu")
        self.assertFalse(d["blocked"])

    def test_gpu_with_toolkit_is_gpu(self):
        with mock.patch("osenv.gpu_info", return_value=_BLACKWELL), \
             mock.patch("osenv.gpu_arch", return_value=_BLACKWELL), \
             mock.patch("osenv.best_cuda_root", return_value="/usr/local/cuda-12.8"):
            d = lifecycle.resolve_build_tier(self_heal=False)
        self.assertEqual(d["tier"], "gpu")
        self.assertFalse(d["blocked"])
        self.assertEqual(d["cuda_root"], "/usr/local/cuda-12.8")

    def test_gpu_without_toolkit_is_blocked_not_cpu(self):
        """THE fix: a GPU box with no toolkit and no consent is blocked, NOT a silent CPU build."""
        with mock.patch("osenv.gpu_info", return_value=_BLACKWELL), \
             mock.patch("osenv.gpu_arch", return_value=_BLACKWELL), \
             mock.patch("osenv.best_cuda_root", return_value=None), \
             mock.patch("osenv.cuda_missing_message", return_value="install CUDA or use a distrobox"):
            d = lifecycle.resolve_build_tier(self_heal=False)
        self.assertTrue(d["blocked"])
        self.assertNotEqual(d["tier"], "cpu")
        self.assertIn("distrobox", d["remedy"])

    def test_self_heal_installs_then_resolves_gpu(self):
        """self_heal delegates to install_prereqs.ensure_cuda_toolkit (which installs on a mutable distro)."""
        with mock.patch("osenv.gpu_info", return_value=_BLACKWELL), \
             mock.patch("osenv.gpu_arch", return_value=_BLACKWELL), \
             mock.patch("bob.install_prereqs.ensure_cuda_toolkit", return_value="/usr/local/cuda-12.9") as ensure:
            d = lifecycle.resolve_build_tier(self_heal=True)
        ensure.assert_called_once()
        self.assertEqual(d["tier"], "gpu")
        self.assertFalse(d["blocked"])


class TestApplyBlockPolicy(unittest.TestCase):
    def _blocked(self):
        return {"tier": "gpu", "cuda_root": None, "arch": 120, "blocked": True,
                "reason": "GPU present, no toolkit", "remedy": "install CUDA or use a distrobox"}

    def test_stop_raises_with_remedy(self):
        with self.assertRaises(RuntimeError) as cm:
            lifecycle.apply_block_policy(self._blocked(), on_block="stop")
        self.assertIn("distrobox", str(cm.exception))

    def test_warn_downgrades_to_cpu(self):
        d = lifecycle.apply_block_policy(self._blocked(), on_block="warn")
        self.assertEqual(d["tier"], "cpu")
        self.assertFalse(d["blocked"])

    def test_non_blocked_passthrough(self):
        ok = {"tier": "gpu", "blocked": False}
        self.assertIs(lifecycle.apply_block_policy(ok, on_block="stop"), ok)


class TestEnsureEngine(unittest.TestCase):
    def test_blocked_stop_raises_before_building(self):
        build = __import__("build")
        with mock.patch("osenv.gpu_info", return_value=_BLACKWELL), \
             mock.patch("osenv.gpu_arch", return_value=_BLACKWELL), \
             mock.patch.object(lifecycle, "_select_engine_row", return_value=None), \
             mock.patch("bob.install_prereqs.ensure_cuda_toolkit", return_value=None), \
             mock.patch("osenv.cuda_missing_message", return_value="use a distrobox"), \
             mock.patch.object(build, "build_llama") as bl:
            with self.assertRaises(RuntimeError):
                lifecycle.ensure_engine(cpu=False, on_block="stop")
        bl.assert_not_called()

    def test_gpu_build_passes_arch_and_root(self):
        build = __import__("build")
        with mock.patch("osenv.gpu_info", return_value=_BLACKWELL), \
             mock.patch("osenv.gpu_arch", return_value=_BLACKWELL), \
             mock.patch.object(lifecycle, "_select_engine_row", return_value=None), \
             mock.patch("bob.install_prereqs.ensure_cuda_toolkit", return_value="/usr/local/cuda-12.8"), \
             mock.patch.object(build, "build_llama", return_value="Built.") as bl:
            res = lifecycle.ensure_engine(cpu=False, on_block="stop")
        self.assertEqual(res["tier"], "gpu")
        self.assertEqual(res["source"], "source")
        self.assertEqual(bl.call_args.kwargs["cpu"], False)
        self.assertEqual(bl.call_args.kwargs["arch"], 120)
        self.assertEqual(bl.call_args.kwargs["cuda_root"], "/usr/local/cuda-12.8")

    def test_cpu_build_passes_cpu_true(self):
        build = __import__("build")
        with mock.patch("osenv.gpu_info", return_value=None), \
             mock.patch("osenv.gpu_arch", return_value=None), \
             mock.patch.object(lifecycle, "_select_engine_row", return_value=None), \
             mock.patch.object(build, "build_llama", return_value="Built.") as bl:
            res = lifecycle.ensure_engine(cpu=True, on_block="stop")
        self.assertEqual(res["tier"], "cpu")
        self.assertEqual(bl.call_args.kwargs["cpu"], True)
        self.assertEqual(bl.call_args.kwargs["arch"], 0)


class TestBuildTierMarker(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self._patch = mock.patch.object(osenv, "REPO", self.tmp)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_missing_marker_is_none(self):
        self.assertIsNone(osenv.build_tier_marker())

    def test_round_trip(self):
        osenv.write_build_tier_marker("gpu", 120, "12", "source")
        m = osenv.build_tier_marker()
        self.assertEqual(m["tier"], "gpu")
        self.assertEqual(m["arch"], 120)
        self.assertEqual(m["source"], "source")
        self.assertIn("builtAt", m)

    def test_corrupt_marker_is_none(self):
        p = osenv.build_tier_marker_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not json", encoding="utf-8")
        self.assertIsNone(osenv.build_tier_marker())


class TestEngineTierReport(unittest.TestCase):
    """The diagnose safety-net logic (health.engine_tier_report)."""

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "tools"))
        import health
        self.health = health

    def test_no_marker(self):
        val, extra, issue = self.health.engine_tier_report(_BLACKWELL, None)
        self.assertIn("no build-tier marker", val)
        self.assertFalse(issue)

    def test_cpu_on_gpu_is_loud_issue(self):
        with mock.patch("osenv.best_cuda_root", return_value=None), \
             mock.patch("osenv.cuda_missing_message", return_value="use a distrobox"):
            val, extra, issue = self.health.engine_tier_report(_BLACKWELL, {"tier": "cpu", "source": "source"})
        self.assertTrue(issue)
        self.assertIn("IDLE", val)
        self.assertTrue(any("distrobox" in ln for ln in extra))

    def test_cpu_on_gpu_with_toolkit_points_at_rebuild(self):
        with mock.patch("osenv.best_cuda_root", return_value="/usr/local/cuda-12.8"):
            val, extra, issue = self.health.engine_tier_report(_BLACKWELL, {"tier": "cpu", "source": "source"})
        self.assertTrue(issue)
        self.assertTrue(any("bob build --force" in ln for ln in extra))

    def test_gpu_build_is_clean(self):
        val, extra, issue = self.health.engine_tier_report(_BLACKWELL, {"tier": "gpu", "source": "source"})
        self.assertFalse(issue)
        self.assertIn("gpu build", val)

    def test_cpu_build_no_gpu_is_clean(self):
        val, extra, issue = self.health.engine_tier_report(None, {"tier": "cpu", "source": "source"})
        self.assertFalse(issue)


class TestNoTierDriftOutsideSeam(unittest.TestCase):
    """Anti-drift: the entry points must NOT re-derive the tier from gpu_info()/best_cuda_root(). The one
    legitimate place is lifecycle.resolve_build_tier. This is what keeps the four old deciders collapsed."""

    def _func_body(self, relpath, defline):
        """Source of one top-level function: from its `def` to the next top-level `def`/`class` or EOF."""
        src = (Path(__file__).resolve().parent.parent / relpath).read_text(encoding="utf-8")
        start = src.index(defline)
        rest = src[start + len(defline):]
        ends = [rest.index(m) for m in ("\ndef ", "\nclass ") if m in rest]
        return rest[:min(ends)] if ends else rest

    def test_handle_build_has_no_inline_tier(self):
        body = self._func_body("scripts/bob/cli.py", "def _handle_build(")
        self.assertNotIn("gpu_info", body)
        self.assertIn("lifecycle.ensure_engine", body)

    def test_kernel_bootstrap_has_no_inline_tier(self):
        body = self._func_body("scripts/bob/kernel.py", "def bootstrap(")
        self.assertNotIn("best_cuda_root", body)
        self.assertIn("lifecycle.ensure_engine", body)

    def test_update_stack_derives_tier_via_seam(self):
        body = self._func_body("scripts/tools/build.py", "def update_stack(")
        self.assertNotIn("gpu_info() is None", body)
        self.assertIn("resolve_build_tier", body)


class TestEngineManifestResolution(unittest.TestCase):
    """_load_engine_manifest: local config/engines.json override (dev/test), else the release-hosted
    engines.json for the checkout's exact tag or (off a tag) the newest release tag, else {} (source). The
    manifest is NOT in versions.lock."""

    def setUp(self):
        import json
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.repo, True)
        (self.repo / "config").mkdir()
        self.cfg = self.repo / "config" / "engines.json"
        self._json = json
        p = mock.patch.object(osenv, "REPO", self.repo)
        p.start()
        self.addCleanup(p.stop)

    def test_local_override_used_and_skips_docs(self):
        self.cfg.write_text(self._json.dumps({
            "_comment": "doc",
            "llama-server-linux-x86_64-cuda": {"component": "llama-server", "tier": "gpu"}}), encoding="utf-8")
        self.assertEqual(list(lifecycle._load_engine_manifest()), ["llama-server-linux-x86_64-cuda"])

    def test_no_local_no_tag_is_empty(self):
        with mock.patch.object(lifecycle, "_current_release_tag", return_value=None), \
             mock.patch.object(lifecycle, "_latest_release_tag", return_value=None):
            self.assertEqual(lifecycle._load_engine_manifest(), {})

    def test_off_tag_falls_back_to_latest_release(self):
        # On main (not exactly on a tag) the manifest still resolves from the newest release tag; the
        # commit-match guard in _select_engine_row is what keeps that safe.
        payload = self._json.dumps({
            "llama-server-linux-x86_64-cuda": {"component": "llama-server"}}).encode()
        cm = mock.MagicMock()
        cm.__enter__.return_value.read.return_value = payload
        with mock.patch.object(lifecycle, "_current_release_tag", return_value=None), \
             mock.patch.object(lifecycle, "_latest_release_tag", return_value="v1.2.0"), \
             mock.patch.object(lifecycle, "_repo_slug", return_value="owner/repo"), \
             mock.patch("urllib.request.urlopen", return_value=cm):
            self.assertEqual(list(lifecycle._load_engine_manifest()), ["llama-server-linux-x86_64-cuda"])

    def test_release_manifest_fetched_on_tag(self):
        payload = self._json.dumps({"_comment": "x",
                                    "llama-server-linux-x86_64-cuda": {"component": "llama-server"}}).encode()
        cm = mock.MagicMock()
        cm.__enter__.return_value.read.return_value = payload
        with mock.patch.object(lifecycle, "_current_release_tag", return_value="v1.2.0"), \
             mock.patch.object(lifecycle, "_repo_slug", return_value="owner/repo"), \
             mock.patch("urllib.request.urlopen", return_value=cm):
            self.assertEqual(list(lifecycle._load_engine_manifest()), ["llama-server-linux-x86_64-cuda"])

    def test_fetch_failure_is_empty(self):
        with mock.patch.object(lifecycle, "_current_release_tag", return_value="v1.2.0"), \
             mock.patch.object(lifecycle, "_repo_slug", return_value="owner/repo"), \
             mock.patch("urllib.request.urlopen", side_effect=OSError("offline")):
            self.assertEqual(lifecycle._load_engine_manifest(), {})


class TestCommitMatchGuard(unittest.TestCase):
    """_select_engine_row only returns a prebuilt row whose builtFromCommit matches the pinned submodule
    commit, so a prebuilt is never a different llama.cpp version than a source build would produce here."""

    def _row(self, built):
        # The published manifest names the accelerated tier "cuda" (the CI matrix + asset vocabulary); the
        # tier decision asks for "gpu". The row here uses "cuda" on purpose so these also cover the synonym
        # bridge (query "gpu" must resolve a "cuda" row).
        r = {"component": "llama-server", "os": "linux", "cpuArch": "x86_64", "tier": "cuda"}
        if built is not None:
            r["builtFromCommit"] = built
        return r

    def _select(self, built, pinned):
        with mock.patch.object(lifecycle, "_load_engine_manifest", return_value={"r": self._row(built)}), \
             mock.patch.object(lifecycle, "_pinned_submodule_commit", return_value=pinned):
            return lifecycle._select_engine_row("llama-server", "linux", "x86_64", "gpu")

    def test_matching_commit_is_used(self):
        self.assertIsNotNone(self._select("abc123", "abc123"))

    def test_mismatched_commit_is_skipped(self):
        self.assertIsNone(self._select("abc123", "def456"))   # version skew -> skip -> source

    def test_missing_builtfrom_is_allowed(self):
        self.assertIsNotNone(self._select(None, "abc123"))    # unversioned row: guard does not block

    def test_unknown_pinned_is_allowed(self):
        self.assertIsNotNone(self._select("abc123", None))    # git unavailable: don't break the install

    def test_cpu_query_does_not_match_cuda_row(self):
        with mock.patch.object(lifecycle, "_load_engine_manifest", return_value={"r": self._row("abc123")}), \
             mock.patch.object(lifecycle, "_pinned_submodule_commit", return_value="abc123"):
            self.assertIsNone(lifecycle._select_engine_row("llama-server", "linux", "x86_64", "cpu"))


class TestInstallPrebuilt(unittest.TestCase):
    """The real download + SHA-verify + extract + stage path, driven by a local tar.gz over file://."""

    def setUp(self):
        import hashlib
        import tarfile
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        # Build a fake engine archive: the binary + a bundled runtime lib.
        payload = self.tmp / "payload"
        payload.mkdir()
        (payload / "llama-server").write_text("ELF-fake")
        (payload / "libcudart.so.12").write_text("lib-fake")
        self.archive = self.tmp / "llama-server-linux-x86_64-cuda.tar.gz"
        with tarfile.open(self.archive, "w:gz") as t:
            for f in payload.iterdir():
                t.add(f, arcname=f.name)
        self.sha = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.bin = self.tmp / "bin"

    def _row(self, sha):
        return {"url": self.archive.as_uri(), "sha256": sha, "component": "llama-server",
                "tier": "gpu", "cudaMajor": "12"}

    def test_verified_download_stages_binary_and_libs(self):
        out = lifecycle._install_prebuilt(self._row(self.sha), self.bin)
        self.assertTrue((self.bin / "llama-server").exists())
        self.assertTrue((self.bin / "libcudart.so.12").exists())   # bundled runtime lib staged (driver-only)
        self.assertIn("Installed prebuilt", out)

    def test_sha_mismatch_raises(self):
        with self.assertRaises(RuntimeError) as cm:
            lifecycle._install_prebuilt(self._row("deadbeef"), self.bin)
        self.assertIn("SHA256 mismatch", str(cm.exception))
        self.assertFalse((self.bin / "llama-server").exists())     # nothing staged on a bad hash


class TestEnsureEnginePrebuiltFirst(unittest.TestCase):
    def test_prebuilt_used_when_row_matches(self):
        build = __import__("build")
        row = {"component": "llama-server", "cudaMajor": "12"}
        with mock.patch("osenv.gpu_info", return_value=None), mock.patch("osenv.gpu_arch", return_value=None), \
             mock.patch.object(lifecycle, "_select_engine_row", return_value=row), \
             mock.patch.object(lifecycle, "_install_prebuilt", return_value="Installed prebuilt") as inst, \
             mock.patch.object(lifecycle, "_binary_runs", return_value=True), \
             mock.patch("osenv.write_build_tier_marker"), \
             mock.patch("osenv.bin_exe") as be, \
             mock.patch.object(build, "build_llama") as bl:
            be.return_value = Path("/nonexistent/llama-server")   # not already present -> install
            res = lifecycle.ensure_engine(cpu=True)
        self.assertEqual(res["source"], "prebuilt")
        inst.assert_called_once()
        bl.assert_not_called()                                    # no source compile when a prebuilt lands

    def test_prebuilt_that_wont_run_falls_back_to_source(self):
        # glibc-too-old / wrong-ABI: the prebuilt stages but won't launch -> drop it and build from source,
        # so no user is ever left with a non-starting engine. This is what makes "all distros" honest.
        build = __import__("build")
        with mock.patch("osenv.gpu_info", return_value=None), mock.patch("osenv.gpu_arch", return_value=None), \
             mock.patch.object(lifecycle, "_select_engine_row", return_value={"component": "llama-server"}), \
             mock.patch.object(lifecycle, "_install_prebuilt", return_value="Installed"), \
             mock.patch.object(lifecycle, "_binary_runs", return_value=False), \
             mock.patch("osenv.write_build_tier_marker"), \
             mock.patch("osenv.bin_exe", return_value=Path("/nonexistent/llama-server")), \
             mock.patch.object(build, "build_llama", return_value="built") as bl:
            res = lifecycle.ensure_engine(cpu=True)
        self.assertEqual(res["source"], "source")
        bl.assert_called_once()

    def test_prebuilt_used_on_gpu_without_toolkit_no_block(self):
        # The atomic Bazzite case: GPU present, no toolkit (a SOURCE build would block on on_block='stop'),
        # but a prebuilt exists -> use it, never probe/install the toolkit, never raise. This is the outlier
        # fix: prebuilt-first must run BEFORE the toolkit self-heal/block.
        build = __import__("build")
        with mock.patch("osenv.gpu_info", return_value=_BLACKWELL), \
             mock.patch("osenv.gpu_arch", return_value=_BLACKWELL), \
             mock.patch("osenv.best_cuda_root", return_value=None), \
             mock.patch("bob.install_prereqs.ensure_cuda_toolkit") as ensure, \
             mock.patch.object(lifecycle, "_select_engine_row", return_value={"component": "llama-server"}), \
             mock.patch.object(lifecycle, "_install_prebuilt", return_value="Installed") as inst, \
             mock.patch.object(lifecycle, "_binary_runs", return_value=True), \
             mock.patch("osenv.write_build_tier_marker"), \
             mock.patch("osenv.bin_exe", return_value=Path("/nonexistent/llama-server")), \
             mock.patch.object(build, "build_llama") as bl:
            res = lifecycle.ensure_engine(cpu=False, on_block="stop")   # would RAISE if it blocked
        self.assertEqual(res["source"], "prebuilt")
        self.assertEqual(res["tier"], "gpu")
        inst.assert_called_once()
        ensure.assert_not_called()        # driver-only prebuilt: no CUDA-toolkit probe/install
        bl.assert_not_called()

    def test_falls_back_to_source_when_no_row(self):
        build = __import__("build")
        with mock.patch("osenv.gpu_info", return_value=None), mock.patch("osenv.gpu_arch", return_value=None), \
             mock.patch.object(lifecycle, "_select_engine_row", return_value=None), \
             mock.patch.object(build, "build_llama", return_value="built") as bl:
            res = lifecycle.ensure_engine(cpu=True)
        self.assertEqual(res["source"], "source")
        bl.assert_called_once()

    def test_update_style_call_trusts_gpu_decision_no_reblock(self):
        # Regression (CI): `bob update` resolves the tier itself and calls ensure_engine with self_heal=False.
        # The seam must TRUST that GPU decision and not re-block into CPU just because best_cuda_root reads None
        # here (a just-installed toolkit the probe misses, or a GPU-less CI runner). Otherwise a GPU rebuild
        # silently downgrades to CPU.
        build = __import__("build")
        with mock.patch("osenv.gpu_info", return_value=_BLACKWELL), \
             mock.patch("osenv.gpu_arch", return_value=_BLACKWELL), \
             mock.patch("osenv.best_cuda_root", return_value=None), \
             mock.patch.object(lifecycle, "_select_engine_row", return_value=None), \
             mock.patch.object(build, "build_llama", return_value="built") as bl:
            res = lifecycle.ensure_engine(cpu=False, on_block="warn", self_heal=False)
        self.assertEqual(res["source"], "source")
        self.assertEqual(res["tier"], "gpu")
        self.assertFalse(bl.call_args.kwargs["cpu"])   # GPU rebuild, not a spurious CPU downgrade

    def test_from_source_skips_prebuilt(self):
        build = __import__("build")
        with mock.patch("osenv.gpu_info", return_value=None), mock.patch("osenv.gpu_arch", return_value=None), \
             mock.patch.object(lifecycle, "_select_engine_row") as sel, \
             mock.patch.object(build, "build_llama", return_value="built") as bl:
            res = lifecycle.ensure_engine(cpu=True, from_source=True)
        self.assertEqual(res["source"], "source")
        sel.assert_not_called()                                   # --from-source never even looks for a prebuilt
        bl.assert_called_once()

    def test_prebuilt_failure_falls_back_to_source(self):
        build = __import__("build")
        with mock.patch("osenv.gpu_info", return_value=None), mock.patch("osenv.gpu_arch", return_value=None), \
             mock.patch.object(lifecycle, "_select_engine_row", return_value={"component": "llama-server"}), \
             mock.patch.object(lifecycle, "_install_prebuilt", side_effect=RuntimeError("404")), \
             mock.patch("osenv.bin_exe", return_value=Path("/nonexistent/llama-server")), \
             mock.patch.object(build, "build_llama", return_value="built") as bl:
            res = lifecycle.ensure_engine(cpu=True)
        self.assertEqual(res["source"], "source")                # a broken download degrades to source, not a crash
        bl.assert_called_once()


if __name__ == "__main__":
    unittest.main()
