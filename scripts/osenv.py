"""The OS-abstraction seam for the Python runtime.

One place that knows about the OS, so the rest of the Python core stays OS-neutral:
  - default_shell()  the agent tool shell, always OS-native (pwsh on Windows, bash/sh elsewhere)
  - data_dir()/cache_dir()  repo-relative data/ + logs/ by default; BOB_DATA_DIR override
  - secret(name)     env -> OS keychain -> <data_dir>/secrets.json -> default; never a tracked file
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
    """'windows' | 'linux' | 'macos' — the tri-state OS. Honors a TEST-ONLY
    BOB_FORCE_OS ('windows'|'linux'|'macos'); anything else warns and is ignored. Replaces the 2-way
    is_windows() for the lifecycle/provisioning seams that must branch macOS separately."""
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


# --- shell (the agent tool shell is OS-native) --------

def default_shell() -> list:
    """Argv prefix for running a command *string* in the OS-native shell.

    Windows -> pwsh (the previously hardcoded default); elsewhere -> bash, falling back to
    sh. Append the command string to run it: subprocess.run(default_shell() + [cmd]).
    """
    if is_windows():
        return ["pwsh", "-NonInteractive", "-Command"]
    shell = shutil.which("bash") or shutil.which("sh") or "sh"
    return [shell, "-c"]


# --- desktop input (computer-use) : the ONE OS seam for screen input injection ----------
# Backends are chosen by shutil.which, mirroring bob_vision._LINUX_CAPTURE. Coordinates are REAL screen
# pixels; the caller maps the model's image-space coordinates to screen pixels first (see bob_vision).
# Every entry point raises RuntimeError with an install hint when no backend is available, so the
# computer-use tool degrades gracefully rather than silently no-op'ing.

def is_wayland() -> bool:
    """True on a native Wayland session (synthetic input needs ydotool/uinput or wtype, not xdotool).
    Env-driven so BOB_FORCE_* style overrides work in tests."""
    if os.environ.get("WAYLAND_DISPLAY"):
        return True
    return os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"


def is_x11() -> bool:
    """True on an X11 session (xdotool drives it directly)."""
    if os.environ.get("XDG_SESSION_TYPE", "").lower() == "x11":
        return True
    return bool(os.environ.get("DISPLAY")) and not is_wayland()


def _input_backend():
    """Pick the Linux input backend by session type + availability. Prefers the tool that matches the
    session (xdotool on X11, ydotool/wtype on Wayland) but falls back to whatever is installed. Returns
    a backend name or None when none is present."""
    if is_wayland():
        order = ("ydotool", "wtype", "xdotool")   # xdotool only reaches XWayland apps, last resort
    else:
        order = ("xdotool", "ydotool", "wtype")
    for tool in order:
        if shutil.which(tool):
            return tool
    return None


_XDOTOOL_BUTTON = {"left": 1, "middle": 2, "right": 3}
_YDOTOOL_BUTTON = {"left": "0xC0", "middle": "0xC2", "right": "0xC1"}


def _linux_input_commands(backend: str, action: str, **kw) -> list:
    """Build the argv command(s) for one input action on a Linux backend. Returns a list of argv lists
    (a click is move-then-click on ydotool). Raises RuntimeError for a capability the backend lacks."""
    if backend == "xdotool":
        if action == "move":
            return [["xdotool", "mousemove", "--sync", str(kw["x"]), str(kw["y"])]]
        if action == "click":
            b = _XDOTOOL_BUTTON.get(kw.get("button", "left"), 1)
            return [["xdotool", "mousemove", "--sync", str(kw["x"]), str(kw["y"]), "click", str(b)]]
        if action == "type":
            return [["xdotool", "type", "--clearmodifiers", "--", kw["text"]]]
        if action == "key":
            return [["xdotool", "key", "--", kw["keys"]]]
        if action == "scroll":
            dy, dx = int(kw.get("dy", 0)), int(kw.get("dx", 0))
            cmds = []
            if dy:
                cmds.append(["xdotool", "click", "--repeat", str(abs(dy)), "5" if dy > 0 else "4"])
            if dx:
                cmds.append(["xdotool", "click", "--repeat", str(abs(dx)), "7" if dx > 0 else "6"])
            return cmds or [["xdotool", "click", "5"]]
    if backend == "ydotool":
        if action == "move":
            return [["ydotool", "mousemove", "-a", "-x", str(kw["x"]), "-y", str(kw["y"])]]
        if action == "click":
            code = _YDOTOOL_BUTTON.get(kw.get("button", "left"), "0xC0")
            return [["ydotool", "mousemove", "-a", "-x", str(kw["x"]), "-y", str(kw["y"])],
                    ["ydotool", "click", code]]
        if action == "type":
            return [["ydotool", "type", "--", kw["text"]]]
        if action == "key":
            return [["ydotool", "key", "--", kw["keys"]]]
        if action == "scroll":
            return [["ydotool", "mousemove", "-w", "-x", str(int(kw.get("dx", 0))),
                     "-y", str(int(kw.get("dy", 0)))]]
    if backend == "wtype":
        if action == "type":
            return [["wtype", kw["text"]]]
        if action == "key":
            return [["wtype", "-k", kw["keys"]]]
        raise RuntimeError("wtype drives only keyboard input on Wayland; install ydotool for mouse actions")
    raise RuntimeError(f"input backend {backend!r} cannot perform {action!r}")


def _run_input(action: str, **kw) -> None:
    name = os_name()
    if name == "macos":
        raise RuntimeError("computer-use input is not yet supported on macOS")
    if name == "windows":  # pragma: no cover — exercised only on Windows
        return _windows_input(action, **kw)
    backend = _input_backend()
    if backend is None:
        raise RuntimeError("no desktop input backend found — install xdotool (X11) or ydotool (Wayland)")
    for argv in _linux_input_commands(backend, action, **kw):
        subprocess.run(argv, check=True)


def _windows_input(action: str, **kw) -> None:  # pragma: no cover — Windows-only
    try:
        import pyautogui
    except ImportError as e:
        raise RuntimeError("computer-use input on Windows needs pyautogui (pip install pyautogui)") from e
    if action == "move":
        pyautogui.moveTo(kw["x"], kw["y"])
    elif action == "click":
        pyautogui.click(kw["x"], kw["y"], button=kw.get("button", "left"))
    elif action == "type":
        pyautogui.typewrite(kw["text"])
    elif action == "key":
        pyautogui.hotkey(*str(kw["keys"]).split("+"))
    elif action == "scroll":
        pyautogui.scroll(int(kw.get("dy", 0)))


def computer_display(mode: str = "virtual"):
    """Resolve which X display computer-use should drive. 'host' returns the current session's $DISPLAY
    (the real logged-in desktop -- highest risk). 'virtual' returns a dedicated Xvfb/nested display from
    $BOB_VIRTUAL_DISPLAY when one is provisioned, else None so the caller reports it rather than silently
    falling back to the host session. Returning the display string lets the tool run input under it
    (DISPLAY=<value>); a virtual display also neutralizes the Wayland synthetic-input block."""
    if mode == "host":
        return os.environ.get("DISPLAY")
    return os.environ.get("BOB_VIRTUAL_DISPLAY")


def input_move(x: int, y: int) -> None:
    """Move the pointer to (x, y) in real screen pixels."""
    _run_input("move", x=x, y=y)


def input_click(x: int, y: int, button: str = "left") -> None:
    """Click at (x, y) in real screen pixels (button: left|middle|right)."""
    _run_input("click", x=x, y=y, button=button)


def input_type(text: str) -> None:
    """Type literal text at the current focus."""
    _run_input("type", text=text)


def input_key(keys: str) -> None:
    """Press a key or chord, e.g. 'Return' or 'ctrl+c'."""
    _run_input("key", keys=keys)


def input_scroll(dx: int = 0, dy: int = 0) -> None:
    """Scroll by (dx, dy) steps; positive dy scrolls down."""
    _run_input("scroll", dx=dx, dy=dy)


# --- data / state location ------------------------------------------------------------------

def _repo_data() -> Path:
    return REPO / "data"


def data_dir() -> Path:
    """The directory for state (sessions.db, bob.db, schedules.json, secrets.json).

    Repo-relative data/ by default (local-first, zero migration). Only when BOB_DATA_DIR is
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
    """One-time copy of existing data/* into a freshly-used BOB_DATA_DIR. Marked with a
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


# --- secrets --------------------------------------------------------------------------------

def secrets_file() -> Path:
    """The resolved secrets.json path (under data_dir(); data/ is gitignored, so never tracked)."""
    return data_dir() / "secrets.json"


def secret(name: str, default=None, config: dict = None):
    """Resolve a secret by name with precedence env -> OS keychain -> secrets.json -> default.

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
        # No win10toast (optional) — the Python seam is a no-op fallback, not a hard dependency.
        return False


