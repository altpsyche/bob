"""Setup's opt-in and never-clobber contract (scripts/bob/kernel.py, scripts/tools/build.py):

  * aider and fabric are opt-in: a default setup builds neither, --with-fabric builds fabric once,
    `aider-setup` creates venv-aider from the lock, `bob aider` runs it with Bob's generated --config.
  * setup only merges Bob-owned entries: an existing ~/.continue / ~/.local/bin / ~/.config/fabric entry
    the user owns is left alone; a legacy ~/.aider.conf.yml symlink into the repo is removed.
  * a re-run never overrides a profile the user already chose.
  * config/user.json is resolved like bob_config (BOB_USER_CONFIG) and written atomically.
  * llama-swap needs no Go on the default path; whisper.cpp is gone from setup.
  * the shell installers' newest-ready-tag probe matches lifecycle.latest_ready_release_tag.
Every home/config path is a temp dir; nothing touches the real machine."""
import contextlib
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _common  # noqa: F401  (puts scripts/ + scripts/tools on sys.path, isolates env/data)
import osenv
from bob import kernel

REPO = Path(__file__).resolve().parent.parent


def _tmp(test) -> Path:
    d = Path(tempfile.mkdtemp(prefix="bob-optin-"))
    test.addCleanup(__import__("shutil").rmtree, d, True)
    return d


# --- user.json -----------------------------------------------------------------------------------

class TestUserConfig(unittest.TestCase):
    def test_path_honours_bob_user_config(self):
        with mock.patch.dict(os.environ, {"BOB_USER_CONFIG": "/x/y/user.json"}):
            self.assertEqual(kernel._user_config_path(), Path("/x/y/user.json"))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BOB_USER_CONFIG", None)
            self.assertEqual(kernel._user_config_path(), kernel.REPO / "config" / "user.json")

    def test_atomic_write_round_trips_and_leaves_no_temp(self):
        d = _tmp(self)
        p = d / "config" / "user.json"
        kernel._write_user_config({"a": 1, "bob": {"x": True}}, p)
        self.assertEqual(kernel._read_user_config(p), {"a": 1, "bob": {"x": True}})
        self.assertEqual(sorted(x.name for x in p.parent.iterdir()), ["user.json"])

    def test_failed_write_keeps_the_old_file(self):
        d = _tmp(self)
        p = d / "user.json"
        p.write_text('{"keep": 1}', encoding="utf-8")
        with mock.patch("json.dumps", side_effect=RuntimeError("boom")), self.assertRaises(RuntimeError):
            kernel._write_user_config({"new": 1}, p)
        self.assertEqual(json.loads(p.read_text(encoding="utf-8")), {"keep": 1})
        self.assertEqual([x.name for x in d.iterdir()], ["user.json"])

    def test_bad_overlay_reads_as_empty(self):
        d = _tmp(self)
        p = d / "user.json"
        p.write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(kernel._read_user_config(p), {})
        p.write_text("{nope", encoding="utf-8")
        self.assertEqual(kernel._read_user_config(p), {})

    def test_reset_strips_marker_via_the_env_path(self):
        d = _tmp(self)
        cfg = d / "user.json"
        cfg.write_text(json.dumps({"bob": {}, "port": 1}), encoding="utf-8")
        data = d / "data"
        data.mkdir()
        with mock.patch.dict(os.environ, {"BOB_USER_CONFIG": str(cfg)}):
            kernel.reset_all_data(data_dir=data)
        self.assertEqual(json.loads(cfg.read_text(encoding="utf-8")), {"port": 1})


# --- profile selection ---------------------------------------------------------------------------

