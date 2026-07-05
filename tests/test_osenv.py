"""NB3 (contracts C3 secrets, C4 data-dir) — the OS seam. Per-OS branches are exercised by
monkeypatching platform.system(); the secret precedence and data-dir migration are exercised
against temp trees so no real state is touched."""
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import _common  # noqa: F401 — puts scripts/ on sys.path
import osenv


class TestDefaultShell(unittest.TestCase):
    def test_windows_uses_pwsh(self):
        with mock.patch("osenv.platform.system", return_value="Windows"):
            self.assertEqual(osenv.default_shell(), ["pwsh", "-NonInteractive", "-Command"])

    def test_non_windows_uses_bash(self):
        with mock.patch("osenv.platform.system", return_value="Linux"), \
             mock.patch("osenv.shutil.which", side_effect=lambda x: "/bin/bash" if x == "bash" else None):
            self.assertEqual(osenv.default_shell(), ["/bin/bash", "-c"])


class TestDataDir(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.pop("BOB_DATA_DIR", None)
        self._repo = osenv.REPO

    def tearDown(self):
        osenv.REPO = self._repo
        if self._env is not None:
            os.environ["BOB_DATA_DIR"] = self._env
        else:
            os.environ.pop("BOB_DATA_DIR", None)

    def test_default_is_repo_relative(self):
        fake_repo = Path(tempfile.mkdtemp(prefix="bob-repo-"))
        try:
            osenv.REPO = fake_repo
            self.assertEqual(osenv.data_dir(), fake_repo / "data")
        finally:
            shutil.rmtree(fake_repo, ignore_errors=True)

    def test_override_migrates_once(self):
        fake_repo = Path(tempfile.mkdtemp(prefix="bob-repo-"))
        dst = Path(tempfile.mkdtemp(prefix="bob-xdg-"))
        try:
            osenv.REPO = fake_repo
            (fake_repo / "data").mkdir()
            (fake_repo / "data" / "bob.db").write_text("original", encoding="utf-8")
            os.environ["BOB_DATA_DIR"] = str(dst)

            self.assertEqual(osenv.data_dir(), dst)
            self.assertEqual((dst / "bob.db").read_text(encoding="utf-8"), "original")
            self.assertTrue((dst / ".migrated").exists())

            # a second call must NOT re-copy over a since-modified destination file
            (dst / "bob.db").write_text("modified", encoding="utf-8")
            osenv.data_dir()
            self.assertEqual((dst / "bob.db").read_text(encoding="utf-8"), "modified")
        finally:
            shutil.rmtree(fake_repo, ignore_errors=True)
            shutil.rmtree(dst, ignore_errors=True)


class TestSecret(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.pop("BOB_DATA_DIR", None)
        self._litellm_env = os.environ.pop("BOB_LITELLMKEY", None)
        self.dst = Path(tempfile.mkdtemp(prefix="bob-sec-"))
        os.environ["BOB_DATA_DIR"] = str(self.dst)
        # Force the keychain step to a no-op so precedence tests are deterministic.
        self._fake_keyring = types.SimpleNamespace(get_password=lambda service, name: None)
        self._km = mock.patch.dict(sys.modules, {"keyring": self._fake_keyring})
        self._km.start()

    def tearDown(self):
        self._km.stop()
        shutil.rmtree(self.dst, ignore_errors=True)
        for k, v in (("BOB_DATA_DIR", self._env), ("BOB_LITELLMKEY", self._litellm_env)):
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def test_file_then_env_precedence(self):
        (self.dst / "secrets.json").write_text(json.dumps({"litellmKey": "from-file"}), encoding="utf-8")
        # file wins over the config default
        self.assertEqual(osenv.secret("litellmKey", default="sk-local"), "from-file")
        # env wins over the file
        os.environ["BOB_LITELLMKEY"] = "from-env"
        self.assertEqual(osenv.secret("litellmKey", default="sk-local"), "from-env")

    def test_default_when_absent(self):
        self.assertEqual(osenv.secret("nope", default="fallback"), "fallback")

    def test_secrets_file_lives_under_data_dir(self):
        # C3: the secrets file is under data_dir() (gitignored /data/), never a tracked path.
        self.assertEqual(osenv.secrets_file(), self.dst / "secrets.json")


class TestNotify(unittest.TestCase):
    def test_noop_when_no_backend(self):
        with mock.patch("osenv.platform.system", return_value="Linux"), \
             mock.patch("osenv.shutil.which", return_value=None):
            self.assertFalse(osenv.notify("t", "b"))


class TestAudioSeam(unittest.TestCase):
    """ONE-B3 — the mic-in / speaker-out seam. Playback backends are exercised by monkeypatching
    platform.system + shutil.which + subprocess.run; capture is exercised for its no-audio-stack path."""

    def test_play_audio_linux_uses_first_available_player(self):
        ran = {}
        with mock.patch("osenv.platform.system", return_value="Linux"), \
             mock.patch("osenv.shutil.which", side_effect=lambda n: "/usr/bin/paplay" if n == "paplay" else None), \
             mock.patch("osenv.subprocess.run", side_effect=lambda argv, check=False: ran.update(argv=argv)):
            self.assertTrue(osenv.play_audio("/tmp/x.wav"))
        self.assertEqual(ran["argv"], ["/usr/bin/paplay", "/tmp/x.wav"])

    def test_play_audio_ffplay_gets_quiet_flags(self):
        ran = {}
        with mock.patch("osenv.platform.system", return_value="Linux"), \
             mock.patch("osenv.shutil.which", side_effect=lambda n: "/usr/bin/ffplay" if n == "ffplay" else None), \
             mock.patch("osenv.subprocess.run", side_effect=lambda argv, check=False: ran.update(argv=argv)):
            self.assertTrue(osenv.play_audio("/tmp/x.wav"))
        self.assertEqual(ran["argv"],
                         ["/usr/bin/ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "/tmp/x.wav"])

    def test_play_audio_no_backend_returns_false(self):
        with mock.patch("osenv.platform.system", return_value="Linux"), \
             mock.patch("osenv.shutil.which", return_value=None):
            self.assertFalse(osenv.play_audio("/tmp/x.wav"))

    def test_record_audio_without_audio_stack_raises(self):
        # Forcing the module entry to None makes `import sounddevice` raise ImportError.
        with mock.patch.dict(sys.modules, {"sounddevice": None}):
            with self.assertRaises(RuntimeError):
                osenv.record_audio(0.1)

    def test_pcm_to_wav_produces_valid_wav(self):
        wav = osenv._pcm_to_wav(b"\x00\x00\x01\x00")
        self.assertTrue(wav.startswith(b"RIFF"))
        self.assertIn(b"WAVE", wav[:16])


if __name__ == "__main__":
    unittest.main()