# --- audio I/O (mic-in / speaker-out seam for the /voice mode + speak) -------------------
# The one place that knows how this OS records the mic and plays a clip, so the voice capability and
# the shell /voice mode stay OS-neutral. sounddevice/numpy are optional and imported lazily (like
# keyring/win10toast) so the base runtime never depends on the audio stack.

_AUDIO_SAMPLE_RATE = 16000   # whisper prefers 16 kHz mono
_AUDIO_CHANNELS = 1


def play_audio(path: str) -> bool:
    """Play a WAV file through the OS. Windows -> winsound; macOS -> afplay; Linux -> first of
    paplay/aplay/ffplay. Returns True if a backend played it, False when none is available (the
    caller can then point the user at the file)."""
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


def record_audio(silence_sec: float = 1.5, rms_silence: int = 200,
                 max_wait_sec: float = 10.0, max_record_sec: float = 30.0,
                 silence_ratio: float = 0.25) -> bytes:
    """Record the mic until `silence_sec` of continuous silence; return 16 kHz mono WAV bytes (b'' if
    nothing was captured). Discards leading silence so recording starts when speech does. Cross-platform
    via sounddevice (PortAudio). Raises RuntimeError if the audio stack isn't installed — single source
    of the RMS-silence capture used by `bob listen`/`transcribe` and the /voice mode.

    Silence is detected RELATIVE to the loudest speech seen (rms < max(rms_silence, peak*silence_ratio)),
    so a mic whose ambient noise sits above the fixed `rms_silence` floor still detects the pause instead
    of recording to the cap. `rms_silence` is the floor for a quiet mic; raise it for a noisy one.

    Two guards keep it from hanging forever (the "I speak and nothing happens" bug): if no speech clears
    the floor within `max_wait_sec` (wrong/quiet input device, low gain) it returns b'' instead of
    looping forever, and once speaking it stops after `max_record_sec` even if silence never lands."""
    try:
        import numpy as np
        import sounddevice as sd
    except ImportError as e:
        raise RuntimeError("mic capture needs sounddevice + numpy (pip install sounddevice numpy)") from e
    chunk_secs = 0.1
    silence_chunks = int(silence_sec / chunk_secs)
    max_wait_chunks = int(max_wait_sec / chunk_secs)      # bail if speech never starts
    max_record_chunks = int(max_record_sec / chunk_secs)  # hard cap on a single utterance
    chunk_samples = int(_AUDIO_SAMPLE_RATE * chunk_secs)
    frames, consecutive_silence, started, elapsed, peak = [], 0, False, 0, 0.0
    with sd.InputStream(samplerate=_AUDIO_SAMPLE_RATE, channels=_AUDIO_CHANNELS, dtype="int16") as stream:
        while True:
            data, _ = stream.read(chunk_samples)
            elapsed += 1
            rms = float(np.sqrt(np.mean(data.astype(np.float32) ** 2)))
            if not started:
                if rms > rms_silence:
                    started, peak = True, rms
                    frames.append(data.copy())
                elif elapsed >= max_wait_chunks:
                    return b""                # no speech detected in max_wait — don't hang
                continue
            # Speaking: track the peak and call silence RELATIVE to it, so ambient above the floor
            # still ends the turn shortly after the pause (was: kept recording to max_record_sec).
            peak = max(peak, rms)
            frames.append(data.copy())
            if rms < max(rms_silence, peak * silence_ratio):
                consecutive_silence += 1
                if consecutive_silence >= silence_chunks:
                    break
            else:
                consecutive_silence = 0
            if elapsed >= max_record_chunks:
                break                         # utterance ran past the cap — stop
    if not frames:
        return b""
    return _pcm_to_wav(np.concatenate(frames, axis=0).tobytes())


# --- process + service lifecycle -----------------------------------------------------
# The OS core of every lifecycle/provisioning primitive. Low-level primitives live here; the orchestration
# (pidfile read/write, readiness polling) sits above in scripts/tools/stack.py, and keeps the
# BOB_FORCE_OS test hook.
# psutil is an OPTIONAL accelerator (imported lazily like keyring/sounddevice); every function has an
# stdlib fallback so the base runtime never hard-depends on it. Best-effort: a dead PID is not an error.


