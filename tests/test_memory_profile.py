"""POST-ONE Mission 3 — the "Bob doesn't know me" fixes:
  * config staleness — off Windows, load_config resolves from config/defaults.json and ignores a
    retired-PowerShell data/config.json (which would freeze persona/memory changes);
  * profile injection — a seeded type='profile' row reaches the session's system-prompt block;
  * onboarding-skip — _needs_onboard keys on an actual profile row, not just the config marker.
All hermetic: embed is mocked, DBs are temp, REPO/BOB_DATA_DIR are redirected."""
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import _common  # noqa: F401 — puts scripts/ + scripts/tools on sys.path
import bob_core


class TestConfigStaleness(unittest.TestCase):
    """Off Windows, load_config must resolve from the neutral sources even if a stale data/config.json
    exists — otherwise every config/defaults.json change (persona, memory) is silently shadowed."""

    def setUp(self):
        import bob_config
        self.bob_config = bob_config
        self._orig_resolve = bob_config.resolve_runtime_config
        self._orig_env = os.environ.get("BOB_FORCE_OS")

    def tearDown(self):
        self.bob_config.resolve_runtime_config = self._orig_resolve
        if self._orig_env is None:
            os.environ.pop("BOB_FORCE_OS", None)
        else:
            os.environ["BOB_FORCE_OS"] = self._orig_env

    def test_posix_resolves_from_defaults_not_stale_file(self):
        os.environ["BOB_FORCE_OS"] = "linux"
        self.bob_config.resolve_runtime_config = lambda user_path=None: {"_sentinel": "resolved"}
        # Even if REPO/data/config.json exists on this box, the POSIX path ignores it and resolves.
        self.assertEqual(bob_core.load_config().get("_sentinel"), "resolved")

    def test_persona_guidance_reaches_runtime(self):
        # Regression: the durable-identity guidance in defaults.json must actually reach the loop's
        # system prompt (it was shadowed by the stale data/config.json).
        os.environ["BOB_FORCE_OS"] = "linux"
        sp = bob_core.load_config().get("persona", {}).get("systemPrompt", "")
        self.assertIn("memory_store", sp)


import bob_memory  # noqa: E402 — after sys.path is set by _common


def _fake_embed(text: str):
    return [float(len(text)), float(sum(ord(c) for c in text) % 97), 1.0]


@unittest.skipUnless(bob_memory._DEPS_ERROR is None,
                     f"memory deps not installed: {bob_memory._DEPS_ERROR}")
class TestProfileInjection(unittest.TestCase):
    """A seeded profile row surfaces in profile_block AND in the framed once-per-session block that the
    loop injects into the system prompt."""

    def setUp(self):
        self._orig_embed = bob_memory.embed
        bob_memory.embed = _fake_embed
        self.dir = Path(tempfile.mkdtemp(prefix="bob-prof-"))
        self.db = self.dir / "bob.db"

    def tearDown(self):
        bob_memory.embed = self._orig_embed
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_profile_block_contains_seeded_identity(self):
        bob_memory.cmd_init_profile("Ada Lovelace", "compilers", self.db)
        block = bob_memory.profile_block("local", self.db)
        self.assertIsNotNone(block)
        self.assertIn("Ada Lovelace", block)

    def test_memory_profile_block_framed_and_gated(self):
        bob_memory.cmd_init_profile("Ada Lovelace", "compilers", self.db)
        cfg = _common.fake_config(memory={"enabled": True, "dbPath": str(self.db)})
        block = bob_core.memory_profile_block(owner="local", config=cfg)
        self.assertIsNotNone(block)
        self.assertIn("Ada Lovelace", block)

    def test_disabled_memory_injects_nothing(self):
        bob_memory.cmd_init_profile("Ada Lovelace", "compilers", self.db)
        cfg = _common.fake_config(memory={"enabled": False, "dbPath": str(self.db)})
        self.assertIsNone(bob_core.memory_profile_block(owner="local", config=cfg))


class TestOnboardingSkip(unittest.TestCase):
    """_has_profile_rows (stdlib sqlite3) + _needs_onboard keying on profile presence — so a
    marked-but-unseeded machine re-onboards instead of staying 'Bob doesn't know me'."""

    def setUp(self):
        import bob.kernel as kernel
        self.kernel = kernel
        self.dir = Path(tempfile.mkdtemp(prefix="bob-onb-"))
        self._orig_env = os.environ.get("BOB_DATA_DIR")
        self._orig_repo = kernel.REPO
        self._orig_hpr = kernel._has_profile_rows
        os.environ["BOB_DATA_DIR"] = str(self.dir)

    def tearDown(self):
        self.kernel.REPO = self._orig_repo
        self.kernel._has_profile_rows = self._orig_hpr
        if self._orig_env is None:
            os.environ.pop("BOB_DATA_DIR", None)
        else:
            os.environ["BOB_DATA_DIR"] = self._orig_env
        shutil.rmtree(self.dir, ignore_errors=True)

    def _make_db(self, with_profile: bool):
        db = self.dir / "bob.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE IF NOT EXISTS memories (type TEXT)")
        if with_profile:
            con.execute("INSERT INTO memories (type) VALUES ('profile')")
        con.commit()
        con.close()

    def test_has_profile_rows_no_db(self):
        self.assertFalse(self.kernel._has_profile_rows())

    def test_has_profile_rows_without_and_with(self):
        self._make_db(with_profile=False)
        self.assertFalse(self.kernel._has_profile_rows())
        self._make_db(with_profile=True)
        self.assertTrue(self.kernel._has_profile_rows())

    def _needs(self, user_json, has_profile):
        repo = Path(tempfile.mkdtemp(prefix="bob-repo-", dir=self.dir))
        (repo / "config").mkdir()
        if user_json is not None:
            (repo / "config" / "user.json").write_text(user_json, encoding="utf-8")
        self.kernel.REPO = repo
        self.kernel._has_profile_rows = lambda: has_profile
        return self.kernel._needs_onboard()

    def test_unmarked_always_onboards(self):
        self.assertTrue(self._needs(None, has_profile=False))
        self.assertTrue(self._needs('{"other": 1}', has_profile=True))

    def test_marked_but_no_profile_onboards(self):
        # The exact bug: user.json was {"bob": {}} but the profile never seeded.
        self.assertTrue(self._needs('{"bob": {}}', has_profile=False))

    def test_marked_with_profile_does_not_onboard(self):
        self.assertFalse(self._needs('{"bob": {}}', has_profile=True))


if __name__ == "__main__":
    unittest.main()
