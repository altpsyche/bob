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


# --- ONE-C §1b seams -----------------------------------------------------------------------------


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
    """Mocked subprocess so the suite never actually pkills anything (a real pkill -f can match the
    test runner itself). Asserts the right command per OS and the killed-names return contract."""

    def test_linux_uses_pkill_f_and_returns_killed(self):
        self._force("linux")
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            rc = 0 if argv[-1] == "llama-swap" else 1  # only llama-swap "matched"
            return types.SimpleNamespace(returncode=rc)

        with mock.patch("osenv.subprocess.run", side_effect=fake_run):
            killed = osenv.stop_processes_by_name(["llama-swap", "open-webui"])
        self.assertEqual(killed, ["llama-swap"])
        self.assertEqual(calls[0], ["pkill", "-f", "llama-swap"])
        self.assertEqual(calls[1], ["pkill", "-f", "open-webui"])

    def test_string_arg_is_treated_as_single_name(self):
        self._force("linux")
        with mock.patch("osenv.subprocess.run",
                        return_value=types.SimpleNamespace(returncode=1)):
            self.assertEqual(osenv.stop_processes_by_name("nothing"), [])

    def test_windows_uses_taskkill_im_exe(self):
        self._force("windows")
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return types.SimpleNamespace(returncode=0)

        with mock.patch("osenv.subprocess.run", side_effect=fake_run):
            killed = osenv.stop_processes_by_name("llama-swap")
        self.assertEqual(killed, ["llama-swap"])
        self.assertEqual(calls[0], ["taskkill", "/IM", "llama-swap.exe", "/F"])


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


def _smi(stdout):
    r = mock.Mock()
    r.stdout = stdout
    r.stderr = ""
    r.returncode = 0
    return r


class TestGpuSeams(unittest.TestCase):
    """ONE-D §1b — gpu_vram_gb / gpu_arch / gpu_info consolidated into osenv (were duped in health/models)."""

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

    def test_remove_backup_discards(self):
        d = self.tmp / "bin"
        d.mkdir()
        bak = osenv.backup_build_output(d)
        self.assertTrue(bak.exists())
        osenv.remove_build_output_backup(d)
        self.assertFalse(bak.exists())


if __name__ == "__main__":
    unittest.main()