def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """True if something accepts a TCP connection on host:port (a service is up). Identical on both OSes."""
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
    """Terminate a process and its children (uvicorn workers, piper's native child, …).
    Windows reaps children via psutil/taskkill; POSIX prefers a process-GROUP kill
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


def start_detached(argv: list, pidfile=None, log_path=None, env: dict = None, append: bool = False) -> int:
    """Launch a fully-detached background process and return its PID. POSIX uses start_new_session=True
    (setsid) so the child is its own group leader — REQUIRED so stop_process_tree's killpg reaps the
    whole tree. Windows uses DETACHED_PROCESS|CREATE_NO_WINDOW (no console window, survives the parent).
    Writes the PID to `pidfile` when given. When `log_path` is set, the child's stdout+stderr are
    redirected there — truncated per launch by default (one clean log per run), or appended when
    `append` is set (a resumable task keeps one growing log across relaunches) — else discarded. `env`
    overrides/extends the process environment. Launches the target binary DIRECTLY, with no
    shell wrapper in between."""
    if log_path is not None:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        out = open(log_path, "ab" if append else "wb")  # append keeps one log across a task's relaunches
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


# --- executable + path resolvers -----------------------------------------------------
# Where binaries and per-app config live, OS-aware, so a capability that shells out to a staged
# binary resolves the same path everywhere. All key off os_name() so BOB_FORCE_OS drives them in tests.


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


# --- docker (optional, for the compose services) -------------------------------------------------


def docker_present() -> bool:
    """True if a `docker` CLI is on PATH. The compose services (SearXNG / n8n / langfuse) need it."""
    return bool(shutil.which("docker"))


def docker_install_hint() -> str:
    """OS-appropriate one-liner for installing Docker (shown by status/doctor when it's absent)."""
    name = os_name()
    if name == "macos":
        return "install Docker Desktop, then re-run bob setup"
    if name == "windows":
        return "install Docker Desktop (WSL2 backend), then re-run bob setup"
    return "install your distro's docker package (with the compose plugin), then re-run bob setup"


# --- open a URL ----------------------------------------------------------------------


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


# --- agent scheduler quartet -----------------------------------------------------
# The OS task that ticks every minute and fires the Python runner (scripts/bob_agent_runner.py); the
# runner evaluates the per-schedule cron expressions itself (schedule.cron_due). Windows uses
# schtasks.exe (the ScheduledTasks cmdlets aren't callable from Python); POSIX uses an idempotent
# crontab edit tagged "# <task_name>".


def crontab_available() -> bool:
    """True when a `crontab` binary is on PATH. Minimal installs (e.g. CachyOS) ship none, so callers
    guard: status/unregister no-op, register raises a clear error."""
    return bool(shutil.which("crontab"))


def agent_task_spec(python_exe: str, script_path: str, task_name: str = "BobAgent", os: str = None) -> dict:
    """PURE. The every-minute registration spec for the OS (no side effects). Windows -> schtasks kind;
    POSIX -> a cron line whose trailing '# <task_name>' tag is the removal/detection key."""
    os = os or os_name()
    if os == "windows":
        return {
            "kind": "schtasks", "name": task_name, "execute": python_exe,
            "argument": f'"{script_path}"', "interval_minutes": 1, "time_limit_minutes": 5,
            "command": f'"{python_exe}" "{script_path}"',
        }
    return {
        "kind": "cron", "name": task_name,
        "crontab": f'* * * * * "{python_exe}" "{script_path}" # {task_name}',
    }


def _crontab_lines() -> list:
    """Current crontab entries as a list of lines ([] if none / no crontab installed)."""
    proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return []
    return [ln for ln in proc.stdout.splitlines()]


def _crontab_write(lines: list) -> None:
    """Replace the crontab with `lines` (piped to `crontab -`)."""
    payload = "\n".join(ln for ln in lines if ln != "")
    subprocess.run(["crontab", "-"], input=(payload + "\n") if payload else "", text=True, check=False)


def register_agent_task(python_exe: str, script_path: str, task_name: str = "BobAgent") -> None:
    """Register the every-minute OS task firing `python_exe script_path`. Idempotent. Windows: schtasks
    /Create /F (replaces). POSIX: rewrite the crontab minus any prior tagged line, plus the new one;
    warn if no cron daemon is active (the entry is written but never fires)."""
    spec = agent_task_spec(python_exe, script_path, task_name)
    if spec["kind"] == "schtasks":  # pragma: no cover — exercised only on Windows
        subprocess.run(["schtasks", "/Create", "/SC", "MINUTE", "/MO", "1", "/TN", task_name,
                        "/TR", spec["command"], "/F"], check=True)
        return
    if not crontab_available():
        raise RuntimeError(
            "cron not found — the Linux agent scheduler needs a 'crontab' binary. Install it "
            "(Arch: 'sudo pacman -S cronie' + 'sudo systemctl enable --now cronie'; Debian/Ubuntu: "
            "'sudo apt-get install -y cron'), then re-run 'bob agent install'.")
    kept = [ln for ln in _crontab_lines() if not ln.rstrip().endswith(f"# {task_name}")]
    _crontab_write(kept + [spec["crontab"]])
    if shutil.which("systemctl"):
        active = [subprocess.run(["systemctl", "is-active", d], capture_output=True, text=True,
                                 check=False).stdout.strip() for d in ("cronie", "cron", "crond")]
        if "active" not in active:
            print("cron entry written, but no cron daemon appears to be running — scheduled agents "
                  "won't fire. Enable it, e.g.: sudo systemctl enable --now cronie (Arch/Fedora) or "
                  "cron (Debian/Ubuntu).", file=sys.stderr)


def unregister_agent_task(task_name: str = "BobAgent") -> None:
    """Remove the OS task. Windows: schtasks /Delete /F. POSIX: rewrite the crontab minus the tagged
    line (no-op if no crontab is installed)."""
    if os_name() == "windows":  # pragma: no cover — exercised only on Windows
        subprocess.run(["schtasks", "/Delete", "/TN", task_name, "/F"], check=False, capture_output=True)
        return
    if not crontab_available():
        return
    _crontab_write([ln for ln in _crontab_lines() if not ln.rstrip().endswith(f"# {task_name}")])


def agent_task_status(task_name: str = "BobAgent") -> dict:
    """{'registered': bool, 'state': str|None, 'next_run': str|None}. Windows queries schtasks; POSIX
    greps the tagged crontab line."""
    if os_name() == "windows":  # pragma: no cover — exercised only on Windows
        proc = subprocess.run(["schtasks", "/Query", "/TN", task_name, "/FO", "LIST"],
                              capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            return {"registered": False, "state": None, "next_run": None}
        state = next_run = None
        for line in proc.stdout.splitlines():
            if line.lower().startswith("status:"):
                state = line.split(":", 1)[1].strip()
            elif line.lower().startswith("next run time:"):
                next_run = line.split(":", 1)[1].strip()
        return {"registered": True, "state": state or "Ready", "next_run": next_run}
    if not crontab_available():
        return {"registered": False, "state": None, "next_run": None}
    line = next((ln for ln in _crontab_lines() if ln.rstrip().endswith(f"# {task_name}")), None)
    return {"registered": bool(line), "state": "Ready" if line else None, "next_run": None}


# --- hardware + build-time discovery -------------------------------------------------
# The build-time / deep-OS-discovery family. GPU probes are pure nvidia-smi (no OS branch); RAM/NUMA
# and the package-manager helpers fork by OS. Consolidated here so tools/health.py + tools/models.py
# (which each carried a copy of gpu_arch / gpu_vram_gb) delegate to one source.

def gpu_vram_gb():
    """Total VRAM of GPU 0 in whole GB via nvidia-smi, or None (no GPU / nvidia-smi absent).
    Cross-platform — pure nvidia-smi, no OS branch."""
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10)
        first = out.stdout.strip().splitlines()[0].strip() if out.stdout.strip() else ""
        if first.isdigit():
            return round(int(first) / 1024)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        pass
    return None


def gpu_arch():
    """{'CudaArch': int, 'Gen': str, 'MinCudaMajor': int} for GPU 0, or None.
    nvidia-smi compute_cap ('8.9'->89, '12.0'->120) mapped to a generation name.
    Cross-platform — pure nvidia-smi, no OS branch."""
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=10)
        cap = out.stdout.strip().splitlines()[0].strip() if out.stdout.strip() else ""
        parts = cap.split(".")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            return None
        arch = int(parts[0]) * 10 + int(parts[1])  # "8.9" -> 89, "12.0" -> 120
        if arch >= 120:
            gen = "Blackwell"
        elif arch >= 89:
            gen = "Ada Lovelace"
        elif arch >= 80:
            gen = "Ampere"
        elif arch >= 75:
            gen = "Turing"
        else:
            gen = f"sm_{arch}"
        return {"CudaArch": arch, "Gen": gen, "MinCudaMajor": 12 if arch >= 120 else 11}
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def gpu_info():
    """Unified GPU probe → {'VramGB', 'CudaArch', 'Gen', 'MinCudaMajor'} for GPU 0, or None.
    Composes gpu_arch + gpu_vram_gb; None when no NVIDIA GPU."""
    arch = gpu_arch()
    if not arch:
        return None
    return {"VramGB": gpu_vram_gb(), **arch}