class TestProfileSelection(unittest.TestCase):
    def _select(self, chosen: bool, sug="24gb", active="16gb", profile=None):
        import bob_models
        import models as models_mod
        with mock.patch.object(kernel, "_profile_chosen", return_value=chosen), \
             mock.patch.object(osenv, "gpu_vram_gb", return_value=24), \
             mock.patch.object(models_mod, "suggested_profile", return_value=sug), \
             mock.patch.object(bob_models, "load_models_config", return_value={"activeProfile": active}), \
             mock.patch.object(bob_models, "set_active_profile") as sap:
            kernel._select_profile(profile)
        return sap

    def test_rerun_keeps_a_chosen_profile(self):
        self._select(chosen=True).assert_not_called()

    def test_first_run_auto_selects(self):
        self._select(chosen=False).assert_called_once_with("24gb")

    def test_explicit_profile_wins(self):
        self._select(chosen=True, profile="8gb").assert_called_once_with("8gb")

    def test_profile_chosen_signals(self):
        import bob_models
        d = _tmp(self)
        apf = d / "active-profile.json"
        cfg = d / "user.json"
        with mock.patch.object(bob_models, "_active_profile_file", return_value=apf), \
             mock.patch.dict(os.environ, {"BOB_USER_CONFIG": str(cfg)}):
            os.environ.pop("BOB_PROFILE", None)
            self.assertFalse(kernel._profile_chosen())
            cfg.write_text('{"activeProfile": "12gb"}', encoding="utf-8")
            self.assertTrue(kernel._profile_chosen())
            cfg.unlink()
            apf.write_text('{"activeProfile": "12gb"}', encoding="utf-8")
            self.assertTrue(kernel._profile_chosen())
            apf.unlink()
            with mock.patch.dict(os.environ, {"BOB_PROFILE": "cpu"}):
                self.assertTrue(kernel._profile_chosen())


# --- venvs ---------------------------------------------------------------------------------------

class TestVenvTable(unittest.TestCase):
    def test_one_table(self):
        self.assertFalse(hasattr(kernel, "_VENV_REQ"))
        self.assertEqual(kernel.VENVS["aider"], ("venv-aider", "aider-requirements"))
        src = (REPO / "scripts" / "bob" / "kernel.py").read_text(encoding="utf-8")
        # every venv/requirements pair is spelled once, in VENVS
        self.assertEqual(src.count('"aider-requirements"'), 1)
        self.assertEqual(src.count('"litellm-requirements"'), 1)


# --- bootstrap / setup ---------------------------------------------------------------------------

class _SetupHarness:
    """Run kernel.setup / kernel.bootstrap with every heavy capability mocked."""

    @contextlib.contextmanager
    def patched(self, have=lambda n: True):
        import build
        import generate
        import health
        import provision
        from bob import lifecycle
        calls = {}
        with contextlib.ExitStack() as es:
            ent = es.enter_context
            calls["bootstrap"] = ent(mock.patch.object(kernel, "bootstrap"))
            calls["setup_clients"] = ent(mock.patch.object(kernel, "setup_clients"))
            calls["setup_aider"] = ent(mock.patch.object(kernel, "setup_aider", return_value="aider ok"))
            calls["install_cli"] = ent(mock.patch.object(kernel, "install_cli"))
            calls["onboard"] = ent(mock.patch.object(kernel, "_needs_onboard", return_value=False))
            calls["have"] = ent(mock.patch.object(kernel, "_have", side_effect=have))
            calls["setup_fabric"] = ent(mock.patch.object(build, "setup_fabric", return_value="fabric ok"))
            ent(mock.patch.object(build, "configure"))
            ent(mock.patch.object(health, "diagnose", return_value="diag"))
            ent(mock.patch.object(provision, "setup_voice", return_value="voice"))
            ent(mock.patch.object(provision, "configure"))
            ent(mock.patch.object(lifecycle, "prebuilt_available", return_value=True))
            calls["cmake3"] = ent(mock.patch.object(osenv, "linux_cmake3", return_value="/c/cmake"))
            ent(mock.patch.object(osenv, "mlock_status", return_value={"granted": True, "detail": ""}))
            ent(mock.patch.object(osenv, "os_name", return_value="linux"))
            ent(mock.patch.object(generate, "gen_all"))
            yield calls


class TestSetupOptIn(_SetupHarness, unittest.TestCase):
    def test_default_setup_builds_neither_fabric_nor_aider(self):
        with self.patched() as c:
            self.assertEqual(kernel.setup(skip_voice=True), 0)
        c["setup_fabric"].assert_not_called()
        c["setup_aider"].assert_not_called()
        self.assertFalse(c["bootstrap"].call_args.kwargs["with_aider"])

    def test_with_fabric_builds_it_exactly_once(self):
        with self.patched() as c:
            kernel.setup(skip_voice=True, with_fabric=True)
        c["setup_fabric"].assert_called_once()

    def test_with_aider_sets_it_up(self):
        with self.patched() as c:
            kernel.setup(skip_voice=True, with_aider=True)
        c["setup_aider"].assert_called_once()
        self.assertTrue(c["bootstrap"].call_args.kwargs["with_aider"])

    def test_missing_node_and_go_do_not_fail_setup(self):
        with self.patched(have=lambda n: n not in ("node", "go")):
            self.assertEqual(kernel.setup(skip_voice=True), 0)

    def test_prebuilt_path_skips_cmake(self):
        with self.patched() as c:
            kernel.setup(skip_voice=True)
        c["cmake3"].assert_not_called()


