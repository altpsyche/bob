"""git_* is restricted to allow-listed repositories. Backs the SECURITY.md claim that the
agent can't read the status/log/diff of an arbitrary git repo on disk (info disclosure)."""
import shutil
import tempfile
import unittest

import _common  # noqa: F401 — puts scripts/tools on sys.path
import git  # scripts/tools/git.py


class TestGitTool(unittest.TestCase):
    def setUp(self):
        git.configure({"agent": {}})  # only the Bob repo root allowed

    def test_default_repo_allowed(self):
        # The Bob repo is a git repo; the point is it's NOT refused by the allow-list.
        self.assertNotIn("Access denied", git._git_status())

    def test_outside_repo_denied(self):
        other = tempfile.mkdtemp(prefix="bob-git-out-")
        try:
            self.assertIn("Access denied", git._git_status(other))
            self.assertIn("Access denied", git._git_log(other))
            self.assertIn("Access denied", git._git_diff(other))
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_extra_root_allowed(self):
        extra = tempfile.mkdtemp(prefix="bob-git-extra-")
        try:
            git.configure({"agent": {"gitAllowedRoots": [extra]}})
            # Allowed now — git itself will complain it's not a repo, but NOT "Access denied".
            self.assertNotIn("Access denied", git._git_status(extra))
        finally:
            git.configure({"agent": {}})
            shutil.rmtree(extra, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