def system_ram_gb():
    """Physical RAM {'TotalGB': int, 'FreeGB': int|None} or None on failure.
    Windows via GlobalMemoryStatusEx (ctypes), Linux via /proc/meminfo
    (MemTotal / MemAvailable, kB -> GB)."""
    if os_name() == "windows":  # pragma: no cover — exercised only on Windows
        import ctypes

        class _MemStatusEx(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        try:
            stat = _MemStatusEx()
            stat.dwLength = ctypes.sizeof(_MemStatusEx)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return None
            return {"TotalGB": round(stat.ullTotalPhys / (1024 ** 3)),
                    "FreeGB": round(stat.ullAvailPhys / (1024 ** 3))}
        except (OSError, AttributeError):
            return None
    try:
        lines = Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    mi = {}
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and parts[0] in ("MemTotal:", "MemAvailable:"):
            try:
                mi[parts[0].rstrip(":")] = int(parts[1])  # kB
            except ValueError:
                pass
    if "MemTotal" not in mi:
        return None
    avail = mi.get("MemAvailable")
    return {"TotalGB": round(mi["MemTotal"] / (1024 ** 2)),                 # kB / 1024^2 = GB
            "FreeGB": round(avail / (1024 ** 2)) if avail is not None else None}


def numa_node_count() -> int:
    """Number of NUMA nodes (>= 1, falls back to 1). Windows via
    GetNumaHighestNodeNumber (ctypes), Linux counts /sys/devices/system/node/node*."""
    try:
        if os_name() == "windows":  # pragma: no cover — exercised only on Windows
            import ctypes
            n = ctypes.c_ulong(0)
            if ctypes.windll.kernel32.GetNumaHighestNodeNumber(ctypes.byref(n)):
                return int(n.value) + 1
            return 1
        import re
        node_dir = Path("/sys/devices/system/node")
        nodes = [p for p in node_dir.iterdir() if re.match(r"^node\d+$", p.name)]
        return len(nodes) if nodes else 1
    except (OSError, AttributeError):
        return 1


_LINUX_PKG_MANAGERS = (("apt-get", "apt"), ("dnf", "dnf"), ("pacman", "pacman"), ("zypper", "zypper"))


def is_atomic_linux() -> bool:
    """True on an rpm-ostree / atomic-Fedora host (Silverblue/Kinoite/Bazzite/uBlue). /run/ostree-booted
    is the canonical marker that the running system is an ostree deployment (read-only /usr; packages are
    LAYERED via rpm-ostree and apply on the next boot)."""
    return os_name() == "linux" and Path("/run/ostree-booted").exists() and shutil.which("rpm-ostree") is not None


def linux_package_manager():
    """Normalized Linux package manager: 'rpm-ostree' on an atomic host (checked first — an atomic box may
    also carry a dnf that doesn't persist), else the first of apt/dnf/pacman/zypper on PATH, or None
    (with atomic-update support). None on non-Linux."""
    if os_name() != "linux":
        return None
    if is_atomic_linux():
        return "rpm-ostree"
    for cmd, name in _LINUX_PKG_MANAGERS:
        if shutil.which(cmd):
            return name
    return None


def linux_os_family(os_release_path: str = "/etc/os-release"):
    """Distro family ('debian'|'rhel'|'arch'|'suse'|<ID>|None) from /etc/os-release.
    Reads ID then ID_LIKE so derivatives resolve to their base (CachyOS/Manjaro->arch,
    Mint/Pop->debian, Rocky/Alma->rhel)."""
    import re

    p = Path(os_release_path)
    if not p.exists():
        return None
    kv = {}
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            m = re.match(r'^\s*(ID|ID_LIKE|VERSION_ID)\s*=\s*"?([^"]*)"?\s*$', line)
            if m:
                kv[m.group(1)] = m.group(2)
    except OSError:
        return None
    tokens = [kv.get("ID", "")] + (kv.get("ID_LIKE", "") or "").split()
    for t in tokens:
        if re.match(r"^(debian|ubuntu)$", t):
            return "debian"
        if re.match(r"^(rhel|fedora|centos)$", t):
            return "rhel"
        if t == "arch":
            return "arch"
        if re.match(r"^(suse|opensuse.*|sles)$", t):
            return "suse"
    return kv.get("ID") or None


# --- build-output rollback (used by update) -----------------------------------------------
# Cross-platform snapshot/restore of a build-output dir (bin/), used by `update` to roll back a failed
# rebuild. Operate on any path — no .exe assumptions.

def backup_build_output(path):
    """Snapshot <path> to <path>.bak (clearing any stale .bak first). Returns the .bak Path, or None if
    <path> doesn't exist (a fresh build — nothing to protect)."""
    src = Path(path)
    bak = Path(f"{src}.bak")
    if bak.exists():
        _rm_rf(bak)
    if not src.exists():
        return None
    if src.is_dir():
        shutil.copytree(src, bak)
    else:
        shutil.copy2(src, bak)
    return bak


def restore_build_output(path, bak_path=None) -> bool:
    """Roll <path> back to the snapshot from backup_build_output. Returns True if a restore happened."""
    src = Path(path)
    bak = Path(bak_path) if bak_path else Path(f"{src}.bak")
    if not bak.exists():
        return False
    if src.exists():
        _rm_rf(src)
    shutil.move(str(bak), str(src))
    return True


def remove_build_output_backup(path, bak_path=None) -> None:
    """Discard the snapshot after a verified-successful update. Port of Remove-BuildOutputBackup."""
    bak = Path(bak_path) if bak_path else Path(f"{path}.bak")
    if bak.exists():
        _rm_rf(bak)


def _rm_rf(p: Path) -> None:
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p, ignore_errors=True)
    else:
        try:
            p.unlink()
        except OSError:
            pass


# --- CUDA toolkit discovery (the hardest seam; used by diagnose + build) --------------
# The pin is a FLOOR, not an exact match: sm_120 (Blackwell) needs >= 12.8 but 12.9 / 13.x also qualify.
# Versions are compared as (major, minor) tuples. Windows and Linux fork on search roots + dir prefix.

def _parse_ver(s):
    """'12.8' / '12' / None -> (major, minor) tuple for ordering, or None."""
    import re
    if not s:
        return None
    m = re.search(r"(\d+)\.(\d+)", str(s))
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m = re.search(r"(\d+)", str(s))
    return (int(m.group(1)), 0) if m else None


