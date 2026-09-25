"""The cold-start kernel (scripts/bob/kernel.py + scripts/bob/install_prereqs.py) +
the Tier-0 osenv provisioning seams (PACKAGE_MAP / resolve_package_name / resolve_package_cmd /
install_package / python_at_least / new_bob_venv). The kernel runs pre-venv under system python3 and
IMPORTS the ported capabilities (build/provision/generate/stack/health), so these tests exercise the
pure resolvers + the dispatch/normalization/wiring with the heavy work (cmake/go/pip/network) mocked.
Covers both-OS package-family resolution (resolve_package_cmd / resolve_package_name)."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _common  # noqa: F401 — puts scripts/ on sys.path
import osenv
from bob import install_prereqs, kernel

REPO = Path(__file__).resolve().parent.parent


# --- Tier-0 osenv seams: package family ---------------------------------------------------------

class TestPackageSeams(unittest.TestCase):
    def test_resolve_name_per_manager(self):
        self.assertEqual(osenv.resolve_package_name("cuda", "apt"), "nvidia-cuda-toolkit")
        self.assertEqual(osenv.resolve_package_name("cuda", "pacman"), "cuda")
        self.assertEqual(osenv.resolve_package_name("toolchain-cc", "dnf"), "gcc-c++")
        self.assertEqual(osenv.resolve_package_name("node", "zypper"), "nodejs-default")

    def test_resolve_name_bundled_returns_none(self):
        # make is bundled in base-devel (pacman) and build-essential (apt); the caller skips it.
        self.assertIsNone(osenv.resolve_package_name("make", "pacman"))
        self.assertIsNone(osenv.resolve_package_name("make", "apt"))
        self.assertIsNone(osenv.resolve_package_name("python-venv", "dnf"))

    def test_resolve_name_unknown_logical_raises(self):
        with self.assertRaises(KeyError):
            osenv.resolve_package_name("totally-made-up", "apt")

    def test_resolve_name_unknown_manager_raises(self):
        with self.assertRaises(KeyError):
            osenv.resolve_package_name("git", "brew")

    def test_package_map_every_cell_resolves_for_every_manager(self):
        # No mapping gap silently no-ops an install — every logical resolves (to a name or None) for all 4.
        for logical in osenv.PACKAGE_MAP:
            for mgr in ("apt", "dnf", "pacman", "zypper"):
                # Should not raise; None is a valid "bundled" answer.
                osenv.resolve_package_name(logical, mgr)

    def test_resolve_cmd_windows_winget(self):
        spec = osenv.resolve_package_cmd("Kitware.CMake", os="windows")
        self.assertEqual(spec["Exe"], "winget")
        self.assertFalse(spec["Sudo"])
        self.assertIn("--accept-package-agreements", spec["Args"])

    def test_resolve_cmd_linux_all_managers(self):
        cases = {"apt": ("apt-get", ["install", "-y", "cmake"]),
                 "dnf": ("dnf", ["install", "-y", "cmake"]),
                 "pacman": ("pacman", ["-S", "--needed", "--noconfirm", "cmake"]),
                 "zypper": ("zypper", ["--non-interactive", "install", "cmake"]),
                 "rpm-ostree": ("rpm-ostree", ["install", "--idempotent", "--allow-inactive", "cmake"])}
        for mgr, (exe, args) in cases.items():
            spec = osenv.resolve_package_cmd("cmake", os="linux", manager=mgr)
            self.assertEqual(spec["Exe"], exe, mgr)
            self.assertEqual(spec["Args"], args, mgr)
            self.assertTrue(spec["Sudo"], mgr)

    def test_rpm_ostree_reuses_the_dnf_names(self):
        # atomic Fedora layers Fedora RPMs — resolve via the dnf column, no separate table.
        self.assertEqual(osenv.resolve_package_name("toolchain-cc", "rpm-ostree"), "gcc-c++")
        self.assertEqual(osenv.resolve_package_name("node", "rpm-ostree"), "nodejs")

    def test_install_packages_batches_one_call(self):
        # the fix for sudo-prompt-per-package: many packages -> ONE manager invocation.
        with mock.patch.dict(os.environ, {"BOB_FORCE_OS": "linux"}), \
             mock.patch.object(osenv, "linux_package_manager", return_value="pacman"), \
             mock.patch.object(osenv.subprocess, "run", return_value=mock.Mock(returncode=0)) as run, \
             mock.patch.object(osenv.shutil, "which", return_value="/usr/bin/sudo"), \
             mock.patch.object(osenv.os, "geteuid", return_value=1000, create=True):
            osenv.install_packages(["git", "cmake", "ninja", "git"], manager="pacman")  # dupe dropped
            self.assertEqual(run.call_count, 1)
            argv = run.call_args[0][0]
            self.assertEqual(argv[:5], ["sudo", "pacman", "-S", "--needed", "--noconfirm"])
            self.assertEqual(argv[5:], ["git", "cmake", "ninja"])

    def test_install_packages_dry_run_no_exec(self):
        with mock.patch.dict(os.environ, {"BOB_FORCE_OS": "linux"}), \
             mock.patch.object(osenv.subprocess, "run") as run:
            osenv.install_packages(["git", "cmake"], manager="dnf", dry_run=True)
            run.assert_not_called()

    def test_resolve_cmd_unknown_manager_is_null(self):
        spec = osenv.resolve_package_cmd("x", os="linux", manager="brew")
        self.assertIsNone(spec["Exe"])

    def test_install_package_dry_run_returns_spec_no_exec(self):
        with mock.patch.dict(os.environ, {"BOB_FORCE_OS": "linux"}), \
             mock.patch.object(osenv, "linux_package_manager", return_value="apt"), \
             mock.patch.object(subprocess, "run") as run:
            spec = osenv.install_package("git", dry_run=True)
            self.assertEqual(spec["Exe"], "apt-get")
            run.assert_not_called()  # dry-run must never execute

    def test_python_at_least(self):
        self.assertTrue(osenv.python_at_least(sys.executable, "3.9"))
        self.assertFalse(osenv.python_at_least("/no/such/python", "3.9"))
        self.assertFalse(osenv.python_at_least(sys.executable, "99.0"))


# --- new_bob_venv (the shared venv-creator) ------------------------------------------------------

class TestNewBobVenv(unittest.TestCase):
    def test_no_python_raises(self):
        with mock.patch.object(osenv, "bob_venv_python", return_value=None):
            with self.assertRaises(RuntimeError):
                osenv.new_bob_venv("venv-x", python=None)


# --- install_prereqs (Tier 0) --------------------------------------------------------------------

class TestInstallPrereqsLinux(unittest.TestCase):
    def _run(self, cpu, mgr="apt", batch_side_effect=None, build=True, go=True, with_node=False,
             have=lambda name: True, from_source=False):
        batched = []   # toolchain, installed in ONE call via install_packages
        singles = []   # cuda/cron/docker, individual install_package

        def fake_batch(pkgs, *a, **k):
            batched.extend(pkgs)
            if batch_side_effect:
                batch_side_effect(pkgs)

        def fake_single(pkg, *a, **k):
            singles.append(pkg)

        with mock.patch.dict(os.environ, {"BOB_FORCE_OS": "linux"}), \
             mock.patch.object(install_prereqs.subprocess, "run",
                               return_value=mock.Mock(returncode=0)), \
             mock.patch.object(osenv, "linux_package_manager", return_value=mgr), \
             mock.patch.object(osenv, "install_packages", side_effect=fake_batch), \
             mock.patch.object(osenv, "install_package", side_effect=fake_single), \
             mock.patch.object(osenv, "bob_python", return_value="/usr/bin/python3"), \
             mock.patch.object(osenv, "linux_cmake3", return_value="/usr/bin/cmake"), \
             mock.patch.object(install_prereqs, "_prime_sudo"), \
             mock.patch.object(install_prereqs, "needs_build_toolchain", return_value=build), \
             mock.patch.object(install_prereqs, "needs_go", return_value=go), \
             mock.patch.object(install_prereqs, "_present", side_effect=lambda lg: have(lg)), \
             mock.patch.object(install_prereqs, "_have", return_value=True):
            rc = install_prereqs.install_prereqs(cpu=cpu, from_source=from_source, with_node=with_node)
        return rc, batched, singles

    def test_cpu_installs_toolchain_in_one_batch_skips_cuda(self):
        rc, batched, singles = self._run(cpu=True)
        self.assertEqual(rc, 0)
        # one batched toolchain install (make bundled in build-essential -> skipped); cuda skipped (--cpu).
        self.assertIn("build-essential", batched)
        self.assertIn("cmake", batched)
        self.assertIn("golang-go", batched)
        self.assertNotIn("make", batched)  # bundled on apt
        self.assertNotIn("nvidia-cuda-toolkit", batched + singles)

    def test_toolchain_failure_raises_before_setup(self):
        def boom(pkgs):
            raise RuntimeError("apt exploded")
        with self.assertRaises(RuntimeError):
            self._run(cpu=True, batch_side_effect=boom)

    def test_prebuilt_path_skips_compiler_cmake_go_and_node(self):
        # The default driver-only install compiles nothing: no compiler, cmake, ninja, Go or Node.
        rc, batched, _ = self._run(cpu=False, build=False, go=False)
        self.assertEqual(rc, 0)
        for pkg in ("build-essential", "cmake", "ninja-build", "golang-go", "nodejs", "npm"):
            self.assertNotIn(pkg, batched)
        for pkg in ("git", "curl", "python3", "python3-venv"):
            self.assertIn(pkg, batched)

    def test_prebuilt_path_never_provisions_cmake(self):
        with mock.patch.object(osenv, "linux_cmake3") as cm:
            with mock.patch.dict(os.environ, {"BOB_FORCE_OS": "linux"}), \
                 mock.patch.object(install_prereqs.subprocess, "run", return_value=mock.Mock(returncode=0)), \
                 mock.patch.object(osenv, "linux_package_manager", return_value="apt"), \
                 mock.patch.object(osenv, "install_packages"), \
                 mock.patch.object(osenv, "install_package"), \
                 mock.patch.object(osenv, "bob_python", return_value="/usr/bin/python3"), \
                 mock.patch.object(install_prereqs, "_prime_sudo"), \
                 mock.patch.object(install_prereqs, "needs_build_toolchain", return_value=False), \
                 mock.patch.object(install_prereqs, "needs_go", return_value=False), \
                 mock.patch.object(install_prereqs, "_have", return_value=True):
                install_prereqs.install_prereqs(cpu=True)
        cm.assert_not_called()

    def test_with_node_adds_node_and_npm(self):
        _, batched, _ = self._run(cpu=True, build=False, go=False, with_node=True)
        self.assertIn("nodejs", batched)
        self.assertIn("npm", batched)

    def test_logical_package_sets(self):
        base = install_prereqs.linux_logical_packages(build=False, go=False, node=False)
        self.assertEqual(base, ["git", "curl", "python", "python-pip", "python-venv"])
        full = install_prereqs.linux_logical_packages(build=True, go=True, node=True)
        for lg in ("toolchain-cc", "make", "cmake", "ninja", "go", "node", "npm"):
            self.assertIn(lg, full)

    def test_atomic_host_layers_and_returns_with_reboot_note(self):
        # rpm-ostree host, source build: toolchain layers in one transaction, CUDA is NOT layered.
        rc, batched, singles = self._run(cpu=False, mgr="rpm-ostree", have=lambda lg: False)
        self.assertEqual(rc, 0)
        self.assertIn("gcc-c++", batched)      # dnf/rpm-ostree name for toolchain-cc
        self.assertNotIn("nodejs", batched)    # Node only with --with-node
        self.assertEqual(singles, [])          # no per-pkg cuda/cron/docker on the atomic path

    def test_atomic_host_skips_what_the_image_has_and_needs_no_reboot(self):
        # Everything already present on the image: nothing is layered, so no new deployment / reboot.
        with mock.patch.object(install_prereqs, "_layer_atomic", wraps=install_prereqs._layer_atomic) as la:
            rc, batched, _ = self._run(cpu=False, mgr="rpm-ostree", build=False, go=False)
        self.assertEqual(rc, 0)
        self.assertEqual(batched, [])
        self.assertEqual(la.call_args[0][0], [])

    def test_atomic_host_layers_only_the_missing(self):
        rc, batched, _ = self._run(cpu=False, mgr="rpm-ostree", build=True, go=False,
                                   have=lambda lg: lg not in ("cmake", "ninja"))
        self.assertEqual(sorted(batched), ["cmake", "ninja-build"])


class TestPrereqNeeds(unittest.TestCase):
    def test_from_source_always_needs_toolchain_and_go(self):
        self.assertTrue(install_prereqs.needs_build_toolchain(from_source=True))
        self.assertTrue(install_prereqs.needs_go(from_source=True))

    def test_toolchain_follows_prebuilt_availability(self):
        from bob import lifecycle
        with mock.patch.object(lifecycle, "prebuilt_available", return_value=True):
            self.assertFalse(install_prereqs.needs_build_toolchain(cpu=False))
        with mock.patch.object(lifecycle, "prebuilt_available", return_value=False):
            self.assertTrue(install_prereqs.needs_build_toolchain(cpu=False))

    def test_go_follows_the_llama_swap_pin(self):
        from bob import versions
        with mock.patch.object(versions, "pinned_binary", return_value={"sha256": "ab" * 32, "url": "u"}):
            self.assertFalse(install_prereqs.needs_go())
        with mock.patch.object(versions, "pinned_binary", return_value=None):
            self.assertTrue(install_prereqs.needs_go())
        with mock.patch.object(versions, "pinned_binary", return_value={"sha256": "", "url": "u"}):
            self.assertTrue(install_prereqs.needs_go())   # an unverifiable pin is no pin

    def test_arm64_linux_has_a_llama_swap_pin_but_no_prebuilt_engine(self):
        # arm64 Linux: llama-swap ships a pinned release (no Go), the engine compiles (toolchain needed).
        from bob import lifecycle, versions
        self.assertIsNotNone(versions.pinned_binary("llama-swap", "linux-arm64"))
        self.assertNotIn("arm64", lifecycle._PREBUILT_ARCHES)
        with mock.patch.object(osenv, "os_name", return_value="linux"), \
             mock.patch.object(osenv, "normalized_cpu_arch", return_value="arm64"):
            self.assertFalse(install_prereqs.needs_go())

    def test_no_manager_raises(self):
        with mock.patch.dict(os.environ, {"BOB_FORCE_OS": "linux"}), \
             mock.patch.object(osenv, "linux_package_manager", return_value=None):
            with self.assertRaises(RuntimeError):
                install_prereqs.install_prereqs(cpu=True)


# --- kernel dispatch + helpers -------------------------------------------------------------------

class TestKernelArgvNormalize(unittest.TestCase):
    def test_pwsh_style_flags_map_to_kebab(self):
        got = kernel._normalize_argv(["setup", "-SkipModels", "-Cpu", "-Profile", "cpu"])
        self.assertEqual(got, ["setup", "--skip-models", "--cpu", "--profile", "cpu"])

    def test_leaves_unknown_tokens_intact(self):
        self.assertEqual(kernel._normalize_argv(["venv", "litellm"]), ["venv", "litellm"])


class TestOfferOnboard(unittest.TestCase):
    """Onboarding reach: a fresh interactive `bob` offers to seed a profile (not only setup).
    No-op when a profile exists, on a non-TTY, or once declined; a 'yes' runs the same onboard()."""

    def _offer(self, *, tty=True, needs=True, declined=False, answer="y"):
        calls = {}
        with mock.patch.object(kernel.sys.stdin, "isatty", return_value=tty), \
             mock.patch.object(kernel, "_needs_onboard", return_value=needs), \
             mock.patch.object(kernel, "_onboard_declined", return_value=declined), \
             mock.patch.object(kernel, "_record_onboard_declined",
                               side_effect=lambda: calls.__setitem__("declined", True)), \
             mock.patch.object(kernel, "onboard",
                               side_effect=lambda: calls.__setitem__("onboarded", True)), \
             mock.patch("builtins.input", return_value=answer):
            kernel.offer_onboard()
        return calls

    def test_yes_runs_onboard(self):
        self.assertTrue(self._offer(answer="y").get("onboarded"))

    def test_empty_answer_defaults_to_yes(self):
        self.assertTrue(self._offer(answer="").get("onboarded"))

    def test_no_records_declined_and_skips_onboard(self):
        calls = self._offer(answer="n")
        self.assertTrue(calls.get("declined"))
        self.assertNotIn("onboarded", calls)

    def test_noop_when_profile_exists(self):
        self.assertEqual(self._offer(needs=False), {})   # never prompts, never onboards

    def test_noop_when_already_declined(self):
        self.assertEqual(self._offer(declined=True), {})

    def test_noop_on_non_tty(self):
        self.assertEqual(self._offer(tty=False), {})

    def test_declined_marker_round_trips_in_user_json(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config" / "user.json"
            with mock.patch.dict(os.environ, {"BOB_USER_CONFIG": str(cfg)}):
                self.assertFalse(kernel._onboard_declined())
                kernel._record_onboard_declined()
                self.assertTrue(kernel._onboard_declined())
            self.assertTrue(cfg.exists())


class TestKernelDispatch(unittest.TestCase):
    def test_venv_subcommand_creates_named_venv(self):
        with mock.patch.object(kernel, "make_venv", return_value="/tools/venv-litellm/bin/python") as mv:
            self.assertEqual(kernel.main(["venv", "litellm"]), 0)
            mv.assert_called_once_with("litellm")

    def test_build_swap_subcommand(self):
        with mock.patch.object(kernel, "build_swap", return_value="built") as bs:
            self.assertEqual(kernel.main(["build-swap"]), 0)
            bs.assert_called_once()

    def test_prereqs_subcommand_delegates(self):
        with mock.patch.object(install_prereqs, "install_prereqs", return_value=0) as ip:
            self.assertEqual(kernel.main(["prereqs", "--cpu"]), 0)
            ip.assert_called_once_with(cpu=True, from_source=False, with_node=False)

    def test_prereqs_from_source_delegates(self):
        with mock.patch.object(install_prereqs, "install_prereqs", return_value=0) as ip:
            self.assertEqual(kernel.main(["prereqs", "--from-source"]), 0)
            ip.assert_called_once_with(cpu=False, from_source=True, with_node=False)

    def test_prereqs_with_node_delegates(self):
        with mock.patch.object(install_prereqs, "install_prereqs", return_value=0) as ip:
            self.assertEqual(kernel.main(["prereqs", "--with-node"]), 0)
            ip.assert_called_once_with(cpu=False, from_source=False, with_node=True)

    def test_aider_setup_subcommand(self):
        with mock.patch.object(kernel, "setup_aider", return_value="ok") as sa:
            self.assertEqual(kernel.main(["aider-setup", "--force"]), 0)
            sa.assert_called_once_with(force=True)

    def test_build_swap_from_source_flag(self):
        with mock.patch.object(kernel, "build_swap", return_value="built") as bs:
            self.assertEqual(kernel.main(["build-swap", "--from-source"]), 0)
            bs.assert_called_once_with(from_source=True)

    def test_setup_flags_parse(self):
        with mock.patch.object(kernel, "setup", return_value=0) as st:
            self.assertEqual(kernel.main(["setup", "--with-aider", "--with-fabric", "--skip-models"]), 0)
        kw = st.call_args.kwargs
        self.assertTrue(kw["with_aider"] and kw["with_fabric"] and kw["skip_models"])

    def test_make_venv_unknown_name_raises(self):
        with self.assertRaises(RuntimeError):
            kernel.make_venv("nope")

    def test_make_venv_maps_name_to_requirements(self):
        with mock.patch.object(osenv, "new_bob_venv", return_value="py") as nbv:
            self.assertEqual(kernel.make_venv("eval"), "py")
            nbv.assert_called_once_with("venv-eval", "eval-requirements")


class TestKernelWiring(unittest.TestCase):
    def test_wire_symlink_then_copy_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            target = d / "src.txt"
            target.write_text("hi", encoding="utf-8")
            link = d / "sub" / "link.txt"
            kernel._wire(target, link)
            self.assertTrue(link.exists())
            self.assertEqual(link.read_text(encoding="utf-8"), "hi")
            # second call: already exists -> left as-is (no raise)
            kernel._wire(target, link)

    def test_needs_onboard(self):
        # the real signal is a durable profile row, not just the config `bob` marker (onboard()
        # writes that marker even when the profile save failed — the "Bob doesn't know me" bug).
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.dict(os.environ, {"BOB_USER_CONFIG": str(Path(d) / "config" / "user.json")}), \
             mock.patch.object(kernel, "_has_profile_rows", return_value=True) as hpr:
            (Path(d) / "config").mkdir()
            self.assertTrue(kernel._needs_onboard())                       # user.json missing
            (Path(d) / "config" / "user.json").write_text('{"bob": {}}', encoding="utf-8")
            self.assertFalse(kernel._needs_onboard())                      # marked + profile present
            hpr.return_value = False
            self.assertTrue(kernel._needs_onboard())                       # marked but NEVER seeded (bug)
            (Path(d) / "config" / "user.json").write_text('{"peers": {}}', encoding="utf-8")
            self.assertTrue(kernel._needs_onboard())                       # not marked

    def test_onboard_skips_on_non_tty(self):
        # stdin is not a TTY under the test runner -> onboard must return without prompting/raising.
        with mock.patch.object(sys.stdin, "isatty", return_value=False):
            kernel.onboard()  # no exception, no input() call

    @unittest.skipIf(sys.platform == "win32",
                     "POSIX symlink install; on Windows install_cli copies and real symlinks need privilege")
    def test_install_cli_posix_symlinks_bob(self):
        with tempfile.TemporaryDirectory() as home, \
             mock.patch.dict(os.environ, {"BOB_FORCE_OS": "linux", "HOME": home}):
            kernel.install_cli()
            link = Path(home) / ".local" / "bin" / "bob"
            self.assertTrue(link.is_symlink())
            self.assertEqual(os.path.realpath(link), str((REPO / "bob").resolve()))


# --- the shell stubs + retired scripts -----------------------------------------------------------

class TestShellStubsAndRetirement(unittest.TestCase):
    def test_stubs_hand_off_to_the_python_kernel(self):
        for stub in ("setup.sh", "install_prereqs.sh"):
            text = (REPO / stub).read_text(encoding="utf-8")
            self.assertIn("python3 -m bob.kernel", text, stub)
            self.assertNotIn(".ps1", text, stub)
            self.assertNotIn("pwsh", text, stub)
        for stub in ("setup.bat", "install_prereqs.bat"):
            text = (REPO / stub).read_text(encoding="utf-8")
            self.assertIn("bob.kernel", text, stub)
            self.assertNotIn(".ps1", text, stub)

    def test_provisioning_pwsh_scripts_retired(self):
        gone = ["setup.ps1", "bootstrap.ps1", "install-prereqs.ps1", "build-llama.ps1",
                "gen-llama-swap.ps1", "fetch-models.ps1", "up.ps1", "setup-clients.ps1",
                "onboard.ps1", "diagnose.ps1", "grant-mlock.ps1", "setup-voice.ps1"]
        for name in gone:
            self.assertFalse((REPO / "scripts" / name).exists(),
                             f"{name} should be retired (kernel/capability replaces it)")


if __name__ == "__main__":
    unittest.main()
