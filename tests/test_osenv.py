"""The OS seam: secrets and data-dir contracts. Per-OS branches are exercised by
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

try:
    import numpy  # noqa: F401 — optional dep of osenv.record_audio (mic capture); absent on the CI gate python
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False


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
        # the secrets file is under data_dir() (gitignored /data/), never a tracked path.
        self.assertEqual(osenv.secrets_file(), self.dst / "secrets.json")


class TestNotify(unittest.TestCase):
    def test_noop_when_no_backend(self):
        with mock.patch("osenv.platform.system", return_value="Linux"), \
             mock.patch("osenv.shutil.which", return_value=None):
            self.assertFalse(osenv.notify("t", "b"))


class TestAudioSeam(unittest.TestCase):
    """The mic-in / speaker-out seam. Playback backends are exercised by monkeypatching
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

    @unittest.skipUnless(_HAS_NUMPY, "numpy not installed (optional mic-capture dep)")
    def test_record_audio_no_speech_returns_instead_of_hanging(self):
        # The "I speak and nothing happens" bug: a silent/too-quiet input must NOT loop forever.
        import numpy as np

        class _Silent:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n): return np.zeros((n, 1), dtype="int16"), None

        fake_sd = types.SimpleNamespace(InputStream=lambda **k: _Silent())
        with mock.patch.dict(sys.modules, {"sounddevice": fake_sd}):
            out = osenv.record_audio(silence_sec=0.1, max_wait_sec=0.3)   # 3 silent chunks -> bail
        self.assertEqual(out, b"")

    @unittest.skipUnless(_HAS_NUMPY, "numpy not installed (optional mic-capture dep)")
    def test_record_audio_captures_then_stops_on_silence(self):
        import numpy as np
        n = int(osenv._AUDIO_SAMPLE_RATE * 0.1)
        loud = np.full((n, 1), 5000, dtype="int16")
        quiet = np.zeros((n, 1), dtype="int16")
        seq = {"i": 0}

        class _Seq:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, _n):
                i = seq["i"]; seq["i"] += 1
                return (loud if i < 2 else quiet), None   # 2 loud chunks, then silence

        fake_sd = types.SimpleNamespace(InputStream=lambda **k: _Seq())
        with mock.patch.dict(sys.modules, {"sounddevice": fake_sd}):
            out = osenv.record_audio(silence_sec=0.2, max_wait_sec=5.0)
        self.assertTrue(out.startswith(b"RIFF"))          # a WAV was produced from the captured speech

    def test_pcm_to_wav_produces_valid_wav(self):
        wav = osenv._pcm_to_wav(b"\x00\x00\x01\x00")
        self.assertTrue(wav.startswith(b"RIFF"))
        self.assertIn(b"WAVE", wav[:16])


# --- OS-detection / platform seams -----------------------------------------------------------------------------


class _ForceOSMixin:
    """setUp/tearDown that drive os_name() via the BOB_FORCE_OS test hook, restoring it after."""

    def _force(self, os_value):
        os.environ["BOB_FORCE_OS"] = os_value

    def setUp(self):
        self._saved_force = os.environ.pop("BOB_FORCE_OS", None)

    def tearDown(self):
        if self._saved_force is not None:
            os.environ["BOB_FORCE_OS"] = self._saved_force
        else:
            os.environ.pop("BOB_FORCE_OS", None)


class TestOsName(_ForceOSMixin, unittest.TestCase):
    def test_forced_values(self):
        for val in ("windows", "linux", "macos"):
            self._force(val)
            self.assertEqual(osenv.os_name(), val)

    def test_invalid_force_is_ignored_and_warns(self):
        self._force("plan9")
        with mock.patch("osenv.platform.system", return_value="Linux"):
            with mock.patch("sys.stderr", new_callable=lambda: __import__("io").StringIO()) as err:
                self.assertEqual(osenv.os_name(), "linux")
        self.assertIn("BOB_FORCE_OS", err.getvalue())

    def test_is_windows_follows_os_name(self):
        self._force("windows")
        self.assertTrue(osenv.is_windows())
        self._force("linux")
        self.assertFalse(osenv.is_windows())

    def test_darwin_maps_to_macos(self):
        with mock.patch("osenv.platform.system", return_value="Darwin"):
            self.assertEqual(osenv.os_name(), "macos")