def resolve_cuda_root_candidates(cuda_arch: int = 0, os: str = None) -> dict:
    """PURE ordered probe description for arch+OS (no disk I/O). Windows: one Base + 'v' prefix, pin v12.8
    for sm_120. Linux: /usr/local base + 'cuda-' prefix + canonical /usr/local/cuda, /opt/cuda and
    $CUDA_HOME/$CUDA_PATH symlinks. Port of Resolve-CudaRootCandidates."""
    import os as _os
    os = os or os_name()
    if os == "windows":
        base = r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA"
        if cuda_arch >= 120:
            return {"Base": base, "DirPrefix": "v", "Pin": "v12.8", "MinMajor": 12, "MinVer": "12.8", "Fixed": []}
        return {"Base": base, "DirPrefix": "v", "Pin": None,
                "MinMajor": 11 if cuda_arch >= 75 else 10,
                "MinVer": "11.0" if cuda_arch >= 75 else "10.0", "Fixed": []}
    fixed = ["/usr/local/cuda", "/opt/cuda"]
    if _os.environ.get("CUDA_HOME"):
        fixed.append(_os.environ["CUDA_HOME"])
    if _os.environ.get("CUDA_PATH"):
        fixed.append(_os.environ["CUDA_PATH"])
    return {
        "Base": "/usr/local", "DirPrefix": "cuda-", "Fixed": fixed,
        "Pin": "/usr/local/cuda-12.8" if cuda_arch >= 120 else None,
        "MinMajor": 12 if cuda_arch >= 120 else (11 if cuda_arch >= 75 else 10),
        "MinVer": "12.8" if cuda_arch >= 120 else ("11.0" if cuda_arch >= 75 else "10.0"),
    }


def cuda_toolkit_version(root):
    """(major, minor) tuple for the CUDA toolkit at `root`, or None. version.json -> version.txt ->
    `bin/nvcc --version` (lets a caller rank unversioned canonical roots like /opt/cuda)."""
    import re
    if not root:
        return None
    root = Path(root)
    if not root.exists():
        return None
    vj = root / "version.json"
    if vj.exists():
        try:
            v = (json.loads(vj.read_text(encoding="utf-8")).get("cuda") or {}).get("version", "")
            p = _parse_ver(v)
            if p:
                return p
        except (OSError, ValueError):
            pass
    vt = root / "version.txt"
    if vt.exists():
        try:
            p = _parse_ver(vt.read_text(encoding="utf-8"))
            if p:
                return p
        except OSError:
            pass
    nvcc = root / "bin" / exe_name("nvcc")
    if nvcc.exists():
        try:
            out = subprocess.run([str(nvcc), "--version"], capture_output=True, text=True, timeout=10)
            m = re.search(r"release (\d+)\.(\d+)", out.stdout)
            if m:
                return (int(m.group(1)), int(m.group(2)))
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def best_cuda_root(cuda_arch: int = 0):
    """Path (str) of the NEWEST CUDA toolkit meeting the arch's minimum version, or None. Pools canonical/
    pinned/env roots (version read from disk) and versioned <prefix><maj.min> dirs (version from the name),
    filters >= MinVer, returns the newest."""
    import re
    c = resolve_cuda_root_candidates(cuda_arch)
    min_ver = _parse_ver(c.get("MinVer")) or (0, 0)
    found = []  # (ver_tuple, path)

    explicit = []
    pin = c.get("Pin")
    if pin:
        explicit.append(pin if re.match(r"^[/~]|^[A-Za-z]:", pin) else str(Path(c["Base"]) / pin))
    explicit += list(c.get("Fixed", []))
    for p in explicit:
        if p and Path(p).exists():
            v = cuda_toolkit_version(p)
            if v:
                found.append((v, str(Path(p).resolve())))

    base = Path(c["Base"])
    if base.exists():
        pat = re.compile(rf"^{re.escape(c['DirPrefix'])}(\d+)\.(\d+)$")
        try:
            for d in base.iterdir():
                m = pat.match(d.name)
                if m and d.is_dir():
                    found.append(((int(m.group(1)), int(m.group(2))), str(d)))
        except OSError:
            pass

    ok = sorted((f for f in found if f[0] >= min_ver), key=lambda x: x[0], reverse=True)
    return ok[0][1] if ok else None


def cuda_host_compiler():
    """The g++ nvcc should use as -ccbin (Linux CUDA), or None to use the default. Honors $NVCC_CCBIN, else
    the newest versioned g++-NN older than the default g++ (a too-new default fails nvcc). None on Windows."""
    import os as _os
    import re
    if os_name() == "windows":
        return None
    ccbin = _os.environ.get("NVCC_CCBIN")
    if ccbin:
        found = shutil.which(ccbin)
        if found:
            return found
    def_major = 0
    if shutil.which("g++"):
        try:
            out = subprocess.run(["g++", "-dumpversion"], capture_output=True, text=True, timeout=10)
            m = re.match(r"^(\d+)", out.stdout.strip())
            if m:
                def_major = int(m.group(1))
        except (OSError, subprocess.SubprocessError):
            pass
    cands = []
    try:
        for p in Path("/usr/bin").glob("g++-*"):
            m = re.match(r"^g\+\+-(\d+)$", p.name)
            if m:
                major = int(m.group(1))
                if def_major == 0 or major < def_major:
                    cands.append((major, str(p)))
    except OSError:
        pass
    cands.sort(reverse=True)
    return cands[0][1] if cands else None


def assert_cuda_host_compiler_ok(nvcc, host_cxx=None) -> None:
    """Verify nvcc accepts the host C++ compiler by compiling a trivial kernel, BEFORE the long build; raise
    RuntimeError with an actionable hint on failure. No-op on Windows / missing nvcc. Port of
    Assert-CudaHostCompilerOk."""
    import tempfile
    if os_name() == "windows":
        return
    nvcc = Path(nvcc)
    if not nvcc.exists():
        return
    tmp = Path(tempfile.gettempdir()) / f"bob-nvcc-probe-{os.getpid()}.cu"
    obj = Path(f"{tmp}.o")
    tmp.write_text("__global__ void k(){}", encoding="ascii")
    ccbin = ["-ccbin", host_cxx] if host_cxx else []
    try:
        rc = subprocess.run([str(nvcc), *ccbin, "-c", str(tmp), "-o", str(obj)],
                            capture_output=True, text=True).returncode
    finally:
        tmp.unlink(missing_ok=True)
        obj.unlink(missing_ok=True)
    if rc != 0:
        hint = (f"nvcc rejected host compiler '{host_cxx}'. Set NVCC_CCBIN to a g++ this CUDA supports and re-run."
                if host_cxx else
                "nvcc rejected the default g++ (likely too new for this CUDA). Install an older g++ "
                "(e.g. g++-14/g++-13) and 'export NVCC_CCBIN=<path>', then re-run.")
        raise RuntimeError(f"CUDA host-compiler check failed: {hint}")


# --- build toolchain flags + cmake provisioning (used by build) ------------------------

def resolve_build_cmake_flags(cpu: bool = False, arch: int = 0, os: str = None) -> dict:
    """PURE. {'Cuda', 'Generator', 'StageDlls'} for a build. CPU => CUDA off + no staging (both OSes);
    GPU => CUDA on, Windows uses the VS generator + stages CUDA DLLs, Linux uses Ninja (rpath/ldconfig,
    no staging). Port of Resolve-BuildCmakeFlags."""
    os = os or os_name()
    gen = "Visual Studio 17 2022" if os == "windows" else "Ninja"
    if cpu:
        return {"Cuda": False, "Generator": gen, "StageDlls": False}
    return {"Cuda": True, "Generator": gen, "StageDlls": os == "windows"}


