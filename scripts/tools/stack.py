"""Bob lifecycle capabilities (ONE-C Slice 2) — the local inference stack: llama-swap endpoint, LiteLLM
proxy, Open WebUI, whisper STT, piper TTS, and optional Docker services.

Functional grouping (D6): one module, several related tool fns, each reached three ways with no
duplicated logic — the agent tool (DISPATCH), the `bob <verb>` cli handler (scripts/bob/cli.py), and
`bob --run <cap>`. Ports scripts/start*.ps1 + up.ps1 + the bob.ps1 lifecycle cases, launching binaries
DIRECTLY via osenv.start_detached (no pwsh Tee-Object wrapper) and reaping via the osenv process seams.

Interim bridge: config regeneration (`gen`) is still PowerShell until ONE-C Slice 6, so bring-up does a
best-effort regen via pwsh IF present, else proceeds with the existing (checked-in-locally, gitignored)
config/llama-swap.yaml + config/litellm.yaml — erroring only if they're absent. When Slice 6 lands,
_regen_configs swaps to the Python generator."""
import shutil
import subprocess
import sys
import time
from pathlib import Path

_cfg: dict = {}

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO / "scripts"

# PID/log convention: logs/<svc>.{pid,log}. C++ daemons are killed by NAME (they survive stale pidfiles);
# python-hosted services (litellm/piper/open-webui) are killed by pidfile + child-reaping tree kill.
_NAME_KILL = ["llama-swap", "llama-server", "whisper-server"]
_PS_SERVICES = ["llama-swap", "litellm", "open-webui", "whisper", "piper"]


def configure(config: dict) -> None:
    global _cfg
    _cfg = config
    scripts_dir = str(SCRIPTS)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)


MUTATING_TOOLS = {"stack_up", "stack_stop", "stack_restart",
                  "litellm_control", "whisper_control", "piper_control", "services_control"}


# --- small helpers --------------------------------------------------------------------------------

def _osenv():
    import osenv
    return osenv


def _logs_dir() -> Path:
    return _osenv().cache_dir()  # repo logs/ by default; BOB_DATA_DIR/logs when overridden (C4)


def _pidfile(svc: str) -> Path:
    return _logs_dir() / f"{svc}.pid"


def _logfile(svc: str) -> Path:
    return _logs_dir() / f"{svc}.log"


