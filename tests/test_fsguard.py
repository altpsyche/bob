"""bob_fsguard: the shared path allowlist + secrets denylist. Pins the pure guard helpers that the
file tools and the code index all route through, including OS-aware home-credential denial."""
import shutil
import tempfile
import unittest
from pathlib import Path

import _common  # noqa: F401 — puts scripts/ on sys.path
import bob_fsguard


class TestAbsPath(unittest.TestCase):
    def test_absolute_passes_through(self):
        p = bob_fsguard.abs_path("/etc/hosts", [Path("/repo")])
        self.assertEqual(p, Path("/etc/hosts"))

    def test_relative_resolves_against_first_allowed_root(self):
        p = bob_fsguard.abs_path("sub/f.txt", [Path("/repo"), Path("/other")])
        self.assertEqual(p, Path("/repo/sub/f.txt"))

    def test_relative_falls_back_to_cwd_when_no_roots(self):
        p = bob_fsguard.abs_path("f.txt", [])
        self.assertEqual(p, Path.cwd() / "f.txt")


class TestIsAllowed(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bob-fsg-"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_inside_root_allowed(self):
        (self.dir / "f.txt").write_text("x", encoding="utf-8")
        self.assertTrue(bob_fsguard.is_allowed(self.dir / "f.txt", [self.dir]))

    def test_outside_root_denied(self):
        other = Path(tempfile.mkdtemp(prefix="bob-fsg-other-"))
        try:
            self.assertFalse(bob_fsguard.is_allowed(other / "f.txt", [self.dir]))
        finally:
            shutil.rmtree(other, ignore_errors=True)


class TestIsDeniedSecret(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bob-fsg-sec-"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _touch(self, rel):
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
        return p

    def test_normal_file_not_denied(self):
        self.assertFalse(bob_fsguard.is_denied_secret(self._touch("normal.txt")))

    def test_denies_secret_basenames(self):
        for name in ("config.json", "secrets.json"):
            self.assertTrue(bob_fsguard.is_denied_secret(self._touch(name)), name)

    def test_denies_secret_suffixes(self):
        for name in ("user.psd1", "sessions.db"):
            self.assertTrue(bob_fsguard.is_denied_secret(self._touch(name)), name)

    def test_denies_dotenv(self):
        self.assertTrue(bob_fsguard.is_denied_secret(self._touch(".env")))
        self.assertTrue(bob_fsguard.is_denied_secret(self._touch(".env.local")))

    def test_denies_logs_segment(self):
        self.assertTrue(bob_fsguard.is_denied_secret(self._touch("logs/a.log")))

    def test_denies_home_credential_dir_with_injected_home(self):
        home = Path(tempfile.mkdtemp(prefix="bob-fsg-home-"))
        try:
            key = home / ".ssh" / "id_rsa"
            key.parent.mkdir(parents=True)
            key.write_text("PRIVATE", encoding="utf-8")
            self.assertTrue(bob_fsguard.is_denied_secret(key, home=home))
            # A different home does not deny it (proves home is honored, not hard-coded).
            self.assertFalse(bob_fsguard.is_denied_secret(key, home=self.dir))
        finally:
            shutil.rmtree(home, ignore_errors=True)


class TestGeneratedSecrets(unittest.TestCase):
    """Files Bob generates inside the repo that hold a key: the file tools and the code index refuse them."""

    def test_every_key_bearing_config_is_denied(self):
        for rel in bob_fsguard.KEY_BEARING:
            self.assertTrue(bob_fsguard.is_denied_secret(bob_fsguard.REPO / rel), rel)

    def test_n8n_config_and_dsh_credentials_are_denied(self):
        repo = bob_fsguard.REPO
        self.assertTrue(bob_fsguard.is_denied_secret(repo / "tools" / "n8n-data" / ".n8n" / "config"))
        self.assertTrue(bob_fsguard.is_denied_secret(repo / "tools" / "n8n-data" / "config"))
        self.assertTrue(bob_fsguard.is_denied_secret(repo / "scripts" / ".credentials.yaml"))

    def test_ordinary_repo_files_stay_readable(self):
        repo = bob_fsguard.REPO
        for rel in ("config/models.json", "config/llama-swap.yaml", "scripts/bob_core.py",
                    "tools/n8n-workflows/config"):
            self.assertFalse(bob_fsguard.is_denied_secret(repo / rel), rel)

    def test_key_bearing_match_ignores_case(self):
        # A case-insensitive filesystem (APFS, NTFS) opens CONFIG/Continue/config.yaml as the real file.
        repo = bob_fsguard.REPO
        for rel in ("CONFIG/Continue/config.yaml", "config/DSH/Settings.yaml", "Config/LiteLLM.YAML"):
            self.assertTrue(bob_fsguard.is_denied_secret(repo / rel), rel)
        self.assertTrue(bob_fsguard.is_denied_secret(repo / "Tools" / "N8N-Data" / ".n8n" / "Config"))

    def test_the_generators_share_the_one_list(self):
        import generate
        self.assertIs(generate._KEY_BEARING, bob_fsguard.KEY_BEARING)

    def test_file_read_refuses_a_key_bearing_config(self):
        import file as file_tool
        saved = (file_tool._allowed_read, file_tool._allowed_write)
        self.addCleanup(lambda: (setattr(file_tool, "_allowed_read", saved[0]),
                                 setattr(file_tool, "_allowed_write", saved[1])))
        file_tool.configure({"agent": {"allowedReadPaths": [str(bob_fsguard.REPO)]}})
        out = file_tool.DISPATCH["file_read"]("config/continue/config.yaml")
        self.assertIn("sensitive file", out)


if __name__ == "__main__":
    unittest.main()


class TestWriteGuard(unittest.TestCase):
    """Agent/MCP writes may not change Bob's own config or code; reads of them stay allowed."""

    R = bob_fsguard.REPO

    def test_bob_config_and_code_are_write_denied(self):
        for rel in ("config/user.json", "config/defaults.json", "config/models.json",
                    "scripts/bob_core.py", "scripts/tools/new_tool.py", "scripts/bob/kernel.py",
                    "plugins/play/tool.py", "plugins/newplug/invoke.py", "plugins/play/sub/helper.py",
                    "CONFIG/User.json"):
            self.assertTrue(bob_fsguard.is_denied_write(self.R / rel), rel)
            self.assertTrue(bob_fsguard.is_protected_code(self.R / rel), rel)

    def test_other_repo_files_stay_writable_and_code_stays_readable(self):
        for rel in ("docs/notes.md", "plugins/play/description.txt", "plugins/play/sub/notes.md",
                    "tests/test_x.py", "config/other.json"):
            self.assertFalse(bob_fsguard.is_denied_write(self.R / rel), rel)
        self.assertFalse(bob_fsguard.is_denied_secret(self.R / "scripts" / "bob_core.py"))

    def test_secrets_are_write_denied_too(self):
        self.assertTrue(bob_fsguard.is_denied_write(self.R / "config" / "litellm.yaml"))

    def test_file_write_and_file_edit_refuse_protected_paths(self):
        import bob_edit
        sys_path_tools = str(self.R / "scripts" / "tools")
        import sys
        if sys_path_tools not in sys.path:
            sys.path.insert(0, sys_path_tools)
        import file as file_tool
        file_tool.configure({"agent": {"allowedReadPaths": [str(self.R)],
                                       "allowedWritePaths": [str(self.R)]}})
        target = self.R / "config" / "user.json"
        before = target.read_bytes() if target.exists() else None
        out = file_tool._file_write(str(target), "{}")
        self.assertIn("attended, approved", out)
        self.assertEqual(target.read_bytes() if target.exists() else None, before)
        res = bob_edit.apply_edits({"path": "scripts/bob_core.py", "search": "x", "replace": "y"},
                                   [self.R])
        self.assertFalse(res.ok)
        self.assertIn("attended, approved", res.rejections[0]["reason"])

    def _in_run(self, approve, unattended_allow=None):
        from types import SimpleNamespace
        import tool_registry
        ctx = SimpleNamespace(approve=approve, unattended_allow=unattended_allow)
        tok = tool_registry._RUN_CONTEXT.set(ctx)
        self.addCleanup(tool_registry._RUN_CONTEXT.reset, tok)

    def test_only_an_attended_run_may_write_protected_paths(self):
        target = self.R / "config" / "user.json"
        self.assertTrue(bob_fsguard.is_denied_write(target))          # outside any run
        self._in_run(approve=None)
        self.assertTrue(bob_fsguard.is_denied_write(target))          # nobody to ask
        self._in_run(approve=lambda a: True, unattended_allow=frozenset({"file_write"}))
        self.assertTrue(bob_fsguard.is_denied_write(target))          # MCP / unattended
        self._in_run(approve=lambda a: True)
        self.assertFalse(bob_fsguard.is_denied_write(target))         # attended: the gate asked
        self.assertTrue(bob_fsguard.is_denied_write(self.R / "config" / "litellm.yaml"))  # secrets never

    def test_attended_file_write_reaches_a_protected_path(self):
        import shutil
        import sys
        sys_path_tools = str(self.R / "scripts" / "tools")
        if sys_path_tools not in sys.path:
            sys.path.insert(0, sys_path_tools)
        import file as file_tool
        probe_dir = self.R / "plugins" / "_bob_guard_probe"
        self.addCleanup(shutil.rmtree, probe_dir, True)
        file_tool.configure({"agent": {"allowedReadPaths": [str(self.R)],
                                       "allowedWritePaths": [str(self.R)]}})
        self._in_run(approve=lambda a: True)
        out = file_tool._file_write(str(probe_dir / "helper.py"), "x = 1\n")
        self.assertNotIn("Access denied", out)
        self.assertEqual((probe_dir / "helper.py").read_text(encoding="utf-8"), "x = 1\n")
