"""file_read/file_write path allowlist + secrets denylist. Backs the SECURITY.md claim that
config.json (litellm key + api tokens), *.psd1, *.db, logs/, and .env* are unreadable even when
they sit inside an allowedReadPaths root (which defaults to the repo root)."""
import shutil
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import _common  # noqa: F401 — puts scripts/tools on sys.path
import file  # scripts/tools/file.py


class TestFileTool(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bob-file-"))
        (self.dir / "normal.txt").write_text("hello", encoding="utf-8")
        (self.dir / "config.json").write_text('{"litellmKey":"SUPERSECRET"}', encoding="utf-8")
        (self.dir / "user.psd1").write_text("@{}", encoding="utf-8")
        (self.dir / "sessions.db").write_text("dbbytes", encoding="utf-8")
        (self.dir / ".env").write_text("TOKEN=abc", encoding="utf-8")
        (self.dir / "logs").mkdir()
        (self.dir / "logs" / "a.log").write_text("logline", encoding="utf-8")
        (self.dir / "secrets.json").write_text('{"litellmKey":"SUPERSECRET"}', encoding="utf-8")
        file.configure({"agent": {"allowedReadPaths": [str(self.dir)]}})

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_reads_normal_file(self):
        self.assertEqual(file._file_read(str(self.dir / "normal.txt")), "hello")

    def test_denies_config_json_and_hides_secret(self):
        out = file._file_read(str(self.dir / "config.json"))
        self.assertIn("sensitive", out)
        self.assertNotIn("SUPERSECRET", out)  # the secret never leaks

    def test_denies_psd1(self):
        self.assertIn("sensitive", file._file_read(str(self.dir / "user.psd1")))

    def test_denies_db(self):
        self.assertIn("sensitive", file._file_read(str(self.dir / "sessions.db")))

    def test_denies_env(self):
        self.assertIn("sensitive", file._file_read(str(self.dir / ".env")))

    def test_denies_logs_dir(self):
        self.assertIn("sensitive", file._file_read(str(self.dir / "logs" / "a.log")))

    def test_denies_secrets_json(self):
        # the resolved secrets file must never be readable, secret never leaks.
        out = file._file_read(str(self.dir / "secrets.json"))
        self.assertIn("sensitive", out)
        self.assertNotIn("SUPERSECRET", out)

    def test_denies_home_ssh_key(self):
        # OS-aware denial of ~/.ssh even when it's inside an allowedReadPaths root.
        import osenv  # noqa: F811
        home = Path(tempfile.mkdtemp(prefix="bob-home-"))
        try:
            (home / ".ssh").mkdir()
            (home / ".ssh" / "id_rsa").write_text("PRIVATEKEY", encoding="utf-8")
            file.configure({"agent": {"allowedReadPaths": [str(home)]}})
            with unittest.mock.patch.object(file, "_home", lambda: home):
                out = file._file_read(str(home / ".ssh" / "id_rsa"))
            self.assertIn("sensitive", out)
            self.assertNotIn("PRIVATEKEY", out)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_denies_outside_allowed_root(self):
        other = Path(tempfile.mkdtemp(prefix="bob-other-"))
        try:
            (other / "f.txt").write_text("x", encoding="utf-8")
            self.assertIn("Access denied", file._file_read(str(other / "f.txt")))
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_write_refuses_secret_even_when_allowed(self):
        file.configure({"agent": {"allowedWritePaths": [str(self.dir)]}})
        out = file._file_write(str(self.dir / "config.json"), "{}")
        self.assertIn("sensitive", out)

    def test_list_lists_directory_entries(self):
        (self.dir / "sub").mkdir()
        out = file._file_list(str(self.dir))
        self.assertIn("normal.txt", out)
        self.assertIn("sub/", out)                 # directories flagged with a trailing slash

    def test_list_relative_resolves_to_allowed_root(self):
        # '.' must resolve to the allowed root (the workspace), NOT the process cwd — the fix for the
        # confusing approve-then-"Access denied ./" case.
        out = file._file_list(".")
        self.assertIn("normal.txt", out)
        self.assertNotIn("Access denied", out)

    def test_list_on_a_file_is_rejected(self):
        out = file._file_list(str(self.dir / "normal.txt"))
        self.assertIn("Not a directory", out)

    def test_list_denies_outside_allowed_root(self):
        other = Path(tempfile.mkdtemp(prefix="bob-other-"))
        try:
            self.assertIn("Access denied", file._file_list(str(other)))
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_read_relative_resolves_to_allowed_root(self):
        # relative file paths now resolve under the workspace root too
        self.assertEqual(file._file_read("normal.txt"), "hello")


if __name__ == "__main__":
    unittest.main()
