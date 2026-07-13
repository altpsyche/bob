"""Phase 1 one-command install: the verify-install gate the installer runs after setup. It reuses the
existing versions.py readers and reds on any drift. (The install/install.sh clone-vs-pull shell logic is
gated by shellcheck; here we test the Python gate it calls.)"""
import unittest
from pathlib import Path
from unittest import mock

import _common  # noqa: F401 — sets sys.path
from bob import kernel, versions

REPO = Path(__file__).resolve().parent.parent


class TestVerifyInstall(unittest.TestCase):
    def test_passes_when_reproducible_and_in_sync(self):
        with mock.patch.object(versions, "check_reproducibility", return_value=[]), \
             mock.patch.object(versions, "check_sync", return_value=0):
            self.assertEqual(kernel.verify_install(), 0)

    def test_fails_on_submodule_or_model_drift(self):
        with mock.patch.object(versions, "check_reproducibility",
                               return_value=["external/llama.cpp: HEAD != lock"]), \
             mock.patch.object(versions, "check_sync", return_value=0):
            self.assertEqual(kernel.verify_install(), 1)

    def test_fails_when_lock_out_of_sync(self):
        with mock.patch.object(versions, "check_reproducibility", return_value=[]), \
             mock.patch.object(versions, "check_sync", return_value=1):
            self.assertEqual(kernel.verify_install(), 1)

    def test_fails_loudly_when_reader_errors(self):
        with mock.patch.object(versions, "check_reproducibility", side_effect=RuntimeError("no lock")), \
             mock.patch.object(versions, "check_sync", return_value=0):
            self.assertEqual(kernel.verify_install(), 1)


class TestInstallScript(unittest.TestCase):
    """Static guarantees about the hosted installer scripts (content, not execution)."""

    def test_linux_installer_clones_with_submodules_and_verifies(self):
        sh = (REPO / "install" / "install.sh").read_text(encoding="utf-8")
        self.assertIn("git clone --recurse-submodules", sh)   # submodules on fresh clone
        self.assertIn("pull --ff-only", sh)                   # idempotent re-run path
        self.assertIn("bob.kernel verify-install", sh)        # verifies against versions.lock
        self.assertIn("BOB_HOME", sh)

    def test_windows_installer_mirrors_the_flow(self):
        ps = (REPO / "install" / "install.ps1").read_text(encoding="utf-8")
        self.assertIn("git clone --recurse-submodules", ps)
        self.assertIn("bob.kernel verify-install", ps)


if __name__ == "__main__":
    unittest.main()