def linux_cmake3(repo, pinned_version: str = "3.31.7") -> str:
    """A cmake < 4.0 path (llama.cpp/whisper.cpp reject 4.x's policy changes). System cmake if it is 3.x,
    else download + cache the pinned Kitware build into tools/ (rolling distros ship only 4.x). urllib, not
    requests, so it works pre-venv in the kernel. Raises on download failure."""
    import re
    import tempfile
    import urllib.request

    sys_cmake = shutil.which("cmake")
    if sys_cmake:
        try:
            out = subprocess.run(["cmake", "--version"], capture_output=True, text=True, timeout=10)
            m = re.search(r"(\d+)\.(\d+)\.(\d+)", out.stdout)
            if m and (int(m.group(1)), int(m.group(2))) < (4, 0):
                return sys_cmake
            if m:
                print(f"  system cmake is {m.group(0)} (4.x) — llama.cpp/whisper.cpp need 3.x.", file=sys.stderr)
        except (OSError, subprocess.SubprocessError):
            pass
    machine = platform.machine() or "x86_64"
    stem = f"cmake-{pinned_version}-linux-{machine}"
    tools = Path(repo) / "tools"
    exe = tools / stem / "bin" / "cmake"
    if not exe.exists():
        url = f"https://github.com/Kitware/CMake/releases/download/v{pinned_version}/{stem}.tar.gz"
        tmp = Path(tempfile.gettempdir()) / f"{stem}.tar.gz"
        print(f"  fetching pinned cmake {pinned_version} ({machine}) from Kitware...", file=sys.stderr)
        urllib.request.urlretrieve(url, tmp)  # noqa: S310 — fixed Kitware https URL
        tools.mkdir(parents=True, exist_ok=True)
        subprocess.run(["tar", "-xzf", str(tmp), "-C", str(tools)], check=True)
        tmp.unlink(missing_ok=True)
    if not exe.exists():
        raise RuntimeError(f"failed to provision cmake {pinned_version} (expected {exe}). "
                           "Install a cmake 3.x manually and re-run.")
    return str(exe)


# --- mlock privilege (read-only status is a tool; grant is CLI-only) -------------

def mlock_status() -> dict:
    """{'granted': bool, 'detail': str}. Read-only, no elevation. Windows: SeLockMemoryPrivilege (secedit
    export); Linux: the memlock rlimit (ulimit -l)."""
    if os_name() == "windows":  # pragma: no cover — exercised only on Windows
        return _mlock_status_windows()
    try:
        out = subprocess.run(["sh", "-c", "ulimit -l"], capture_output=True, text=True, timeout=5)
        lim = out.stdout.strip().splitlines()[0].strip() if out.stdout.strip() else ""
    except (OSError, subprocess.SubprocessError):
        lim = ""
    if lim == "unlimited":
        return {"granted": True, "detail": "memlock limit: unlimited (--mlock active)"}
    return {"granted": False,
            "detail": f"memlock limit: {lim or 'unknown'} KB — insufficient for large models "
                      "(raise it: ulimit -l unlimited, or a limits.conf entry — see: bob mlock --grant)"}


def _current_user_sid() -> str:  # pragma: no cover — exercised only on Windows
    try:
        out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        # CSV: "DOMAIN\user","S-1-5-..."
        return out.strip().strip('"').split('","')[-1].strip('"')
    except (OSError, subprocess.SubprocessError, IndexError):
        return ""


def _mlock_status_windows() -> dict:  # pragma: no cover — exercised only on Windows
    import re
    import tempfile
    sid = _current_user_sid()
    tmp = Path(tempfile.gettempdir()) / f"bob-mlock-{os.getpid()}.inf"
    try:
        rc = subprocess.run(["secedit", "/export", "/cfg", str(tmp), "/areas", "USER_RIGHTS", "/quiet"],
                            capture_output=True, text=True).returncode
        if rc != 0 or not tmp.exists():
            return {"granted": False, "detail": "secedit /export failed — check Group Policy restrictions"}
        text = tmp.read_text(encoding="utf-16", errors="ignore")
        line = next((ln for ln in text.splitlines() if re.match(r"^\s*SeLockMemoryPrivilege\s*=", ln)), "")
        granted = bool(sid and f"*{sid}" in line)
        return {"granted": granted,
                "detail": f"SeLockMemoryPrivilege {'granted' if granted else 'NOT granted'} ({sid})"}
    finally:
        tmp.unlink(missing_ok=True)


def mlock_grant() -> str:
    """Grant the mlock privilege (CLI-only, never an agent tool — privilege escalation). Windows:
    SeLockMemoryPrivilege via secedit, self-elevating via UAC if not admin. Linux: print the
    ulimit/limits.conf guidance (never auto-edits a system file)."""
    if os_name() != "windows":
        return ("mlock on Linux is the memlock rlimit, not a grantable privilege. Raise it:\n"
                "  session:    ulimit -l unlimited\n"
                "  persistent: add '<user> - memlock unlimited' to /etc/security/limits.conf, then re-login")
    return _mlock_grant_windows()  # pragma: no cover


def _mlock_grant_windows() -> str:  # pragma: no cover — exercised only on Windows
    import ctypes
    import re
    import sys as _sys
    import tempfile

    try:
        is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (OSError, AttributeError):
        is_admin = False
    if not is_admin:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", _sys.executable, "-m bob mlock --grant", None, 1)
        if rc <= 32:
            return ("UAC cancelled or elevation failed — mlock not granted. Fallback: secpol.msc -> Local "
                    "Policies -> User Rights Assignment -> Lock pages in memory.")
        return "Requested admin rights (UAC) — the grant runs in the elevated window. Restart your terminal after."

    sid = _current_user_sid()
    inf = Path(tempfile.gettempdir()) / f"bob-mlock-grant-{os.getpid()}.inf"
    db = Path(tempfile.gettempdir()) / f"bob-mlock-grant-{os.getpid()}.sdb"
    try:
        if subprocess.run(["secedit", "/export", "/cfg", str(inf), "/areas", "USER_RIGHTS", "/quiet"]).returncode != 0:
            return "secedit /export failed."
        lines = inf.read_text(encoding="utf-16").splitlines()
        existing = next((ln for ln in lines if re.match(r"^\s*SeLockMemoryPrivilege\s*=", ln)), None)
        if existing and f"*{sid}" in existing:
            return "Already granted — no change needed."
        out = []
        if existing:
            for ln in lines:
                out.append(f"{ln},*{sid}" if re.match(r"^\s*SeLockMemoryPrivilege\s*=", ln) else ln)
        else:
            for ln in lines:
                out.append(ln)
                if re.match(r"^\[Privilege Rights\]", ln):
                    out.append(f"SeLockMemoryPrivilege = *{sid}")
        inf.write_text("\n".join(out), encoding="utf-16")
        subprocess.run(["secedit", "/configure", "/db", str(db), "/cfg", str(inf), "/areas", "USER_RIGHTS", "/quiet"])
        return f"SeLockMemoryPrivilege granted to {sid}. Close this terminal and open a new one, then: bob serve"
    finally:
        inf.unlink(missing_ok=True)
        db.unlink(missing_ok=True)


