"""ONE-D Slice D6 — `update` (scripts/tools/build.py:update_stack). Orchestration over git + build + lock
+ doctor with a bin/ rollback; every piece is mocked so no git/network/compiler runs. CLI-only."""
import sys
import unittest
from pathlib import Path
from unittest import mock

import _common  # noqa: F401 — puts scripts/ on sys.path
from bob import cli, registry

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "tools"))
import build as build_mod  # noqa: E402

CFG = {}


class TestRegistryWiring(unittest.TestCase):
    def test_update_flipped(self):
        self.assertEqual(registry.by_name()["update"]["runtime"], "python")
        self.assertIn("update", cli._HANDLERS)


class TestUpdateStack(unittest.TestCase):
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