class TestBootstrapSwap(unittest.TestCase):
    """llama-swap installs without a Go gate (pinned release by default), and a failure is reported, not
    swallowed; the LiteLLM key is ensured before any config is generated."""

    def _bootstrap(self, swap_effect=None, from_source=False):
        import bob_core
        import build
        import generate
        from bob import lifecycle
        order = []
        with mock.patch.object(kernel, "_select_profile"), \
             mock.patch.object(kernel, "_have", side_effect=lambda n: n != "go"), \
             mock.patch.object(osenv, "bob_venv_python", return_value=None), \
             mock.patch.object(kernel.subprocess, "run", return_value=mock.Mock(returncode=0)), \
             mock.patch.object(lifecycle, "ensure_engine",
                               return_value={"tier": "gpu", "reason": "r", "detail": "d"}), \
             mock.patch.object(build, "configure"), \
             mock.patch.object(build, "build_llama_swap", side_effect=swap_effect,
                               return_value="swap ok") as swap, \
             mock.patch.object(bob_core, "_litellm_key",
                               side_effect=lambda c: order.append("key") or "k"), \
             mock.patch.object(generate, "configure"), \
             mock.patch.object(generate, "gen_all", side_effect=lambda: order.append("gen")):
            kernel.bootstrap(skip_models=True, from_source=from_source)
        return swap, order

    def test_swap_runs_without_go(self):
        swap, _ = self._bootstrap()
        swap.assert_called_once_with(from_source=False)

    def test_swap_from_source_is_forwarded(self):
        swap, _ = self._bootstrap(from_source=True)
        swap.assert_called_once_with(from_source=True)

    def test_swap_failure_is_reported_and_setup_continues(self):
        err = __import__("io").StringIO()
        with mock.patch.object(sys, "stderr", err):
            _, order = self._bootstrap(swap_effect=RuntimeError("no go"))
        self.assertIn("llama-swap install failed: no go", err.getvalue())
        self.assertIn("gen", order)

    def test_key_is_ensured_before_configs_are_generated(self):
        _, order = self._bootstrap()
        self.assertLess(order.index("key"), order.index("gen"))


# --- aider ---------------------------------------------------------------------------------------