# --- Tier-0 provisioning seams (the cold-start KERNEL uses these under the
# system python3, before any venv exists). The python-provisioning + package family: PACKAGE_MAP /
# resolve_package_name / resolve_package_cmd / install_package + the venv-creator cluster
# python_version_at_least / install_uv / bob_python / bob_venv_python / new_bob_venv. ----

# Logical package -> concrete name per manager; None means the manager bundles it (caller skips).
# Single source — adding a distro column is a data change here, never re-inlined in a script.
PACKAGE_MAP = {
    "git":          {"apt": "git",             "dnf": "git",         "pacman": "git",        "zypper": "git"},
    "curl":         {"apt": "curl",            "dnf": "curl",        "pacman": "curl",       "zypper": "curl"},
    "toolchain-cc": {"apt": "build-essential", "dnf": "gcc-c++",     "pacman": "base-devel", "zypper": "gcc-c++"},
    "make":         {"apt": None,              "dnf": "make",        "pacman": None,         "zypper": "make"},
    "cmake":        {"apt": "cmake",           "dnf": "cmake",       "pacman": "cmake",      "zypper": "cmake"},
    "ninja":        {"apt": "ninja-build",     "dnf": "ninja-build", "pacman": "ninja",      "zypper": "ninja"},
    "go":           {"apt": "golang-go",       "dnf": "golang",      "pacman": "go",         "zypper": "go"},
    "node":         {"apt": "nodejs",          "dnf": "nodejs",      "pacman": "nodejs",     "zypper": "nodejs-default"},
    "npm":          {"apt": "npm",             "dnf": "npm",         "pacman": "npm",        "zypper": "npm-default"},
    "python":       {"apt": "python3",         "dnf": "python3",     "pacman": "python",     "zypper": "python3"},
    "python-pip":   {"apt": "python3-pip",     "dnf": "python3-pip", "pacman": None,         "zypper": "python3-pip"},
    "python-venv":  {"apt": "python3-venv",    "dnf": None,          "pacman": None,         "zypper": None},
    "cron":         {"apt": "cron",            "dnf": "cronie",      "pacman": "cronie",     "zypper": "cronie"},
    "cuda":         {"apt": "nvidia-cuda-toolkit", "dnf": "cuda-toolkit", "pacman": "cuda",  "zypper": "cuda"},
    "docker":       {"apt": "docker.io",       "dnf": "docker",      "pacman": "docker",     "zypper": "docker"},
}


def resolve_package_name(logical: str, manager: str = None):
    """Concrete package name for a logical one on a manager, or None when it's bundled (caller skips).
    Raises KeyError on an unknown logical name or a manager with no column — a mapping gap fails loudly
    instead of silently no-op'ing an install."""
    manager = manager or linux_package_manager()
    if logical not in PACKAGE_MAP:
        raise KeyError(f"resolve_package_name: no mapping for logical package '{logical}' — "
                       "add it to PACKAGE_MAP in osenv.py.")
    row = PACKAGE_MAP[logical]
    # rpm-ostree layers Fedora RPMs — reuse the dnf column rather than duplicating a whole table.
    key = "dnf" if manager == "rpm-ostree" else manager
    if key not in row:
        raise KeyError(f"resolve_package_name: logical '{logical}' has no entry for manager "
                       f"'{manager}' — add the '{key}' column to PACKAGE_MAP.")
    return row[key]


# Batchable install-arg templates per Linux manager (one transaction, one sudo prompt). rpm-ostree
# LAYERS packages (read-only /usr) and they apply on the next boot; --idempotent avoids erroring on
# already-layered pkgs.
def _linux_pkg_spec(manager: str, packages: list) -> dict:
    pkgs = list(packages)
    specs = {
        "apt":         {"Exe": "apt-get", "Args": ["install", "-y", *pkgs], "Sudo": True},
        "dnf":         {"Exe": "dnf",     "Args": ["install", "-y", *pkgs], "Sudo": True},
        "pacman":      {"Exe": "pacman",  "Args": ["-S", "--needed", "--noconfirm", *pkgs], "Sudo": True},
        "zypper":      {"Exe": "zypper",  "Args": ["--non-interactive", "install", *pkgs], "Sudo": True},
        "rpm-ostree":  {"Exe": "rpm-ostree", "Args": ["install", "--idempotent", "--allow-inactive", *pkgs],
                        "Sudo": True},
    }
    return specs.get(manager, {"Exe": None, "Args": [], "Sudo": False, "Manager": manager})


def resolve_package_cmd(package: str, os: str = None, manager: str = None) -> dict:
    """PURE. The install command spec {'Exe','Args','Sudo'} for the OS (+ Linux manager). Windows uses
    winget; Linux uses the detected manager (+ rpm-ostree)."""
    os = os or os_name()
    if os == "windows":
        return {"Exe": "winget", "Args": ["install", package, "--accept-package-agreements",
                                          "--accept-source-agreements", "--disable-interactivity"],
                "Sudo": False}
    return _linux_pkg_spec(manager, [package])


def _run_pkg_spec(spec: dict, extra=(), label: str = "") -> None:
    """Run an install spec once (sudo-prefixed only when needed + available). Raises on real failure."""
    argv = spec["Args"] + list(extra)
    if spec["Sudo"] and shutil.which("sudo") and not (hasattr(os, "geteuid") and os.geteuid() == 0):
        cmd = ["sudo", spec["Exe"], *argv]
    else:
        cmd = [spec["Exe"], *argv]
    rc = subprocess.run(cmd).returncode
    # -1978335189 = APPINSTALLER_CLI_ERROR_PACKAGE_ALREADY_INSTALLED (winget); treat as success.
    if rc not in (0, -1978335189):
        raise RuntimeError(f"install failed for {label or spec['Exe']} (exit {rc}).")


def install_package(package: str, extra_args=(), dry_run: bool = False):
    """EXECUTOR (single). Install one package: Windows via winget (tolerating already-installed), Linux via
    the detected manager. dry_run prints + returns the resolved spec without executing. Raises on failure."""
    osname = os_name()
    mgr = None if osname == "windows" else linux_package_manager()
    if osname != "windows" and not mgr:
        raise RuntimeError(f"install_package: no supported package manager found for '{package}'.")
    spec = resolve_package_cmd(package, os=osname, manager=mgr)
    if dry_run:
        prefix = "sudo " if spec["Sudo"] else ""
        print(f"  [dry-run] {(prefix + spec['Exe'] + ' ' + ' '.join(spec['Args'] + list(extra_args))).strip()}",
              file=sys.stderr)
        return spec
    _run_pkg_spec(spec, extra_args, label=f"'{package}'")
    return spec


