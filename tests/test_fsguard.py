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


if __name__ == "__main__":
    unittest.main()