class TestAider(unittest.TestCase):
    def test_setup_aider_creates_the_venv_from_the_lock(self):
        import generate
        with mock.patch.object(osenv, "new_bob_venv", return_value="/v/python") as nbv, \
             mock.patch.object(generate, "configure"), \
             mock.patch.object(generate, "gen_aider", create=True) as ga, \
             mock.patch.object(kernel, "_remove_legacy_aider_link"):
            out = kernel.setup_aider()
        nbv.assert_called_once_with("venv-aider", "aider-requirements", force=False)
        ga.assert_called_once()
        self.assertIn("bob aider", out)

    def test_run_aider_passes_config_and_env_key(self):
        d = _tmp(self)
        exe = d / "aider"
        exe.write_text("", encoding="utf-8")
        conf = d / ".aider.conf.yml"
        conf.write_text("model: x\n", encoding="utf-8")
        import bob_core
        with mock.patch.object(osenv, "venv_exe", return_value=exe), \
             mock.patch.object(kernel, "_aider_conf", return_value=conf), \
             mock.patch.object(bob_core, "_litellm_key", return_value="sk-secret"), \
             mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch.object(kernel.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            os.environ.pop("AIDER_OPENAI_API_KEY", None)
            self.assertEqual(kernel.run_aider(["--yes"]), 0)
        argv = run.call_args[0][0]
        self.assertEqual(argv, [str(exe), "--config", str(conf), "--yes"])
        self.assertNotIn("sk-secret", " ".join(argv))              # never on the command line
        self.assertEqual(run.call_args.kwargs["env"]["AIDER_OPENAI_API_KEY"], "sk-secret")

    def test_run_aider_respects_an_explicit_config(self):
        d = _tmp(self)
        exe = d / "aider"
        exe.write_text("", encoding="utf-8")
        with mock.patch.object(osenv, "venv_exe", return_value=exe), \
             mock.patch.dict(os.environ, {"AIDER_OPENAI_API_KEY": "mine"}), \
             mock.patch.object(kernel.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            kernel.run_aider(["--config", "mine.yml"])
        self.assertEqual(run.call_args[0][0], [str(exe), "--config", "mine.yml"])

    def test_run_aider_not_installed(self):
        with mock.patch.object(osenv, "venv_exe", return_value=Path("/nonexistent/aider")):
            self.assertEqual(kernel.run_aider([]), 1)

    @unittest.skipIf(sys.platform == "win32", "POSIX symlinks")
    def test_legacy_symlink_into_repo_is_removed_user_file_kept(self):
        home = _tmp(self)
        link = home / ".aider.conf.yml"
        link.symlink_to(kernel.REPO / "config" / "aider" / ".aider.conf.yml")
        kernel._remove_legacy_aider_link(home)
        self.assertFalse(link.is_symlink() or link.exists())
        # someone else's symlink, and a real file, are never touched
        other = home / "mine.yml"
        other.write_text("x", encoding="utf-8")
        link.symlink_to(other)
        kernel._remove_legacy_aider_link(home)
        self.assertTrue(link.is_symlink())
        link.unlink()
        link.write_text("user", encoding="utf-8")
        kernel._remove_legacy_aider_link(home)
        self.assertEqual(link.read_text(encoding="utf-8"), "user")


# --- never clobber -------------------------------------------------------------------------------

class TestWire(unittest.TestCase):
    def test_user_file_left_bob_copy_refreshed(self):
        d = _tmp(self)
        target = d / "config.yaml"
        target.write_text("# GENERATED - DO NOT EDIT.\nkey: new\n", encoding="utf-8")
        dest = d / "home" / "config.yaml"
        dest.parent.mkdir()
        dest.write_text("# my own config\n", encoding="utf-8")
        kernel._wire(target, dest)
        self.assertEqual(dest.read_text(encoding="utf-8"), "# my own config\n")
        dest.write_text("# GENERATED - DO NOT EDIT.\nkey: old\n", encoding="utf-8")
        kernel._wire(target, dest)
        self.assertIn("key: new", dest.read_text(encoding="utf-8"))

    def test_setup_clients_wires_no_aider(self):
        import generate
        home = _tmp(self)
        with mock.patch.object(Path, "home", return_value=home), \
             mock.patch.object(generate, "configure"), \
             mock.patch.object(generate, "gen_continue"), \
             mock.patch.object(generate, "gen_dsh"), \
             mock.patch.object(generate, "install_dsh", return_value="dsh skipped"), \
             mock.patch.object(kernel, "_wire") as wire:
            kernel.setup_clients()
        linked = [c.args[1] for c in wire.call_args_list]
        self.assertEqual(linked, [home / ".continue" / "config.yaml"])


@unittest.skipIf(sys.platform == "win32", "POSIX symlink install")
class TestInstallCli(unittest.TestCase):
    def _install(self, home, fabric_exe):
        with mock.patch.dict(os.environ, {"BOB_FORCE_OS": "linux", "HOME": str(home)}), \
             mock.patch.object(Path, "home", return_value=home), \
             mock.patch.object(osenv, "bin_exe", return_value=fabric_exe):
            kernel.install_cli()

    def test_user_fabric_binary_is_kept(self):
        home = _tmp(self)
        fabric_exe = _tmp(self) / "bin" / "fabric"
        fabric_exe.parent.mkdir()
        fabric_exe.write_text("", encoding="utf-8")
        mine = home / ".local" / "bin" / "fabric"
        mine.parent.mkdir(parents=True)
        mine.write_text("#!/bin/sh\necho user fabric\n", encoding="utf-8")
        self._install(home, fabric_exe)
        self.assertFalse(mine.is_symlink())
        self.assertIn("user fabric", mine.read_text(encoding="utf-8"))

    def test_user_symlinked_fabric_is_kept(self):
        home = _tmp(self)
        fabric_exe = _tmp(self) / "bin" / "fabric"
        fabric_exe.parent.mkdir()
        fabric_exe.write_text("", encoding="utf-8")
        elsewhere = _tmp(self) / "go" / "fabric"
        elsewhere.parent.mkdir()
        elsewhere.write_text("", encoding="utf-8")
        link = home / ".local" / "bin" / "fabric"
        link.parent.mkdir(parents=True)
        link.symlink_to(elsewhere)
        self._install(home, fabric_exe)
        self.assertEqual(os.readlink(link), str(elsewhere))

    def test_bob_made_fabric_link_is_repointed(self):
        home = _tmp(self)
        new = _tmp(self) / "bin" / "fabric"
        new.parent.mkdir()
        new.write_text("", encoding="utf-8")
        old_clone = _tmp(self)
        (old_clone / "scripts" / "bob").mkdir(parents=True)
        (old_clone / "bin").mkdir()
        (old_clone / "bin" / "fabric").write_text("", encoding="utf-8")
        link = home / ".local" / "bin" / "fabric"
        link.parent.mkdir(parents=True)
        link.symlink_to(old_clone / "bin" / "fabric")       # an older Bob checkout's link
        self._install(home, new)
        self.assertEqual(os.readlink(link), str(new))
        link.unlink()
        link.symlink_to("/moved/clone/bin/fabric")           # dangling: the clone was moved
        self._install(home, new)
        self.assertEqual(os.readlink(link), str(new))

    def test_user_bob_file_is_kept(self):
        home = _tmp(self)
        mine = home / ".local" / "bin" / "bob"
        mine.parent.mkdir(parents=True)
        mine.write_text("#!/bin/sh\necho another bob\n", encoding="utf-8")
        self._install(home, Path("/nonexistent/bin/fabric"))
        self.assertFalse(mine.is_symlink())


# --- fabric .env merge ---------------------------------------------------------------------------

class TestFabricEnvMerge(unittest.TestCase):
    def setUp(self):
        import build
        self.build = build
        self.env = _tmp(self) / "fabric" / ".env"

    def test_fresh_file(self):
        self.build.merge_fabric_env(self.env, 8081, "sk-k")
        text = self.env.read_text(encoding="utf-8")
        self.assertIn("LITELLM_API_KEY=sk-k", text)
        self.assertIn("LITELLM_API_BASE_URL=http://localhost:8081/v1", text)
        self.assertIn("DEFAULT_VENDOR=LiteLLM", text)
        if os.name == "posix":
            self.assertEqual(self.env.stat().st_mode & 0o777, 0o600)

    def test_user_vendor_keys_and_default_are_preserved(self):
        self.env.parent.mkdir(parents=True)
        self.env.write_text("# mine\nOPENAI_API_KEY=sk-real\nANTHROPIC_API_KEY=a\n"
                            "DEFAULT_VENDOR=Anthropic\nDEFAULT_MODEL=claude\n", encoding="utf-8")
        self.build.merge_fabric_env(self.env, 8081, "sk-k")
        text = self.env.read_text(encoding="utf-8")
        for keep in ("# mine", "OPENAI_API_KEY=sk-real", "ANTHROPIC_API_KEY=a",
                     "DEFAULT_VENDOR=Anthropic", "DEFAULT_MODEL=claude"):
            self.assertIn(keep, text)
        self.assertIn("LITELLM_API_KEY=sk-k", text)

    def test_legacy_bob_openai_block_is_migrated(self):
        self.env.parent.mkdir(parents=True)
        self.env.write_text("OPENAI_API_KEY=sk-local\nOPENAI_API_BASE_URL=http://localhost:8081/v1\n"
                            "DEFAULT_VENDOR=OpenAI\nDEFAULT_MODEL=coder\nGROQ_API_KEY=g\n", encoding="utf-8")
        self.build.merge_fabric_env(self.env, 8081, "sk-k")
        text = self.env.read_text(encoding="utf-8")
        self.assertNotIn("OPENAI_API_KEY", text)
        self.assertIn("GROQ_API_KEY=g", text)
        self.assertIn("DEFAULT_VENDOR=LiteLLM", text)

    def test_rerun_is_a_no_op(self):
        self.build.merge_fabric_env(self.env, 8081, "sk-k")
        self.assertEqual(self.build.merge_fabric_env(self.env, 8081, "sk-k"), [])


# --- whisper.cpp is gone, the registry is honest -------------------------------------------------

class TestWhisperCppDropped(unittest.TestCase):
    _SCAN = ["scripts", "install", "setup.sh", "setup.bat", "install_prereqs.sh", "install_prereqs.bat",
             ".gitmodules", ".github", "config"]

    def test_no_reference_to_the_whisper_cpp_submodule(self):
        hits = []
        for rel in self._SCAN:
            root = REPO / rel
            files = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]
            for f in files:
                if "__pycache__" in f.parts or f.suffix in (".pyc", ".gguf"):
                    continue
                try:
                    text = f.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                if "external/whisper.cpp" in text or "build_whisper" in text or "SRC_WHISPER" in text:
                    hits.append(str(f.relative_to(REPO)))
        self.assertEqual(hits, [])

    def test_setup_and_catalog_never_promise_a_whisper_cpp_build(self):
        from bob import catalog, registry
        text = catalog.render_commands() + (REPO / "scripts" / "bob" / "kernel.py").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"(?i)build(s)?\s+whisper")
        self.assertNotIn("whisper-server", " ".join(c["summary"] for c in registry.COMMANDS))

    def test_build_help_names_from_source(self):
        from bob import registry
        self.assertIn("--from-source", registry.by_name()["build"]["args"])

    def test_opt_in_verbs_are_registered(self):
        from bob import registry
        names = registry.by_name()
        for verb in ("aider", "aider-setup", "fabric-setup"):
            self.assertIn(verb, names)