def install_packages(packages, manager: str = None, dry_run: bool = False) -> None:
    """EXECUTOR (batch). Install MANY packages in ONE transaction — so the user is asked for their sudo
    password ONCE (up front), not per package. Windows: winget one at a time (it doesn't batch). Linux:
    a single manager call. Raises RuntimeError on failure (the whole set fails — fix + re-run)."""
    seen, pkgs = set(), []
    for p in packages:  # drop falsy (bundled -> None) + dupes, preserve order
        if p and p not in seen:
            seen.add(p)
            pkgs.append(p)
    if not pkgs:
        return
    if os_name() == "windows":
        for p in pkgs:
            install_package(p, dry_run=dry_run)
        return
    mgr = manager or linux_package_manager()
    if not mgr:
        raise RuntimeError("install_packages: no supported package manager (apt/dnf/pacman/zypper/"
                           "rpm-ostree) found.")
    spec = _linux_pkg_spec(mgr, pkgs)
    if dry_run:
        prefix = "sudo " if spec["Sudo"] else ""
        print(f"  [dry-run] {(prefix + spec['Exe'] + ' ' + ' '.join(spec['Args'])).strip()}", file=sys.stderr)
        return
    _run_pkg_spec(spec, label=f"{len(pkgs)} package(s): {' '.join(pkgs)}")


def _py_minor(exe: str):
    """(major, minor) tuple from `<exe> --version`, or None."""
    import re
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+)\.(\d+)", (out.stdout or "") + (out.stderr or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


def python_at_least(exe: str = "python", min_ver: str = "3.12") -> bool:
    """True if `<exe> --version` reports >= min_ver. Bob needs 3.12+ but not exactly 3.12."""
    v = _py_minor(exe)
    return bool(v and v >= (_parse_ver(min_ver) or (3, 12)))


def install_uv():
    """Ensure astral `uv` is on PATH; return its path or None. pacman ships uv; apt/dnf don't, so fall
    back to astral's official installer (~/.local/bin, no sudo). Linux/macOS only."""
    uv = shutil.which("uv")
    if uv:
        return uv
    if linux_package_manager() == "pacman":
        try:
            install_package("uv")
        except (RuntimeError, KeyError):
            pass
    uv = shutil.which("uv")
    if uv:
        return uv
    print("  installing uv (astral) via the official installer...", file=sys.stderr)
    try:
        subprocess.run(["sh", "-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"], check=False)
    except (OSError, subprocess.SubprocessError):
        pass
    for p in (Path.home() / ".local/bin/uv", Path.home() / ".cargo/bin/uv"):
        if p.exists():
            os.environ["PATH"] = f"{p.parent}{os.pathsep}{os.environ.get('PATH', '')}"
            return str(p)
    return shutil.which("uv")


def bob_python(prefer: str = "3.12"):
    """A Python interpreter Bob's venvs can use: >= 3.11 and < 3.13 (pinned deps cap at <3.13, so a
    too-NEW system Python is rejected). Prefer one on PATH in range; else uv-provision CPython 3.12 (any
    distro, no root). Returns a path/command, or None (Windows resolves upstream)."""
    for cand in ("python3.12", "python3.11", "python3", "python"):
        exe = shutil.which(cand)
        if exe:
            v = _py_minor(cand)
            if v and (3, 11) <= v < (3, 13):
                return exe
    if os_name() == "windows":
        return None
    uv = install_uv()
    if not uv:
        return None
    print(f"  system Python is out of range (venvs need 3.11/3.12) — provisioning CPython {prefer} via uv...",
          file=sys.stderr)
    subprocess.run([uv, "python", "install", prefer], check=False)
    try:
        found = subprocess.run([uv, "python", "find", prefer], capture_output=True, text=True).stdout
        found = found.strip().splitlines()[0].strip() if found.strip() else ""
    except (OSError, subprocess.SubprocessError, IndexError):
        found = ""
    return found if found and Path(found).exists() else None


def bob_venv_python():
    """Resolve a Python suitable for CREATING venvs (>= 3.11, < 3.13). Windows: scoop python312 -> py
    launcher -3.12 -> an in-range PATH interpreter. Linux/macOS: defer to bob_python (uv-provisions when
    the system one is out of range). Returns a path/command or None. The single resolver new_bob_venv and
    the kernel bootstrap share."""
    if os_name() != "windows":
        return bob_python()
    try:  # pragma: no cover — Windows path
        p = subprocess.run(["scoop", "prefix", "python312"], capture_output=True, text=True).stdout.strip()
        if p:
            cand = Path(p) / "python.exe"
            if cand.exists():
                return str(cand)
    except (OSError, subprocess.SubprocessError):
        pass
    if shutil.which("py"):  # pragma: no cover — Windows path
        try:
            resolved = subprocess.run(["py", "-3.12", "-c", "import sys; print(sys.executable)"],
                                      capture_output=True, text=True).stdout.strip().splitlines()
            if resolved and Path(resolved[0].strip()).exists():
                return resolved[0].strip()
        except (OSError, subprocess.SubprocessError, IndexError):
            pass
    for cand in ("python3.12", "python", "python3"):  # pragma: no cover — Windows path
        if shutil.which(cand) and python_at_least(cand, "3.12"):
            return cand
    return None


def new_bob_venv(name: str, requirements_base: str = None, extra_packages=(), python: str = None,
                 force: bool = False, quiet: bool = False) -> str:
    """Create (or self-heal) a Bob venv under tools/<name> and install its requirements. THE single
    venv-build path (the kernel bootstrap loop + `update`/`eval` all call it). Idempotent: reuses an
    in-range venv, recreates one built with an out-of-range interpreter. Requirements from
    tools/<base>.lock on Windows (pinned) else tools/<base>.txt. Returns the venv python path (str).
    Raises RuntimeError on any failure."""
    python = python or bob_venv_python()
    if not python:
        raise RuntimeError("new_bob_venv: no venv-compatible Python (3.11/3.12) found and couldn't "
                           "provision one via uv. Install Python 3.12 and re-run.")
    venv = REPO / "tools" / name
    venv_py = venv_exe(name, "python")
    quiet_arg = ["--quiet"] if quiet else []

    if force and venv.exists():
        _rm_rf(venv)
    if venv.exists() and venv_py.exists():
        v = _py_minor(str(venv_py))
        if not (v and (3, 11) <= v < (3, 13)):
            print(f"  recreating {name} (was Python {v} — need 3.11/3.12)", file=sys.stderr)
            _rm_rf(venv)
    if not venv.exists():
        print(f"  creating {name} ({python})...", file=sys.stderr)
        if subprocess.run([python, "-m", "venv", str(venv)]).returncode != 0:
            raise RuntimeError(f"python -m venv failed for {name}.")
    if not venv_py.exists():
        raise RuntimeError(f"venv creation failed for {name} — {venv_py} not found")

    if subprocess.run([str(venv_py), "-m", "pip", "install", "--upgrade", "pip", *quiet_arg]).returncode != 0:
        raise RuntimeError(f"pip upgrade failed for {name}.")

    if requirements_base:
        lock = REPO / "tools" / f"{requirements_base}.lock"
        txt = REPO / "tools" / f"{requirements_base}.txt"
        req = lock if (os_name() == "windows" and lock.exists()) else txt
        print(f"  installing {name} from {req.name}", file=sys.stderr)
        if subprocess.run([str(venv_py), "-m", "pip", "install", "-r", str(req), *quiet_arg]).returncode != 0:
            raise RuntimeError(f"pip install failed for {name} — re-run to retry.")
    for pkg in extra_packages:
        print(f"  installing {pkg} into {name}", file=sys.stderr)
        if subprocess.run([str(venv_py), "-m", "pip", "install", pkg, *quiet_arg]).returncode != 0:
            raise RuntimeError(f"pip install {pkg} failed for {name}.")
    return str(venv_py)