class TestPathResolvers(_ForceOSMixin, unittest.TestCase):
    def test_exe_name(self):
        self._force("windows")
        self.assertEqual(osenv.exe_name("llama-server"), "llama-server.exe")
        self._force("linux")
        self.assertEqual(osenv.exe_name("llama-server"), "llama-server")

    def test_venv_exe(self):
        self._force("windows")
        self.assertEqual(osenv.venv_exe("venv-aider", "aider"),
                         osenv.REPO / "tools" / "venv-aider" / "Scripts" / "aider.exe")
        self._force("linux")
        self.assertEqual(osenv.venv_exe("venv-aider", "aider"),
                         osenv.REPO / "tools" / "venv-aider" / "bin" / "aider")

    def test_bin_exe(self):
        self._force("windows")
        self.assertEqual(osenv.bin_exe("llama-server"), osenv.REPO / "bin" / "llama-server.exe")
        self._force("linux")
        self.assertEqual(osenv.bin_exe("llama-server"), osenv.REPO / "bin" / "llama-server")

    def test_home_config_dir_windows(self):
        self._force("windows")
        with mock.patch.dict(os.environ, {"USERPROFILE": r"C:\Users\bob"}):
            self.assertEqual(osenv.home_config_dir("fabric"),
                             Path(r"C:\Users\bob") / ".config" / "fabric")

    def test_home_config_dir_linux_xdg(self):
        self._force("linux")
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/custom/cfg"}):
            self.assertEqual(osenv.home_config_dir("fabric"), Path("/custom/cfg") / "fabric")

    def test_home_config_dir_linux_default(self):
        self._force("linux")
        env = {k: v for k, v in os.environ.items() if k != "XDG_CONFIG_HOME"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(osenv.home_config_dir("fabric"), Path.home() / ".config" / "fabric")


class TestPortAndPid(unittest.TestCase):
    def test_port_free_is_not_in_use(self):
        import socket
        # Bind but DON'T listen/accept on an ephemeral port, then close -> nothing accepts there.
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
        s.close()
        self.assertFalse(osenv.is_port_in_use(free_port))

    def test_port_in_use_when_listening(self):
        import socket
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        try:
            self.assertTrue(osenv.is_port_in_use(srv.getsockname()[1]))
        finally:
            srv.close()

    def test_pid_alive(self):
        self.assertTrue(osenv.pid_alive(os.getpid()))
        self.assertFalse(osenv.pid_alive(0))
        self.assertFalse(osenv.pid_alive(2_000_000_000))  # PID far above any live process


@unittest.skipIf(osenv.os_name() == "windows", "POSIX detach/kill round-trip")
class TestProcessLifecyclePosix(unittest.TestCase):
    def test_start_detached_writes_pidfile_and_tree_kill_reaps(self):
        import tempfile
        import time
        import warnings
        # start_detached is fire-and-forget: it discards the Popen, whose finalizer would emit a
        # spurious ResourceWarning ("still running") even though we reap the child below.
        warnings.simplefilter("ignore", ResourceWarning)
        self.addCleanup(warnings.resetwarnings)
        pidfile = Path(tempfile.mkdtemp(prefix="bob-pid-")) / "svc.pid"
        pid = osenv.start_detached(["sleep", "30"], pidfile=pidfile)
        try:
            self.assertEqual(pidfile.read_text().strip(), str(pid))
            time.sleep(0.2)
            self.assertTrue(osenv.pid_alive(pid))
            osenv.stop_process_tree(pid)
            # Reap our direct child so it doesn't linger as a zombie for other tests.
            for _ in range(20):
                try:
                    if os.waitpid(pid, os.WNOHANG)[0] != 0:
                        break
                except ChildProcessError:
                    break
                time.sleep(0.05)
            self.assertFalse(osenv.pid_alive(pid))
        finally:
            try:
                os.kill(pid, 9)
                os.waitpid(pid, 0)
            except (ProcessLookupError, ChildProcessError):
                pass

    def test_start_detached_logs_output_and_passes_env(self):
        import tempfile
        import time
        import warnings
        warnings.simplefilter("ignore", ResourceWarning)
        self.addCleanup(warnings.resetwarnings)
        log = Path(tempfile.mkdtemp(prefix="bob-log-")) / "svc.log"
        # A short-lived child that writes to stdout+stderr and reads an injected env var.
        pid = osenv.start_detached(
            ["sh", "-c", "echo out-$BOB_T; echo err >&2"], log_path=log, env={"BOB_T": "xyz"})
        try:
            for _ in range(40):
                if log.exists() and "out-xyz" in log.read_text():
                    break
                time.sleep(0.05)
            body = log.read_text()
            self.assertIn("out-xyz", body)   # env injected + stdout captured
            self.assertIn("err", body)       # stderr folded into the same log
        finally:
            try:
                os.waitpid(pid, 0)
            except (ChildProcessError, ProcessLookupError):
                pass

    def test_process_stats_live_then_dead(self):
        import time
        import warnings
        warnings.simplefilter("ignore", ResourceWarning)
        self.addCleanup(warnings.resetwarnings)
        pid = osenv.start_detached(["sleep", "30"])
        try:
            time.sleep(0.2)
            stats = osenv.process_stats(pid)
            self.assertIsNotNone(stats)
            self.assertIn("rss_mb", stats)
            self.assertRegex(stats["uptime"], r"^\d+:\d\d:\d\d$")
        finally:
            osenv.stop_process_tree(pid)
            try:
                os.waitpid(pid, 0)
            except (ChildProcessError, ProcessLookupError):
                pass
        self.assertIsNone(osenv.process_stats(pid))  # dead -> None

    def test_fmt_uptime(self):
        self.assertEqual(osenv._fmt_uptime(0), "0:00:00")
        self.assertEqual(osenv._fmt_uptime(3725), "1:02:05")
        self.assertEqual(osenv._fmt_uptime(-5), "0:00:00")


class TestKillByName(_ForceOSMixin, unittest.TestCase):
    """Name-kill matches only Bob's OWN executables under the repo's bin/ and tools/, never a substring of
    some other command line (an editor on logs/llama-swap.log, a `tail -f`, a system llama-server)."""

    def setUp(self):
        super().setUp()
        self.repo = Path(tempfile.mkdtemp(prefix="bob-repo-"))
        self.addCleanup(shutil.rmtree, self.repo, True)
        p = mock.patch.object(osenv, "REPO", self.repo)
        p.start()
        self.addCleanup(p.stop)

    def _table(self):
        r = str(self.repo)
        return [
            (100, f"{r}/bin/llama-swap", [f"{r}/bin/llama-swap", "--config", "x"]),
            (101, "/usr/bin/vim", ["vim", f"{r}/logs/llama-swap.log"]),
            (102, "/usr/bin/tail", ["tail", "-f", "llama-swap"]),
            (103, "/usr/bin/python3.12", [f"{r}/tools/venv-webui/bin/python",
                                          f"{r}/tools/venv-webui/bin/open-webui", "serve"]),
            (104, "/usr/local/bin/llama-server", ["/usr/local/bin/llama-server", "-m", "x"]),
            (105, "/usr/bin/bash", ["bash", "-c", f"pkill -f llama-swap; {r}/bin/llama-swap"]),
            (os.getpid(), f"{r}/bin/llama-swap", []),   # never ourselves
        ]

    def test_matches_only_repo_executables(self):
        with mock.patch.object(osenv, "_process_table", side_effect=self._table):
            found = osenv.find_managed_processes(["llama-swap", "llama-server", "open-webui"])
        self.assertEqual(sorted(found), [(100, "llama-swap"), (103, "open-webui")])

    def test_stop_kills_matches_and_returns_names(self):
        reaped = []
        with mock.patch.object(osenv, "_process_table", side_effect=self._table), \
             mock.patch.object(osenv, "stop_process_tree", side_effect=lambda pid: reaped.append(pid)):
            killed = osenv.stop_processes_by_name(["llama-swap", "open-webui", "nothing"])
        self.assertEqual(killed, ["llama-swap", "open-webui"])
        self.assertEqual(sorted(reaped), [100, 103])

    def test_string_arg_is_treated_as_single_name(self):
        with mock.patch.object(osenv, "_process_table", side_effect=self._table), \
             mock.patch.object(osenv, "stop_process_tree"):
            self.assertEqual(osenv.stop_processes_by_name("nothing"), [])

    @unittest.skipUnless(sys.platform.startswith("linux"), "real /proc scan")
    def test_real_proc_scan_ignores_lookalike_command_lines(self):
        import warnings
        warnings.simplefilter("ignore", ResourceWarning)
        self.addCleanup(warnings.resetwarnings)
        (self.repo / "bin").mkdir()
        exe = self.repo / "bin" / "llama-swap"
        shutil.copy2(shutil.which("sleep"), exe)
        ours = osenv.start_detached([str(exe), "30"])
        lookalike = osenv.start_detached(["sh", "-c", "sleep 30 # llama-swap"])
        try:
            import time
            time.sleep(0.2)
            pids = [pid for pid, _ in osenv.find_managed_processes("llama-swap")]
            self.assertIn(ours, pids)
            self.assertNotIn(lookalike, pids)
        finally:
            for pid in (ours, lookalike):
                osenv.stop_process_tree(pid, timeout=2)


@unittest.skipIf(osenv.os_name() == "windows", "POSIX signal escalation")
class TestStopWaitsForExit(unittest.TestCase):
    def test_term_ignoring_process_is_killed_and_waited_for(self):
        import time
        import warnings
        warnings.simplefilter("ignore", ResourceWarning)
        self.addCleanup(warnings.resetwarnings)
        pid = osenv.start_detached(["sh", "-c", "trap '' TERM; sleep 30"])
        time.sleep(0.2)
        t0 = time.monotonic()
        self.assertTrue(osenv.stop_process_tree(pid, timeout=0.5))   # SIGTERM ignored -> SIGKILL
        self.assertFalse(osenv.pid_alive(pid))                       # gone when it returns, not later
        self.assertLess(time.monotonic() - t0, 5)


class TestCrontabSafety(_ForceOSMixin, unittest.TestCase):
    """A failing `crontab -l` must never be read as an empty crontab (the rewrite would wipe the user's
    entries), and the user's lines are written back verbatim."""

    def _fake(self, rc, stdout="", stderr="", written=None):
        def run(argv, **kw):
            r = types.SimpleNamespace(returncode=0, stdout="", stderr="")
            if argv[:2] == ["crontab", "-l"]:
                r.returncode, r.stdout, r.stderr = rc, stdout, stderr
            elif argv == ["crontab", "-"]:
                if written is not None:
                    written.append(kw.get("input", ""))
            return r
        return run

    def test_read_failure_aborts_without_writing(self):
        self._force("linux")
        written = []
        with mock.patch("osenv.crontab_available", return_value=True), \
             mock.patch("osenv.shutil.which", return_value=None), \
             mock.patch("osenv.subprocess.run", side_effect=self._fake(1, stderr="crontab: permission denied",
                                                                     written=written)):
            with self.assertRaises(RuntimeError):
                osenv.register_agent_task("/py", "/runner.py")
        self.assertEqual(written, [])

    def test_no_crontab_yet_is_empty(self):
        self._force("linux")
        written = []
        with mock.patch("osenv.crontab_available", return_value=True), \
             mock.patch("osenv.shutil.which", return_value=None), \
             mock.patch("osenv.subprocess.run", side_effect=self._fake(1, stderr="no crontab for siva",
                                                                     written=written)):
            osenv.register_agent_task("/py", "/runner.py")
        self.assertEqual(len(written), 1)
        self.assertIn("# BobAgent", written[0])

    def test_user_lines_preserved_verbatim(self):
        self._force("linux")
        existing = "MAILTO=me\n\n# nightly backup\n0 5 * * * backup\n\n"
        written = []
        with mock.patch("osenv.crontab_available", return_value=True), \
             mock.patch("osenv.shutil.which", return_value=None), \
             mock.patch("osenv.subprocess.run", side_effect=self._fake(0, stdout=existing, written=written)):
            osenv.register_agent_task("/py", "/runner.py")
        self.assertTrue(written[0].startswith("MAILTO=me\n\n# nightly backup\n0 5 * * * backup\n\n"))

    def test_status_reports_unknown_on_read_failure(self):
        self._force("linux")
        with mock.patch("osenv.crontab_available", return_value=True), \
             mock.patch("osenv.subprocess.run", side_effect=self._fake(1, stderr="boom")):
            st = osenv.agent_task_status()
        self.assertFalse(st["registered"])
        self.assertIn("unknown", st["state"])


class TestDownload(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = self.tmp / "src.bin"
        self.src.write_bytes(b"payload" * 1000)
        import hashlib
        self.sha = hashlib.sha256(self.src.read_bytes()).hexdigest()

    def test_verified_download_lands_atomically(self):
        dest = self.tmp / "out" / "file.bin"
        osenv.download(self.src.as_uri(), dest, sha256=self.sha)
        self.assertEqual(dest.read_bytes(), self.src.read_bytes())
        self.assertFalse(dest.with_name("file.bin.part").exists())

    def test_mismatch_leaves_nothing_behind(self):
        dest = self.tmp / "file.bin"
        with self.assertRaises(RuntimeError):
            osenv.download(self.src.as_uri(), dest, sha256="0" * 64)
        self.assertFalse(dest.exists())                             # never looks "present"
        self.assertFalse((self.tmp / "file.bin.part").exists())

    def test_interrupted_download_leaves_nothing_behind(self):
        dest = self.tmp / "file.bin"
        with mock.patch("urllib.request.urlopen", side_effect=OSError("connection reset")):
            with self.assertRaises(OSError):
                osenv.download("https://example.invalid/x", dest)
        self.assertFalse(dest.exists())

    def test_require_sha_refuses_empty(self):
        with mock.patch("urllib.request.urlopen") as uo:
            with self.assertRaises(RuntimeError):
                osenv.download("https://example.invalid/x", self.tmp / "x", sha256="", require_sha=True)
        uo.assert_not_called()


def _ensure_secret_worker(args):
    """One process's first use of a secret, released at `start` so the calls overlap (multiprocessing)."""
    import time as _time
    data_dir, name, start = args
    os.environ["BOB_DATA_DIR"] = data_dir
    sys.modules["keyring"] = None
    _time.sleep(max(0.0, start - _time.time()))
    return osenv.ensure_secret(name)


class TestEnsureSecret(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._env = {k: os.environ.pop(k) for k in ("BOB_DATA_DIR", "bobTestSecret", "BOB_BOBTESTSECRET")
                     if k in os.environ}
        os.environ["BOB_DATA_DIR"] = str(self.tmp)
        self.addCleanup(self._restore)
        p = mock.patch.dict(sys.modules, {"keyring": None})   # never touch the real OS keychain
        p.start()
        self.addCleanup(p.stop)

    def _restore(self):
        os.environ.pop("BOB_DATA_DIR", None)
        os.environ.update(self._env)

    def test_generated_once_then_stable(self):
        a = osenv.ensure_secret("bobTestSecret", prefix="pk-")
        self.assertTrue(a.startswith("pk-") and len(a) > 20)
        self.assertEqual(osenv.ensure_secret("bobTestSecret"), a)
        data = json.loads(osenv.secrets_file().read_text())
        self.assertEqual(data["bobTestSecret"], a)
        if osenv.os_name() != "windows":
            self.assertEqual(osenv.secrets_file().stat().st_mode & 0o777, 0o600)

    def test_concurrent_first_use_agrees_on_one_value(self):
        """Many processes generating the same secret at once: every one returns the value that was stored,
        and each earlier secret in the file survives (no lost update)."""
        import multiprocessing
        import time
        osenv.ensure_secret("bobKeepSecret")
        kept = json.loads(osenv.secrets_file().read_text())["bobKeepSecret"]
        start = time.time() + 1.5
        names = [f"race{i % 4}" for i in range(24)]
        with multiprocessing.get_context("spawn").Pool(8) as pool:
            got = pool.map(_ensure_secret_worker, [(str(self.tmp), n, start) for n in names])
        data = json.loads(osenv.secrets_file().read_text())
        for n, v in zip(names, got):
            self.assertEqual(v, data[n], n)
        self.assertEqual(len(set(got)), 4)
        self.assertEqual(data["bobKeepSecret"], kept)
        self.assertEqual([p.name for p in self.tmp.glob("*.tmp")], [])

    def test_the_temp_file_is_never_wider_than_0600(self):
        if osenv.os_name() == "windows":
            self.skipTest("POSIX modes")
        modes = []
        real_replace = os.replace

        def spy(src, dst):
            modes.append(os.stat(src).st_mode & 0o777)
            return real_replace(src, dst)

        old_umask = os.umask(0o022)
        try:
            with mock.patch.object(osenv.os, "replace", side_effect=spy):
                osenv.ensure_secret("bobTestSecret")
        finally:
            os.umask(old_umask)
        self.assertEqual(modes, [0o600])

    def test_legacy_value_adopted_for_existing_data(self):
        self.assertEqual(osenv.ensure_secret("bobTestSecret", legacy="old-key"), "old-key")

    def test_a_malformed_file_is_moved_aside_not_overwritten(self):
        sf = osenv.secrets_file()
        sf.parent.mkdir(parents=True, exist_ok=True)
        broken = '{"N8N_ENCRYPTION_KEY": "keep-me", "WEBUI_SECRET_KEY": '
        sf.write_text(broken)
        self.assertIsNone(osenv.secret("bobTestSecret"))           # a read still does not crash
        with self.assertRaises(osenv.SecretsFileCorrupt) as cm:
            osenv.ensure_secret("bobTestSecret")
        self.assertFalse(sf.exists())                               # nothing was written in its place
        aside = list(self.tmp.glob("secrets.json.corrupt-*"))
        self.assertEqual(len(aside), 1)
        self.assertEqual(aside[0].read_text(), broken)
        self.assertIn(str(aside[0]), str(cm.exception))
        if osenv.os_name() != "windows":
            self.assertEqual(aside[0].stat().st_mode & 0o777, 0o600)

    def test_a_non_object_file_is_also_refused(self):
        sf = osenv.secrets_file()
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_text('["not", "a", "mapping"]')
        with self.assertRaises(osenv.SecretsFileCorrupt):
            osenv.ensure_secret("bobTestSecret")

    def test_a_read_racing_the_replace_on_windows_retries(self):
        sf = osenv.secrets_file()
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_text('{"bobTestSecret": "v"}')
        real = Path.read_text
        fails = iter([PermissionError("sharing violation")])

        def flaky(self_, *a, **k):
            err = next(fails, None)
            if err is not None:
                raise err
            return real(self_, *a, **k)

        with mock.patch.object(osenv, "is_windows", return_value=True), \
             mock.patch.object(Path, "read_text", flaky), \
             mock.patch("time.sleep"):
            self.assertEqual(osenv.secret("bobTestSecret"), "v")

    def test_env_wins(self):
        os.environ["bobTestSecret"] = "from-env"
        self.addCleanup(os.environ.pop, "bobTestSecret", None)
        self.assertEqual(osenv.ensure_secret("bobTestSecret"), "from-env")
        self.assertFalse(osenv.secrets_file().exists())


class TestVenvInstallsFromLock(_ForceOSMixin, unittest.TestCase):
    """Every OS installs a venv from the pinned .lock (not the unpinned .txt), and extras resolve against it."""

    def test_linux_uses_lock_and_constrains_extras(self):
        self._force("linux")
        repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, repo, True)
        (repo / "tools").mkdir()
        (repo / "tools" / "x-requirements.lock").write_text("litellm==1.0.0\n")
        (repo / "tools" / "x-requirements.txt").write_text("litellm>=1.0\n")
        vpy = repo / "tools" / "venv-x" / "bin" / "python"
        vpy.parent.mkdir(parents=True)
        vpy.write_text("")
        calls = []
        with mock.patch.object(osenv, "REPO", repo), \
             mock.patch.object(osenv, "_py_minor", return_value=(3, 12)), \
             mock.patch("osenv.subprocess.run",
                        side_effect=lambda argv, **k: calls.append(argv) or types.SimpleNamespace(returncode=0)):
            osenv.new_bob_venv("venv-x", "x-requirements", extra_packages=["sounddevice"], python="py")
        req = next(c for c in calls if "-r" in c)
        self.assertTrue(req[req.index("-r") + 1].endswith("x-requirements.lock"))
        extra = next(c for c in calls if "sounddevice" in c)
        self.assertIn("-c", extra)

    def test_committed_litellm_lock_installs_everywhere(self):
        lock = (Path(osenv.REPO) / "tools" / "litellm-requirements.lock").read_text().splitlines()
        rows = [ln for ln in lock if ln.strip() and not ln.startswith("#")]
        for ln in rows:
            self.assertRegex(ln, r"^[A-Za-z0-9_.\-]+==[^ ;]+( ; sys_platform [!=]= \"win32\")?$", ln)
        names = {ln.split("==")[0].lower() for ln in rows}
        # The Langfuse callback + OTLP exporter the docs promise, and yt-dlp (music plugin) must be pinned.
        for need in ("langfuse", "opentelemetry-exporter-otlp-proto-http", "yt-dlp", "litellm"):
            self.assertIn(need, names)
        self.assertTrue(any(ln.startswith("pywin32==") and "win32" in ln for ln in rows))


class TestInstallFiles(unittest.TestCase):
    """bin/ installs replace each file (new inode) instead of rewriting it in place, keep SONAME symlinks as
    symlinks, and prune older versions of a lib the new set ships."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = self.tmp / "src"
        self.src.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()

    def test_replaces_with_a_new_inode(self):
        (self.bin / "llama-server").write_text("old")
        before = (self.bin / "llama-server").stat().st_ino
        (self.src / "llama-server").write_text("new")
        osenv.install_files({"llama-server": self.src / "llama-server"}, self.bin)
        self.assertEqual((self.bin / "llama-server").read_text(), "new")
        self.assertNotEqual((self.bin / "llama-server").stat().st_ino, before)
        self.assertEqual([p.name for p in self.bin.iterdir() if p.name.startswith(".staging")], [])

    @unittest.skipUnless(sys.platform.startswith("linux"), "ETXTBSY is a Linux behaviour")
    def test_running_binary_can_be_replaced(self):
        import time
        import warnings
        warnings.simplefilter("ignore", ResourceWarning)
        self.addCleanup(warnings.resetwarnings)
        exe = self.bin / "llama-server"
        shutil.copy2(shutil.which("sleep"), exe)
        pid = osenv.start_detached([str(exe), "30"])
        try:
            time.sleep(0.2)
            with self.assertRaises(OSError):          # the in-place copy the installer must never do
                shutil.copy2(shutil.which("sleep"), exe)
            shutil.copy2(shutil.which("sleep"), self.src / "llama-server")
            osenv.install_files({"llama-server": self.src / "llama-server"}, self.bin)
            self.assertTrue(osenv.pid_alive(pid))      # the running engine is untouched
        finally:
            osenv.stop_process_tree(pid, timeout=2)

    @unittest.skipIf(sys.platform == "win32", "symlinks need privilege on Windows")
    def test_symlinks_kept_and_stale_versions_pruned(self):
        (self.bin / "libggml-base.so.0.9.3").write_text("old-lib")
        (self.bin / "libcublas.so.12").write_text("unrelated, not shipped by this set")
        (self.src / "libggml-base.so.0.9.4").write_text("x" * 1000)
        os.symlink("libggml-base.so.0.9.4", self.src / "libggml-base.so.0")
        os.symlink("libggml-base.so.0", self.src / "libggml-base.so")
        entries = {p.name: p for p in self.src.iterdir()}
        osenv.install_files(entries, self.bin)
        self.assertTrue((self.bin / "libggml-base.so").is_symlink())
        self.assertEqual(os.readlink(self.bin / "libggml-base.so.0"), "libggml-base.so.0.9.4")
        self.assertFalse((self.bin / "libggml-base.so.0.9.3").exists())   # stale version pruned
        self.assertTrue((self.bin / "libcublas.so.12").exists())          # other libs untouched


class TestOpenUrl(_ForceOSMixin, unittest.TestCase):
    def test_linux_uses_xdg_open(self):
        self._force("linux")
        with mock.patch("osenv.shutil.which", return_value="/usr/bin/xdg-open"), \
             mock.patch("osenv.subprocess.Popen") as popen:
            self.assertTrue(osenv.open_url("http://x"))
            popen.assert_called_once()
            self.assertEqual(popen.call_args[0][0][0], "/usr/bin/xdg-open")

    def test_macos_uses_open(self):
        self._force("macos")
        with mock.patch("osenv.shutil.which", return_value="/usr/bin/open") as which, \
             mock.patch("osenv.subprocess.Popen"):
            self.assertTrue(osenv.open_url("http://x"))
            self.assertEqual(which.call_args[0][0], "open")

    def test_no_opener_returns_false(self):
        self._force("linux")
        with mock.patch("osenv.shutil.which", return_value=None):
            self.assertFalse(osenv.open_url("http://x"))


class TestDockerSeam(_ForceOSMixin, unittest.TestCase):
    """#5a — docker presence + an OS-appropriate install hint for the status/doctor 'needs Docker' signal."""

    def test_present_tracks_which(self):
        with mock.patch("osenv.shutil.which", return_value="/usr/bin/docker"):
            self.assertTrue(osenv.docker_present())
        with mock.patch("osenv.shutil.which", return_value=None):
            self.assertFalse(osenv.docker_present())

    def test_install_hint_is_os_specific(self):
        self._force("macos")
        self.assertIn("Docker Desktop", osenv.docker_install_hint())
        self._force("linux")
        self.assertIn("docker package", osenv.docker_install_hint())


def _smi(stdout):
    r = mock.Mock()
    r.stdout = stdout
    r.stderr = ""
    r.returncode = 0
    return r


class TestGpuSeams(unittest.TestCase):
    """gpu_vram_gb / gpu_arch / gpu_info consolidated into osenv (were duped in health/models)."""

    def test_vram_parses_and_rounds(self):
        with mock.patch("osenv.shutil.which", return_value="/usr/bin/nvidia-smi"), \
             mock.patch("osenv.subprocess.run", return_value=_smi("16384\n")):
            self.assertEqual(osenv.gpu_vram_gb(), 16)

    def test_vram_none_without_nvidia_smi(self):
        with mock.patch("osenv.shutil.which", return_value=None):
            self.assertIsNone(osenv.gpu_vram_gb())

    def test_arch_blackwell(self):
        with mock.patch("osenv.shutil.which", return_value="/usr/bin/nvidia-smi"), \
             mock.patch("osenv.subprocess.run", return_value=_smi("12.0\n")):
            g = osenv.gpu_arch()
            self.assertEqual(g["CudaArch"], 120)
            self.assertEqual(g["Gen"], "Blackwell")
            self.assertEqual(g["MinCudaMajor"], 12)

    def test_arch_ada(self):
        with mock.patch("osenv.shutil.which", return_value="/usr/bin/nvidia-smi"), \
             mock.patch("osenv.subprocess.run", return_value=_smi("8.9\n")):
            self.assertEqual(osenv.gpu_arch()["Gen"], "Ada Lovelace")

    def test_arch_unparseable_is_none(self):
        with mock.patch("osenv.shutil.which", return_value="/usr/bin/nvidia-smi"), \
             mock.patch("osenv.subprocess.run", return_value=_smi("weird\n")):
            self.assertIsNone(osenv.gpu_arch())

    def test_info_composes_or_none(self):
        with mock.patch("osenv.gpu_arch", return_value={"CudaArch": 89, "Gen": "Ada Lovelace", "MinCudaMajor": 11}), \
             mock.patch("osenv.gpu_vram_gb", return_value=24):
            self.assertEqual(osenv.gpu_info(), {"VramGB": 24, "CudaArch": 89, "Gen": "Ada Lovelace", "MinCudaMajor": 11})
        with mock.patch("osenv.gpu_arch", return_value=None):
            self.assertIsNone(osenv.gpu_info())


class TestOtherGpuVendors(unittest.TestCase):
    """The non-NVIDIA probe. It drives no decision — it exists so install can name the gap out loud."""

    def _fake_drm(self, tmp, vendors):
        for i, v in enumerate(vendors):
            dev = tmp / f"card{i}" / "device"
            dev.mkdir(parents=True)
            (dev / "vendor").write_text(v + "\n", encoding="utf-8")
        return tmp

    def test_reads_pci_vendor_ids(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            drm = self._fake_drm(Path(d), ["0x1002", "0x10de", "0x8086"])
            with mock.patch("osenv.os_name", return_value="linux"), \
                 mock.patch("osenv.Path", side_effect=lambda p: drm if p == "/sys/class/drm" else Path(p)):
                self.assertEqual(osenv.other_gpu_vendors(), ["AMD", "Intel"])   # NVIDIA is not "other"

    def test_no_drm_tree_is_empty_not_an_error(self):
        with mock.patch("osenv.os_name", return_value="linux"), \
             mock.patch("osenv.Path", side_effect=lambda p: Path("/nonexistent-drm")):
            self.assertEqual(osenv.other_gpu_vendors(), [])

    def test_real_host_probe_never_raises(self):
        self.assertIsInstance(osenv.other_gpu_vendors(), list)


class TestRamAndNuma(unittest.TestCase):
    def test_system_ram_from_proc_meminfo(self):
        meminfo = "MemTotal:       32000000 kB\nMemFree: 1 kB\nMemAvailable:   16000000 kB\n"
        with mock.patch("osenv.os_name", return_value="linux"), \
             mock.patch("osenv.Path.read_text", return_value=meminfo):
            r = osenv.system_ram_gb()
            self.assertEqual(r["TotalGB"], round(32000000 / (1024 ** 2)))
            self.assertEqual(r["FreeGB"], round(16000000 / (1024 ** 2)))

    def test_system_ram_none_when_no_memtotal(self):
        with mock.patch("osenv.os_name", return_value="linux"), \
             mock.patch("osenv.Path.read_text", return_value="Bogus: 1 kB\n"):
            self.assertIsNone(osenv.system_ram_gb())

    def test_numa_counts_sys_nodes(self):
        fake = [Path("/sys/devices/system/node/node0"), Path("/sys/devices/system/node/node1"),
                Path("/sys/devices/system/node/cpu")]
        with mock.patch("osenv.os_name", return_value="linux"), \
             mock.patch("osenv.Path.iterdir", return_value=fake):
            self.assertEqual(osenv.numa_node_count(), 2)

    def test_numa_falls_back_to_one(self):
        with mock.patch("osenv.os_name", return_value="linux"), \
             mock.patch("osenv.Path.iterdir", side_effect=OSError):
            self.assertEqual(osenv.numa_node_count(), 1)


class TestLinuxDistroSeams(_ForceOSMixin, unittest.TestCase):
    def test_package_manager_normalizes_apt(self):
        self._force("linux")
        with mock.patch("osenv.shutil.which", side_effect=lambda c: "/usr/bin/apt-get" if c == "apt-get" else None):
            self.assertEqual(osenv.linux_package_manager(), "apt")

    def test_package_manager_pacman(self):
        self._force("linux")
        with mock.patch("osenv.shutil.which", side_effect=lambda c: "/usr/bin/pacman" if c == "pacman" else None):
            self.assertEqual(osenv.linux_package_manager(), "pacman")

    def test_package_manager_none_on_windows(self):
        self._force("windows")
        self.assertIsNone(osenv.linux_package_manager())

    def test_os_family_id_like_derivative(self):
        # CachyOS -> arch via ID_LIKE
        with tempfile.NamedTemporaryFile("w", suffix=".os-release", delete=False) as f:
            f.write('ID=cachyos\nID_LIKE=arch\nVERSION_ID=1\n')
            path = f.name
        try:
            self.assertEqual(osenv.linux_os_family(path), "arch")
        finally:
            os.unlink(path)

    def test_os_family_debian_base(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write('ID=ubuntu\nID_LIKE=debian\n')
            path = f.name
        try:
            self.assertEqual(osenv.linux_os_family(path), "debian")
        finally:
            os.unlink(path)

    def test_os_family_missing_file_is_none(self):
        self.assertIsNone(osenv.linux_os_family("/nonexistent/os-release"))


class TestBuildOutputRollback(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_backup_restore_dir_roundtrip(self):
        d = self.tmp / "bin"
        d.mkdir()
        (d / "llama-server").write_text("v1")
        bak = osenv.backup_build_output(d)
        self.assertEqual(bak, Path(f"{d}.bak"))
        (d / "llama-server").write_text("v2-broken")  # simulate a bad rebuild
        self.assertTrue(osenv.restore_build_output(d))
        self.assertEqual((d / "llama-server").read_text(), "v1")
        self.assertFalse(bak.exists())  # move consumed it

    def test_backup_none_when_absent(self):
        self.assertIsNone(osenv.backup_build_output(self.tmp / "missing"))

    def test_restore_false_when_no_backup(self):
        self.assertFalse(osenv.restore_build_output(self.tmp / "bin"))

    def test_restore_undoes_added_and_removed_entries_without_nesting(self):
        d = self.tmp / "bin"
        d.mkdir()
        (d / "llama-server").write_text("v1")
        (d / "voices").mkdir()
        (d / "voices" / "a.onnx").write_text("voice")
        if sys.platform != "win32":
            os.symlink("llama-server", d / "llama-server-link")
        bak = osenv.backup_build_output(d)
        if sys.platform != "win32":
            self.assertTrue((bak / "llama-server-link").is_symlink())   # snapshot keeps links as links
        (d / "llama-server").write_text("v2-broken")
        (d / "libnew.so.1").write_text("added by the failed update")
        self.assertTrue(osenv.restore_build_output(d, bak))
        self.assertEqual((d / "llama-server").read_text(), "v1")
        self.assertFalse((d / "libnew.so.1").exists())
        self.assertEqual((d / "voices" / "a.onnx").read_text(), "voice")
        self.assertFalse((d / "bin.bak").exists() or (d / bak.name).exists())   # never nested
        self.assertFalse(bak.exists())

    def test_remove_backup_discards(self):
        d = self.tmp / "bin"
        d.mkdir()
        bak = osenv.backup_build_output(d)
        self.assertTrue(bak.exists())
        osenv.remove_build_output_backup(d)
        self.assertFalse(bak.exists())


if __name__ == "__main__":
    unittest.main()
