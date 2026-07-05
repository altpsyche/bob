"""NB3 (contracts C3 secrets, C4 data-dir) — the OS-abstraction seam for the Python runtime.

One place that knows about the OS, so the rest of the Python core stays OS-neutral:
  - default_shell()  the agent tool shell, always OS-native (pwsh on Windows, bash/sh elsewhere)
  - data_dir()/cache_dir()  repo-relative data/ + logs/ by default; BOB_DATA_DIR override (C4)
  - secret(name)     env -> OS keychain -> <data_dir>/secrets.json -> default (C3); never a tracked file
  - notify()         WinRT toast on Windows, notify-send elsewhere, no-op if neither

Per-OS branches key off platform.system() so tests can monkeypatch it.
"""
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def os_name() -> str:
    """'windows' | 'linux' | 'macos' — the tri-state OS, mirroring pwsh Get-BobOS. Honors a TEST-ONLY
    BOB_FORCE_OS ('windows'|'linux'|'macos'); anything else warns and is ignored. Replaces the 2-way
    is_windows() for the ONE-C lifecycle/provisioning seams that must branch macOS separately."""
    forced = os.environ.get("BOB_FORCE_OS")
    if forced:
        f = forced.lower()
        if f in ("windows", "linux", "macos"):
            return f
        print(f"BOB_FORCE_OS='{forced}' is not one of windows/linux/macos — ignoring.", file=sys.stderr)
    sysname = platform.system()
    if sysname == "Windows":
        return "windows"
    if sysname == "Darwin":
        return "macos"
    return "linux"


def is_windows() -> bool:
    return os_name() == "windows"


# --- shell (C1: the agent tool shell is OS-native, independent of pwsh-for-orchestration) --------

def default_shell() -> list:
    """Argv prefix for running a command *string* in the OS-native shell.

    Windows -> pwsh (byte-identical to the pre-NB3 hardcode); elsewhere -> bash, falling back to
    sh. Append the command string to run it: subprocess.run(default_shell() + [cmd]).
    """
    if is_windows():
        return ["pwsh", "-NonInteractive", "-Command"]
    shell = shutil.which("bash") or shutil.which("sh") or "sh"
    return [shell, "-c"]


# --- data / state location (C4) ------------------------------------------------------------------

def _repo_data() -> Path:
    return REPO / "data"


def data_dir() -> Path:
    """The directory for state (sessions.db, bob.db, schedules.json, secrets.json).

    C4: repo-relative data/ by default (local-first, zero migration). Only when BOB_DATA_DIR is
    set (a future system-install / multi-user mode) does it move — and existing data/* is copied
    once so nothing is lost.
    """
    override = os.environ.get("BOB_DATA_DIR")
    if not override:
        d = _repo_data()
        d.mkdir(parents=True, exist_ok=True)
        return d
    d = Path(override).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    _migrate_once(_repo_data(), d)
    return d


def cache_dir() -> Path:
    """Log/cache directory: repo-relative logs/ by default, <BOB_DATA_DIR>/logs when overridden."""
    override = os.environ.get("BOB_DATA_DIR")
    d = (Path(override).expanduser() / "logs") if override else (REPO / "logs")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _migrate_once(src: Path, dst: Path) -> None:
    """One-time copy of existing data/* into a freshly-used BOB_DATA_DIR (C4). Marked with a
    .migrated stamp so it never re-copies (and never clobbers newer files in dst)."""
    stamp = dst / ".migrated"
    if stamp.exists() or not src.exists() or src.resolve() == dst.resolve():
        return
    for item in src.iterdir():
        target = dst / item.name
        if target.exists():
            continue
        try:
            if item.is_dir():
                shutil.copytree(item, target)
            else:
                shutil.copy2(item, target)
        except OSError:
            pass  # best-effort migration; a partial copy must not crash startup
    stamp.write_text("", encoding="utf-8")


# --- secrets (C3) --------------------------------------------------------------------------------

def secrets_file() -> Path:
    """The resolved secrets.json path (under data_dir(); data/ is gitignored, so never tracked)."""
    return data_dir() / "secrets.json"