def _read_pid(svc: str):
    pf = _pidfile(svc)
    if not pf.exists():
        return None
    try:
        return int(pf.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def _poll(check, timeout: float, interval: float = 0.3) -> bool:
    """Poll `check()` until truthy or `timeout` seconds elapse. time.monotonic is fine here (not a
    workflow script)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(interval)
    return False


def _regen_configs() -> bool:
    """Best-effort refresh of config/llama-swap.yaml + litellm.yaml from models.json (so profile edits
    take effect). Delegates to the single-sourced regen in bob_models (shared with the profile switch),
    which since ONE-C Slice 6 runs the Python generators directly — no pwsh in this hot path."""
    import bob_models
    return bob_models.regenerate_configs()


# --- HTTP health probes (stdlib — stack orchestration stays venv-free, like smoke.py/check.py; the
# runtime deps live in venv-litellm, which the orchestrator launches but doesn't import) -----------

def _http_json(url: str, timeout: float = 3):
    import json
    import urllib.request
    with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 — localhost only
        return json.loads(r.read().decode("utf-8", "replace"))


def _http_ok(url: str, timeout: float = 2) -> bool:
    import urllib.error
    import urllib.request
    try:
        urllib.request.urlopen(url, timeout=timeout)  # noqa: S310 — localhost only
        return True
    except (urllib.error.URLError, OSError):
        return False


# --- ps / logs (read-only) ------------------------------------------------------------------------

def stack_ps(config: dict) -> str:
    """Port of the `ps` case: per-service pidfile -> PID/RAM/uptime/liveness table."""
    osenv = _osenv()
    lines = ["", "Bob Processes", "",
             f"{'Service':<15} {'PID':<8} {'RAM':<10} {'Uptime':<10} Status",
             "-" * 60]
    for svc in _PS_SERVICES:
        pid = _read_pid(svc)
        if pid is None:
            lines.append(f"{svc:<15} {'--':<8} {'--':<10} {'--':<10} not running")
            continue
        stats = osenv.process_stats(pid)
        if stats is None:
            lines.append(f"{svc:<15} {pid:<8} {'--':<10} {'--':<10} dead (stale PID file)")
        else:
            ram = f"{stats['rss_mb']} MB" if stats["rss_mb"] is not None else "?"
            up = stats["uptime"] or "?"
            lines.append(f"{svc:<15} {pid:<8} {ram:<10} {up:<10} running")
    lines.append("")
    return "\n".join(lines)


def stack_status(config: dict) -> str:
    """Loaded models + VRAM state (queries /v1/models) plus whisper/piper port probes. Port of the
    `status` case. Read-only; bails with a hint if the endpoint isn't running (needs the registry, which
    it reads via bob_models — ONE-C Slice 4 made that Python-native)."""
    import bob_models
    from bob_core import _port

    osenv = _osenv()
    port = _port(config, "port")
    base = f"http://localhost:{port}/v1"
    try:
        data = _http_json(f"{base}/models", timeout=3)
    except (OSError, ValueError):
        return "Endpoint not running. Start with: bob serve"
    loaded = {m.get("id") for m in data.get("data", [])}

    mcfg = bob_models.load_models_config()
    profile = bob_models.resolve_profile_name(config=mcfg)
    roles = bob_models.profile_roles(profile, mcfg)
    ordered = [r for r in _ROLE_ORDER if r in roles] + sorted(r for r in roles if r not in _ROLE_ORDER)

    lines = ["", f"Endpoint: {base}  [running]", f"Profile:  {profile}", "",
             f"{'Role':<10} {'Model':<36} {'VRAM':<9} {'State'}", "-" * 70]
    for role in ordered:
        spec = roles[role]
        label = f"{spec.get('gguf', '').replace('.gguf', '')} ({spec.get('sizeGB', '?')} GB)"
        is_loaded = role in loaded
        pinned = spec.get("pinned")
        state = ("loaded (pinned)" if pinned and is_loaded else "loading..." if pinned
                 else "loaded" if is_loaded else "unloaded")
        vram = f"{spec.get('sizeGB', '?')} GB" if is_loaded else "--"
        lines.append(f"{role:<10} {label:<36} {vram:<9} {state}")
    for name, key in (("whisper", "sttPort"), ("piper", "ttsPort")):
        p = _port(config, key)
        up = osenv.is_port_in_use(p)
        lbl = "(stt server)" if name == "whisper" else "(tts server)"
        lines.append(f"  {name:<10} {lbl:<36} {('UP (port ' + str(p) + ')') if up else ('down (port ' + str(p) + ')')}")
    lines.append("")
    return "\n".join(lines)


# canonical role order (mirrors Get-Models / models.py)
_ROLE_ORDER = ["planner", "coder", "chat", "fim", "embed"]


def stack_logs(config: dict, lines: int = 50) -> str:
    """Bounded tail of logs/llama-swap.log (the agent-facing, non-follow read). CLI follow is separate."""
    log_file = _logfile("llama-swap")
    if not log_file.exists():
        return "No log file yet. Start the endpoint first: bob serve (or the stack_up tool)."
    try:
        content = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        return f"Error reading {log_file}: {e}"
    tail = content[-int(lines):] if lines else content
    return f"--- logs/llama-swap.log (last {len(tail)} lines) ---\n" + "\n".join(tail)


# --- teardown -------------------------------------------------------------------------------------

def stack_stop(config: dict) -> str:
    """Canonical teardown (port of the `stop` case): name-kill the C++ daemons, tree-kill the python
    services by pidfile, clean stale pidfiles, and `docker compose down` if services are up."""
    osenv = _osenv()
    killed = []

    # 1) C++ binaries by process name (survive stale pidfiles).
    killed += osenv.stop_processes_by_name(_NAME_KILL)

    # 2) Python-hosted services via pidfile + child-reaping tree kill.
    for svc in ("litellm", "piper", "open-webui"):
        pid = _read_pid(svc)
        if pid is not None and osenv.pid_alive(pid):
            osenv.stop_process_tree(pid)
            killed.append(svc)
        _pidfile(svc).unlink(missing_ok=True)

    # 3) Clean remaining pidfiles.
    for svc in ("llama-swap", "whisper"):
        _pidfile(svc).unlink(missing_ok=True)

    # 4) Docker services.
    compose = REPO / "tools" / "compose" / "docker-compose.yml"
    if shutil.which("docker") and compose.exists():
        running = subprocess.run(["docker", "compose", "-f", str(compose), "ps", "-q"],
                                 check=False, capture_output=True, text=True)
        if running.stdout.strip():
            subprocess.run(["docker", "compose", "-f", str(compose), "down"],
                           check=False, capture_output=True)
            killed.append("docker-services")

    if killed:
        return "Stopped: " + ", ".join(dict.fromkeys(killed))  # dedupe, keep order
    return "Nothing was running."


# --- launching the endpoint + services ------------------------------------------------------------

def _ensure_configs() -> str:
    """Regenerate (best-effort) then require config/llama-swap.yaml. Returns '' on success, else an
    error message."""
    _regen_configs()
    if not (REPO / "config" / "llama-swap.yaml").exists():
        return ("config/llama-swap.yaml is missing and could not be generated from config/models.json. "
                "Run `bob gen` (or check that config/models.json is valid).")
    return ""


def _start_litellm_bg(config: dict) -> str:
    osenv = _osenv()
    from bob_core import _port

    proxy = osenv.venv_exe("venv-litellm", "litellm")
    if not proxy.exists():
        return "LiteLLM venv not found — proxy skipped (run: python -m bob.kernel venv litellm)."
    pid = _read_pid("litellm")
    if pid is not None and osenv.pid_alive(pid):
        return f"LiteLLM already running (PID {pid})."
    _pidfile("litellm").unlink(missing_ok=True)
    port = _port(config, "litellmPort")
    cfg = REPO / "config" / "litellm.yaml"
    new_pid = osenv.start_detached(
        [str(proxy), "--config", str(cfg), "--port", str(port)],
        pidfile=_pidfile("litellm"), log_path=_logfile("litellm"), env={"PYTHONUTF8": "1"})
    return f"LiteLLM proxy: http://localhost:{port}/v1 (PID {new_pid})"


def _start_whisper_bg(config: dict) -> str:
    osenv = _osenv()
    from bob_core import _port

    exe = osenv.bin_exe("whisper-server")
    stt_port = _port(config, "sttPort")
    model = REPO / "models" / "whisper" / f"ggml-{config.get('voice', {}).get('sttModel', 'small')}.bin"
    if not exe.exists():
        return f"whisper-server missing ({exe.name}) — run: bob setup-voice"
    if not model.exists():
        return f"Whisper model not found at {model} — run: bob setup-voice"
    osenv.stop_processes_by_name("whisper-server")  # reap stale (VRAM leak guard)
    _pidfile("whisper").unlink(missing_ok=True)
    pid = osenv.start_detached(
        [str(exe), "--model", str(model), "--port", str(stt_port), "--host", "127.0.0.1"],
        pidfile=_pidfile("whisper"), log_path=_logfile("whisper"))
    ready = _poll(lambda: osenv.is_port_in_use(stt_port), timeout=30, interval=0.5)
    tail = "ready" if ready else "may not be ready yet — check logs/whisper.log"
    return f"whisper-server: http://localhost:{stt_port} (PID {pid}) — {tail}"


def _start_piper_bg(config: dict) -> str:
    osenv = _osenv()
    from bob_core import _port

    piper_exe = osenv.bin_exe("piper")
    tts_port = _port(config, "ttsPort")
    voice = config.get("voice", {}).get("ttsVoice", "en_GB-alan-medium")
    voice_path = REPO / "bin" / "voices" / f"{voice}.onnx"
    py = osenv.venv_exe("venv-litellm", "python")
    server = SCRIPTS / "piper_server.py"
    if not piper_exe.exists():
        return "piper binary not found — run: bob setup-voice"
    if not voice_path.exists():
        return f"Voice model not found at {voice_path} — run: bob setup-voice"
    if not py.exists():
        return "venv-litellm not found — run: python -m bob.kernel venv litellm"
    pid = _read_pid("piper")
    if pid is not None and osenv.pid_alive(pid):
        return f"piper-server already running (PID {pid})."
    _pidfile("piper").unlink(missing_ok=True)
    new_pid = osenv.start_detached(
        [str(py), str(server)], pidfile=_pidfile("piper"), log_path=_logfile("piper"),
        env={"PIPER_EXE": str(piper_exe), "PIPER_VOICE": str(voice_path), "PIPER_PORT": str(tts_port)})
    ready = _poll(lambda: osenv.is_port_in_use(tts_port), timeout=20, interval=0.5)
    tail = "ready" if ready else "may not be ready — check logs/piper.log"
    return f"piper-server: http://localhost:{tts_port} (PID {new_pid}, voice={voice}) — {tail}"


def _start_endpoint_bg(config: dict) -> tuple:
    """Launch llama-swap detached (+ litellm, + whisper if voice.enabled), poll /v1/models. Returns
    (ok, list-of-status-lines)."""
    osenv = _osenv()
    from bob_core import _port

    swap = osenv.bin_exe("llama-swap")
    if not swap.exists():
        return (False, [f"{swap.name} missing — run: python -m bob.kernel build-swap (or drop the binary in bin/)."])
    port = _port(config, "port")
    lines = []
    if osenv.is_port_in_use(port):
        return (True, [f"Port {port} already in use — the endpoint is probably already running "
                       f"(bob stop to free it)."])
    swap_cfg = REPO / "config" / "llama-swap.yaml"
    pid = osenv.start_detached(
        [str(swap), "--config", str(swap_cfg), "--listen", f"127.0.0.1:{port}"],
        pidfile=_pidfile("llama-swap"), log_path=_logfile("llama-swap"),
        env={"LLAMA_LOCAL_ROOT": str(REPO).replace("\\", "/")})
    lines.append(f"Endpoint: http://localhost:{port}/v1 (PID {pid})")
    lines.append(_start_litellm_bg(config))
    if config.get("voice", {}).get("enabled"):
        lines.append(_start_whisper_bg(config))

    def ready():
        return osenv.pid_alive(pid) and _http_ok(f"http://localhost:{port}/v1/models", timeout=2)

    ok = _poll(ready, timeout=60, interval=0.3)
    lines.append("Endpoint ready." if ok else "Endpoint did not respond in 60s — check: bob logs")
    return (ok and osenv.pid_alive(pid), lines)


def stack_up(config: dict, open_browser: bool = True, with_services: bool = False) -> str:
    """Background bring-up (port of up.ps1): endpoint + LiteLLM + (whisper if voice) + Open WebUI,
    then optionally open the browser and start Docker services. Returns a status report."""
    osenv = _osenv()
    from bob_core import _port

    err = _ensure_configs()
    if err:
        return err
    ok, lines = _start_endpoint_bg(config)

    # Open WebUI (opt-in; hidden background window in pwsh -> detached here).
    webui = osenv.venv_exe("venv-webui", "open-webui")
    webui_port = _port(config, "webuiPort")
    if webui.exists():
        litellm_port = _port(config, "litellmPort")
        litellm_key = osenv.secret("litellmKey", config.get("litellmKey") or "sk-local")
        api_base = f"http://localhost:{litellm_port}/v1"
        env = {
            "OPENAI_API_BASE_URL": api_base, "OPENAI_API_KEY": litellm_key,
            "RAG_EMBEDDING_ENGINE": "openai", "RAG_OPENAI_API_BASE_URL": api_base,
            "RAG_OPENAI_API_KEY": litellm_key, "RAG_EMBEDDING_MODEL": "embed",
            "DATA_DIR": str(REPO / "tools" / "webui-data"),
            "WEBUI_SECRET_KEY": config.get("webuiSecret") or "bob-dev",
        }
        pid = osenv.start_detached(
            [str(webui), "serve", "--port", str(webui_port)],
            pidfile=_pidfile("open-webui"), log_path=_logfile("open-webui"), env=env)
        lines.append(f"Open WebUI: http://localhost:{webui_port} (PID {pid})")
        if open_browser:
            if _poll(lambda: osenv.is_port_in_use(webui_port), timeout=120, interval=0.5):
                lines.append("Open WebUI ready.")
                osenv.open_url(f"http://localhost:{webui_port}")
            else:
                lines.append(f"Open WebUI didn't respond — open manually: http://localhost:{webui_port}")
    else:
        lines.append("open-webui not installed (opt-in) — skipping. (re-run setup with --with-webui)")

    if with_services:
        lines.append(services_control(config, "start"))
    lines.append(f"clients: http://localhost:{_port(config, 'litellmPort')}/v1   stop: bob stop   logs: bob logs")
    return "\n".join(lines)


def stack_restart(config: dict) -> str:
    """Background restart for the agent (port of the `restart` case, but non-blocking): tear the
    endpoint down, then bring it back up detached + poll. `bob serve`/`stack_up` remain the fg paths."""
    osenv = _osenv()
    osenv.stop_processes_by_name(["llama-swap", "llama-server", "open-webui"])
    for svc in ("llama-swap", "open-webui", "litellm"):
        pid = _read_pid(svc)
        if pid is not None and osenv.pid_alive(pid):
            osenv.stop_process_tree(pid)
        _pidfile(svc).unlink(missing_ok=True)
    time.sleep(0.5)
    err = _ensure_configs()
    if err:
        return err
    ok, lines = _start_endpoint_bg(config)
    return "Restarting endpoint...\n" + "\n".join(lines)


# --- per-service control (start/stop/status) ------------------------------------------------------

def _service_status(svc: str, label: str, port: int, url_suffix: str = "/v1") -> str:
    osenv = _osenv()
    pid = _read_pid(svc)
    if pid is None:
        return f"{label} not running."
    stats = osenv.process_stats(pid)
    if stats is None:
        return f"{label} dead (stale PID {pid})."
    up = stats["uptime"] or "?"
    return f"{label} running  PID={pid}  uptime={up}  http://localhost:{port}{url_suffix}"


def _service_stop(svc: str, label: str) -> str:
    osenv = _osenv()
    pid = _read_pid(svc)
    stopped = False
    if pid is not None and osenv.pid_alive(pid):
        osenv.stop_process_tree(pid)
        stopped = True
    _pidfile(svc).unlink(missing_ok=True)
    return f"{label} stopped." if stopped else f"{label} not running."


def litellm_control(config: dict, action: str = "start") -> str:
    from bob_core import _port
    if action == "stop":
        return _service_stop("litellm", "LiteLLM")
    if action == "status":
        return _service_status("litellm", "LiteLLM", _port(config, "litellmPort"))
    return _start_litellm_bg(config)


def whisper_control(config: dict, action: str = "start") -> str:
    from bob_core import _port
    if action == "stop":
        return _service_stop("whisper", "whisper-server")
    if action == "status":
        return _service_status("whisper", "whisper-server", _port(config, "sttPort"), url_suffix="")
    return _start_whisper_bg(config)


def piper_control(config: dict, action: str = "start") -> str:
    from bob_core import _port
    if action == "stop":
        return _service_stop("piper", "piper-server")
    if action == "status":
        return _service_status("piper", "piper-server", _port(config, "ttsPort"), url_suffix="")
    return _start_piper_bg(config)


def services_control(config: dict, action: str = "status") -> str:
    """Docker compose lifecycle (langfuse/searxng/n8n). Optional — needs docker + the compose file."""
    from bob_core import _port

    compose = REPO / "tools" / "compose" / "docker-compose.yml"
    if not shutil.which("docker"):
        return "Docker not found. Install docker, then re-run setup (it provisions the compose services)."
    if not compose.exists():
        return f"No compose file at {compose}."
    base = ["docker", "compose", "-f", str(compose)]
    if action == "start":
        env_file = REPO / "tools" / "compose" / ".env"
        env_file.write_text(
            f"REPO_PATH={REPO}\n"
            f"LANGFUSE_PORT={_port(config, 'langfusePort')}\n"
            f"SEARXNG_PORT={_port(config, 'searxngPort')}\n"
            f"N8N_PORT={_port(config, 'n8nPort')}\n", encoding="utf-8")
        r = subprocess.run(base + ["up", "-d"], check=False, capture_output=True, text=True)
        return "Services started.\n" + (r.stderr or r.stdout).strip()
    if action == "stop":
        subprocess.run(base + ["down"], check=False, capture_output=True)
        return "Services stopped."
    if action == "status":
        r = subprocess.run(base + ["ps"], check=False, capture_output=True, text=True)
        return r.stdout.strip() or "No services running."
    if action == "logs":
        r = subprocess.run(base + ["logs", "--tail=50"], check=False, capture_output=True, text=True)
        return r.stdout.strip()
    return "Usage: bob services start|stop|status|logs"


# --- foreground (CLI-only) launchers --------------------------------------------------------------

def serve_foreground(config: dict) -> int:
    """`bob serve` — interactive: start LiteLLM + whisper in the background, then run llama-swap in the
    FOREGROUND (Ctrl+C to stop). Inherits the terminal so logs stream live."""
    osenv = _osenv()
    from bob_core import _port

    err = _ensure_configs()
    if err:
        print(err, file=sys.stderr)
        return 1
    swap = osenv.bin_exe("llama-swap")
    if not swap.exists():
        print(f"{swap.name} missing — run: python -m bob.kernel build-swap (or drop the binary in bin/).",
              file=sys.stderr)
        return 1
    print(_start_litellm_bg(config), file=sys.stderr)
    if config.get("voice", {}).get("enabled"):
        print(_start_whisper_bg(config), file=sys.stderr)
    port = _port(config, "port")
    if osenv.is_port_in_use(port):
        print(f"Port {port} already in use — the endpoint is probably already running (bob stop).",
              file=sys.stderr)
        return 0
    swap_cfg = REPO / "config" / "llama-swap.yaml"
    print(f"Endpoint: http://localhost:{port}/v1   (loopback only; Ctrl+C to stop)", file=sys.stderr)
    env = {**__import__("os").environ, "LLAMA_LOCAL_ROOT": str(REPO).replace("\\", "/")}
    return subprocess.run([str(swap), "--config", str(swap_cfg), "--listen", f"127.0.0.1:{port}"],
                          env=env).returncode


def webui_foreground(config: dict) -> int:
    """`bob webui` — run Open WebUI in the foreground (opt-in, blocking)."""
    osenv = _osenv()
    from bob_core import _port

    webui = osenv.venv_exe("venv-webui", "open-webui")
    if not webui.exists():
        print("Open WebUI not installed (opt-in). Re-run setup with --with-webui", file=sys.stderr)
        return 1
    return subprocess.run([str(webui), "serve", "--port", str(_port(config, "webuiPort"))]).returncode


def logs_follow(config: dict, lines: int = 50) -> int:
    """`bob logs [-n N]` — tail-follow logs/llama-swap.log (CLI-only; the bounded stack_logs tool is
    the agent surface)."""
    log_file = _logfile("llama-swap")
    if not log_file.exists():
        print("No log file yet. Start the endpoint first: bob serve", file=sys.stderr)
        return 1
    print(f"Tailing {log_file} (last {lines} lines, Ctrl+C to stop):\n", file=sys.stderr)
    tail = shutil.which("tail")
    if tail:
        return subprocess.run([tail, "-n", str(lines), "-f", str(log_file)]).returncode
    # No `tail` (e.g. Windows without coreutils): print the bounded tail and return.
    print(stack_logs(config, lines))
    return 0


# --- agent tool adapters --------------------------------------------------------------------------

def _stack_up(open_browser: bool = True, with_services: bool = False) -> str:
    return stack_up(_cfg, open_browser=open_browser, with_services=with_services)


def _stack_stop() -> str:
    return stack_stop(_cfg)


def _stack_restart() -> str:
    return stack_restart(_cfg)


def _stack_status() -> str:
    return stack_status(_cfg)


def _stack_ps() -> str:
    return stack_ps(_cfg)


def _stack_logs(lines: int = 50) -> str:
    return stack_logs(_cfg, lines=lines)


def _litellm_control(action: str = "status") -> str:
    return litellm_control(_cfg, action=action)


def _whisper_control(action: str = "status") -> str:
    return whisper_control(_cfg, action=action)


def _piper_control(action: str = "status") -> str:
    return piper_control(_cfg, action=action)


def _services_control(action: str = "status") -> str:
    return services_control(_cfg, action=action)


_ACTION_ENUM = {"type": "string", "enum": ["start", "stop", "status"],
                "description": "start (background), stop, or status. Default status."}

TOOL_DEFS = [
    {"type": "function", "function": {
        "name": "stack_up",
        "description": ("Bring the local inference stack up in the background: llama-swap endpoint + "
                        "LiteLLM proxy (+ whisper STT if voice is enabled) + Open WebUI. Use when the "
                        "user wants to start Bob / the models / the endpoint."),
        "parameters": {"type": "object", "properties": {
            "open_browser": {"type": "boolean", "description": "Open the WebUI in a browser (default true)."},
            "with_services": {"type": "boolean", "description": "Also start Docker services (default false)."}}}}},
    {"type": "function", "function": {
        "name": "stack_stop",
        "description": "Stop all Bob services (frees VRAM): the endpoint, proxy, WebUI, voice servers, and Docker services.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "stack_restart",
        "description": "Restart the inference endpoint in the background (stop, then start + wait for ready).",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "stack_status",
        "description": ("Show which models are loaded and their VRAM state (queries the endpoint) plus "
                        "whisper/piper server status. Read-only. Use when the user asks what's running / "
                        "loaded / the model status."),
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "stack_ps",
        "description": "Show Bob's running daemons with PID, RAM, uptime, and liveness. Read-only.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "stack_logs",
        "description": "Read the last N lines of the endpoint log (logs/llama-swap.log). Read-only.",
        "parameters": {"type": "object", "properties": {
            "lines": {"type": "integer", "description": "How many trailing lines (default 50)."}}}}},
    {"type": "function", "function": {
        "name": "litellm_control",
        "description": "Manage the LiteLLM proxy (:8081). start brings it up in the background.",
        "parameters": {"type": "object", "properties": {"action": _ACTION_ENUM}}}},
    {"type": "function", "function": {
        "name": "whisper_control",
        "description": "Manage the whisper STT server. start brings it up in the background.",
        "parameters": {"type": "object", "properties": {"action": _ACTION_ENUM}}}},
    {"type": "function", "function": {
        "name": "piper_control",
        "description": "Manage the piper TTS server. start brings it up in the background.",
        "parameters": {"type": "object", "properties": {"action": _ACTION_ENUM}}}},
    {"type": "function", "function": {
        "name": "services_control",
        "description": "Manage optional Docker services (Langfuse / SearXNG / n8n): start|stop|status|logs.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["start", "stop", "status", "logs"],
                       "description": "Default status."}}}}},
]

DISPATCH = {
    "stack_up": _stack_up, "stack_stop": _stack_stop, "stack_restart": _stack_restart,
    "stack_status": _stack_status, "stack_ps": _stack_ps, "stack_logs": _stack_logs,
    "litellm_control": _litellm_control, "whisper_control": _whisper_control,
    "piper_control": _piper_control, "services_control": _services_control,
}