# --- installers agree with lifecycle on the newest ready tag -------------------------------------

class TestReadyTagProbeParity(unittest.TestCase):
    def test_shell_probes_match_lifecycle(self):
        from bob import lifecycle
        lc = (REPO / "scripts" / "bob" / "lifecycle.py").read_text(encoding="utf-8")
        sh = (REPO / "install" / "install.sh").read_text(encoding="utf-8")
        ps = (REPO / "install" / "install.ps1").read_text(encoding="utf-8")
        walk = lifecycle._MAX_TAG_WALK
        self.assertIn('releases/download/{tag}/engines.json', lc)
        self.assertIn('"tag", "--list", "v*", "--sort=-v:refname"', lc)
        self.assertIn("releases/download/$t/engines.json", sh)
        self.assertIn("releases/download/$t/engines.json", ps)
        self.assertIn(f"tag --list 'v*' --sort=-v:refname | head -n{walk}", sh)
        self.assertIn(f"tag --list 'v*' --sort=-v:refname | Select-Object -First {walk}", ps)
        # readiness = the manifest has a real row (a top-level key not starting with '_'), as _manifest_rows
        self.assertIn('not k.startswith("_")', sh)
        self.assertIn("-not $_.StartsWith('_')", ps)
        self.assertIn('not k.startswith("_")', lc)

    def test_no_stale_release_claims(self):
        sh = (REPO / "install" / "install.sh").read_text(encoding="utf-8")
        self.assertNotRegex(sh, r"ship in 1\.1|out of scope for 1\.1|arrives in Bob 2\.0")

    def test_sh_readiness_check_without_python(self):
        # The grep fallback: an '_'-only manifest is not ready, one real row is.
        pat = re.compile(r'"[^_"][^"]*"[\s]*:[\s]*\{')
        template = (REPO / "config" / "engines.json").read_text(encoding="utf-8")
        self.assertIsNone(pat.search(template))
        self.assertIsNotNone(pat.search('{"_c": 1, "llama-server-linux-x86_64-gpu": {"url": "u"}}'))


class TestGitignore(unittest.TestCase):
    def test_no_powershell_era_comments_and_aider_generated(self):
        text = (REPO / ".gitignore").read_text(encoding="utf-8")
        for stale in ("build-*.ps1", "Get-LinuxCmake3", "fetch-models.ps1", "models.psd1", "setup-voice.ps1",
                      "setup-docker.ps1", "user.psd1"):
            self.assertNotIn(stale, text)
        self.assertIn("/config/aider/", text)


if __name__ == "__main__":
    unittest.main()