def secret(name: str, default=None, config: dict = None):
    """Resolve a secret by name with precedence env -> OS keychain -> secrets.json -> default (C3).

    Env keys checked: the exact name, then BOB_<UPPER>. Keychain via the optional `keyring`
    package (skipped if not installed). No secret is ever read from a git-tracked file.
    """
    # 1. process env
    val = os.environ.get(name) or os.environ.get("BOB_" + name.upper())
    if val:
        return val
    # 2. OS keychain (Credential Manager / Keychain / secret-tool) — optional dependency
    try:
        import keyring  # type: ignore

        val = keyring.get_password("bob", name)
        if val:
            return val
    except Exception:
        pass  # keyring absent or backend unavailable — fall through to the file
    # 3. <data_dir>/secrets.json (never a tracked file)
    sf = secrets_file()
    if sf.exists():
        try:
            data = json.loads(sf.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get(name):
                return data[name]
        except (json.JSONDecodeError, OSError):
            pass  # a malformed secrets file must not leak or crash — treat as absent
    # 4. default (may be a config-carried reference/value on Windows)
    return default


# --- notifications -------------------------------------------------------------------------------

def notify(title: str, body: str) -> bool:
    """Best-effort desktop notification. Returns True if a backend fired. No-op (False) when none
    is available — e.g. Linux without notify-send, or any headless box."""
    if is_windows():
        return _notify_windows(title, body)
    send = shutil.which("notify-send")
    if send:
        try:
            subprocess.run([send, title, body], check=False, timeout=5)
            return True
        except (OSError, subprocess.SubprocessError):
            return False
    return False


def _notify_windows(title: str, body: str) -> bool:  # pragma: no cover — exercised only on Windows
    try:
        from win10toast import ToastNotifier  # type: ignore

        ToastNotifier().show_toast(title, body, threaded=True)
        return True
    except Exception:
        # WinRT/toast is handled by scripts/bob-toast.ps1 in the PowerShell layer today; the
        # Python seam is a no-op fallback rather than a hard dependency.
        return False


# --- audio I/O (ONE-B3: mic-in / speaker-out seam for the /voice mode + speak) -------------------
# The one place that knows how this OS records the mic and plays a clip, so the voice capability and
# the shell /voice mode stay OS-neutral. sounddevice/numpy are optional and imported lazily (like
# keyring/win10toast) so the base runtime never depends on the audio stack.

_AUDIO_SAMPLE_RATE = 16000   # whisper prefers 16 kHz mono
_AUDIO_CHANNELS = 1


def play_audio(path: str) -> bool:
    """Play a WAV file through the OS. Windows -> winsound; macOS -> afplay; Linux -> first of
    paplay/aplay/ffplay. Returns True if a backend played it, False when none is available (the
    caller can then point the user at the file). Replaces the inline pwsh SoundPlayer/paplay branch."""
    if is_windows():
        try:
            import winsound  # type: ignore

            winsound.PlaySound(path, winsound.SND_FILENAME)
            return True
        except Exception:
            return False
    if platform.system() == "Darwin":
        exe = shutil.which("afplay")
        if not exe:
            return False
        subprocess.run([exe, path], check=False)
        return True
    for name in ("paplay", "aplay", "ffplay"):
        exe = shutil.which(name)
        if not exe:
            continue
        argv = ([exe, "-nodisp", "-autoexit", "-loglevel", "quiet", path]
                if name == "ffplay" else [exe, path])
        subprocess.run(argv, check=False)
        return True
    return False


def _pcm_to_wav(pcm_bytes: bytes) -> bytes:
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(_AUDIO_CHANNELS)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(_AUDIO_SAMPLE_RATE)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


def record_audio(silence_sec: float = 1.5, rms_silence: int = 200) -> bytes:
    """Record the mic until `silence_sec` of continuous silence; return 16 kHz mono WAV bytes (b'' if
    nothing was captured). Discards leading silence so recording starts when speech does. Cross-platform
    via sounddevice (PortAudio). Raises RuntimeError if the audio stack isn't installed — single source
    of the RMS-silence capture used by `bob listen`/`transcribe` and the /voice mode."""
    try:
        import numpy as np
        import sounddevice as sd
    except ImportError as e:
        raise RuntimeError("mic capture needs sounddevice + numpy (pip install sounddevice numpy)") from e
    chunk_secs = 0.1
    silence_chunks = int(silence_sec / chunk_secs)
    chunk_samples = int(_AUDIO_SAMPLE_RATE * chunk_secs)
    frames, consecutive_silence, started = [], 0, False
    with sd.InputStream(samplerate=_AUDIO_SAMPLE_RATE, channels=_AUDIO_CHANNELS, dtype="int16") as stream:
        while True:
            data, _ = stream.read(chunk_samples)
            rms = np.sqrt(np.mean(data.astype(np.float32) ** 2))
            if rms > rms_silence:
                started, consecutive_silence = True, 0
                frames.append(data.copy())
            elif started:
                consecutive_silence += 1
                frames.append(data.copy())
                if consecutive_silence >= silence_chunks:
                    break
    if not frames:
        return b""
    return _pcm_to_wav(np.concatenate(frames, axis=0).tobytes())


# --- process + service lifecycle (ONE-C §1b) -----------------------------------------------------
# The OS core of every lifecycle/provisioning port. Low-level primitives live here; the orchestration
# (pidfile read/write, readiness polling) sits above in scripts/tools/stack.py. Mirrors the pwsh split
# (Test-PortInUse/Stop-ProcessTree/Start-BobBackgroundProcess) and keeps the BOB_FORCE_OS test hook.
# psutil is an OPTIONAL accelerator (imported lazily like keyring/sounddevice); every function has an
# stdlib fallback so the base runtime never hard-depends on it. Best-effort: a dead PID is not an error.


def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """True if something accepts a TCP connection on host:port (a service is up). Port of pwsh
    Test-PortInUse — identical on both OSes."""
    import socket

    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def pid_alive(pid: int) -> bool:
    """True if a process with this PID exists. psutil if present, else os.kill(pid, 0) on POSIX / an
    OpenProcess probe on Windows."""
    if not pid or pid <= 0:
        return False
    try:
        import psutil  # type: ignore

        if not psutil.pid_exists(pid):
            return False
        try:  # a reaped-but-not-yet-collected zombie is not "alive"
            return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False
    except ImportError:
        pass
    if os_name() == "windows":  # pragma: no cover — Windows-only fallback when psutil is absent
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    if os_name() == "linux":
        # A killed-but-unreaped child lingers as a zombie whose PID os.kill(pid,0) still accepts;
        # /proc reports state 'Z'. Treat defunct as dead so stop_process_tree reads as effective.
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
                state = f.read().rsplit(")", 1)[1].split()[0]
            if state == "Z":
                return False
        except (OSError, IndexError):
            pass
    return True


def process_stats(pid: int):
    """{'rss_mb': int, 'uptime': 'H:MM:SS'} for a live PID, or None if dead/unknown. Powers `bob ps`.
    psutil if present; else /proc on Linux; else (macOS/Windows w/o psutil) rss/uptime are None but a
    live process still reports {'rss_mb': None, 'uptime': None}."""
    if not pid or pid <= 0 or not pid_alive(pid):
        return None
    try:
        import psutil  # type: ignore

        p = psutil.Process(pid)
        import time
        return {"rss_mb": int(p.memory_info().rss / (1024 * 1024)),
                "uptime": _fmt_uptime(time.time() - p.create_time())}
    except ImportError:
        pass
    except Exception:
        return None
    if os_name() == "linux":
        try:
            rss_mb = None
            with open(f"/proc/{pid}/status", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        rss_mb = int(int(line.split()[1]) / 1024)  # kB -> MB
                        break
            with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
                starttime_ticks = int(f.read().rsplit(")", 1)[1].split()[19])  # field 22 (0-based 19 after comm)
            with open("/proc/uptime", encoding="utf-8") as f:
                sys_uptime = float(f.read().split()[0])
            hz = os.sysconf("SC_CLK_TCK")
            proc_uptime = sys_uptime - (starttime_ticks / hz)
            return {"rss_mb": rss_mb, "uptime": _fmt_uptime(proc_uptime)}
        except (OSError, IndexError, ValueError):
            return {"rss_mb": None, "uptime": None}
    return {"rss_mb": None, "uptime": None}  # pragma: no cover — macOS/Windows without psutil


def _fmt_uptime(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def stop_process_tree(pid: int) -> None:
    """Terminate a process and its children (uvicorn workers, piper's native child, …). Port of pwsh
    Stop-ProcessTree: Windows reaps children via psutil/taskkill; POSIX prefers a process-GROUP kill
    (works when start_detached made the PID a group leader) then belt-and-suspenders pkill -P + kill.
    Best-effort — a dead PID never raises."""
    if not pid or pid <= 0:
        return
    if os_name() == "windows":  # pragma: no cover — exercised only on Windows
        try:
            import psutil  # type: ignore

            proc = psutil.Process(pid)
            for child in proc.children(recursive=True):
                child.terminate()
            proc.terminate()
            return
        except ImportError:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                           check=False, capture_output=True)
            return
        except Exception:
            return
    # POSIX: group kill first, then children by parent, then the parent itself.
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    if shutil.which("pkill"):
        subprocess.run(["pkill", "-P", str(pid)], check=False, capture_output=True)
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def stop_processes_by_name(names) -> list:
    """Kill processes by executable name — the way to reap C++ daemons (llama-swap/llama-server/
    whisper-server/open-webui) that survive a stale pidfile. Windows: taskkill /IM <name>.exe /F;
    POSIX: pkill -f <name>. Returns the names for which a live process was actually killed."""
    if isinstance(names, str):
        names = [names]
    killed = []
    win = os_name() == "windows"
    for name in names:
        if win:  # pragma: no cover — exercised only on Windows
            proc = subprocess.run(["taskkill", "/IM", exe_name(name), "/F"],
                                  check=False, capture_output=True)
        else:
            proc = subprocess.run(["pkill", "-f", name], check=False, capture_output=True)
        if proc.returncode == 0:  # pkill/taskkill exit 0 only when a match was terminated
            killed.append(name)
    return killed


def start_detached(argv: list, pidfile=None, log_path=None, env: dict = None) -> int:
    """Launch a fully-detached background process and return its PID. POSIX uses start_new_session=True
    (setsid) so the child is its own group leader — REQUIRED so stop_process_tree's killpg reaps the
    whole tree. Windows uses DETACHED_PROCESS|CREATE_NO_WINDOW (no console window, survives the parent).
    Writes the PID to `pidfile` when given. When `log_path` is set, the child's stdout+stderr are
    redirected there (truncated per launch — one clean log per run, replacing the pwsh `Tee-Object`
    wrapper), else discarded. `env` overrides/extends the process environment. Port of pwsh
    Start-BobBackgroundProcess — but launches the target binary DIRECTLY, no pwsh shell in between."""
    if log_path is not None:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        out = open(log_path, "wb")  # truncate: one clean log per run
        stdout, stderr = out, subprocess.STDOUT
    else:
        out, stdout, stderr = None, subprocess.DEVNULL, subprocess.DEVNULL
    proc_env = {**os.environ, **env} if env else None
    try:
        if os_name() == "windows":  # pragma: no cover — exercised only on Windows
            DETACHED_PROCESS = 0x00000008
            CREATE_NO_WINDOW = 0x08000000
            proc = subprocess.Popen(argv, creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW,
                                    close_fds=True, stdout=stdout, stderr=stderr, env=proc_env)
        else:
            proc = subprocess.Popen(argv, start_new_session=True, close_fds=True,
                                    stdout=stdout, stderr=stderr, env=proc_env)
    finally:
        if out is not None:
            out.close()  # the child holds its own dup'd fd; our copy isn't needed
    if pidfile is not None:
        Path(pidfile).write_text(str(proc.pid), encoding="utf-8")
    return proc.pid


# --- executable + path resolvers (ONE-C §1b) -----------------------------------------------------
# Where binaries and per-app config live, OS-aware. Mirror pwsh Get-BobExeName/Get-VenvExe/Get-BinExe/
# Get-HomeConfigDir so a capability that shells out to a staged binary resolves the same path in both
# languages. All key off os_name() so BOB_FORCE_OS drives them in tests.


def exe_name(base: str) -> str:
    """'llama-server' -> 'llama-server.exe' on Windows, bare elsewhere."""
    return f"{base}.exe" if os_name() == "windows" else base


def venv_exe(venv: str, exe: str) -> Path:
    """Absolute path to a console script in a repo venv: tools/<venv>/Scripts/<exe>.exe on Windows,
    tools/<venv>/bin/<exe> on POSIX (where `python -m venv` puts scripts)."""
    base = REPO / "tools" / venv
    if os_name() == "windows":
        return base / "Scripts" / f"{exe}.exe"
    return base / "bin" / exe


def bin_exe(base: str) -> Path:
    """Absolute path to a native binary staged in repo bin/ (adds .exe on Windows)."""
    return REPO / "bin" / exe_name(base)


def home_config_dir(app: str) -> Path:
    """Per-app config dir: %USERPROFILE%\\.config\\<app> on Windows; $XDG_CONFIG_HOME/<app> (or
    ~/.config/<app>) on POSIX."""
    if os_name() == "windows":
        return Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".config" / app
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / app


# --- open a URL (ONE-C §1b) ----------------------------------------------------------------------


def open_url(url: str) -> bool:
    """Open a URL in the user's default browser (e.g. `up` opens the WebUI). POSIX: xdg-open / open;
    Windows: webbrowser (os.startfile-backed). Returns True if a launcher fired, False on a headless
    box with no opener."""
    name = os_name()
    if name == "windows":  # pragma: no cover — exercised only on Windows
        import webbrowser

        return webbrowser.open(url)
    opener = "open" if name == "macos" else "xdg-open"
    exe = shutil.which(opener)
    if not exe:
        return False
    try:
        subprocess.Popen([exe, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False
