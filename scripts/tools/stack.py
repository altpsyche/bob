"""Bob lifecycle capabilities (ONE-C Slice 2) — the local inference stack: llama-swap endpoint, LiteLLM
proxy, Open WebUI, whisper STT, piper TTS, and optional Docker services.

Functional grouping (D6): one module, several related tool fns, each reached three ways with no
duplicated logic — the agent tool (DISPATCH), the `bob <verb>` cli handler (scripts/bob/cli.py), and
`bob --run <cap>`. Launches binaries DIRECTLY via osenv.start_detached (no pwsh wrapper) and reaps via
the osenv process seams.

Config regeneration (`gen`) runs the Python generators (scripts/tools/generate.py) via the
single-sourced bob_models.regenerate_configs; bring-up does a best-effort regen, then requires
config/llama-swap.yaml + config/litellm.yaml (checked-in-locally, gitignored), erroring only if
they're absent."""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

_cfg: dict = {}

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO / "scripts"

# THE single source of truth for every service Bob manages. Each op — status/health, `ps`, stop,
# name-kill, the grouped dashboard — reads from THIS list instead of its own hardcoded copy (there used
# to be five). One entry per service:
#   name      pidfile key + display name (logs/<name>.{pid,log})
#   port      config port key (bob_core._port)
#   group     dashboard grouping
#   desc      one-line description
#   pidfile   True if it's a Bob-launched daemon tracked by logs/<name>.pid (→ appears in `ps`, stopped
#             by pidfile tree-kill)
#   procnames process names for the pkill fallback — reaped by name so they die even with a stale/missing
#             pidfile (empty = pidfile-only teardown). open-webui is here: it's detached and a prior stop
#             unlinks its pidfile, so a name-kill is the only thing that can still find a reparented WebUI.
#   docker    True if it's a docker-compose service (stopped via `docker compose down`, no pidfile)
#   core      True if it's part of core inference (what ensure_inference starts)
#   hint      the command that starts it (shown on a `down` line so the dashboard is actionable, and in
#             the TUI cockpit's toggle routing)
SERVICES = [
    {"name": "llama-swap", "label": "endpoint", "port": "port", "group": "Inference", "pidfile": True,
     "procnames": ("llama-swap", "llama-server"), "core": True, "hint": "bob up",
     "desc": "llama.cpp via llama-swap"},
    {"name": "litellm",    "label": "api", "port": "litellmPort", "group": "Inference", "pidfile": True,
     "procnames": (), "core": True, "hint": "bob up",
     "desc": "OpenAI-compatible proxy — point any client here"},
    {"name": "whisper",    "port": "sttPort",     "group": "Voice", "pidfile": True,
     "procnames": ("whisper-server",), "hint": "bob whisper start", "desc": "speech-to-text"},
    {"name": "piper",      "port": "ttsPort",     "group": "Voice", "pidfile": True,
     "procnames": (), "hint": "bob piper start",
     "desc": "text-to-speech server (optional; voice also works without it)"},
    {"name": "open-webui", "label": "webui", "port": "webuiPort", "group": "Web & automation", "pidfile": True,
     "procnames": ("open-webui",), "hint": "bob up", "desc": "Open WebUI — browser chat"},
    {"name": "searxng",    "port": "searxngPort", "group": "Web & automation", "docker": True,
     "hint": "bob services start", "desc": "private web search"},
    {"name": "n8n",        "port": "n8nPort",     "group": "Web & automation", "docker": True,
     "hint": "bob services start", "desc": "workflow automation"},
    {"name": "langfuse",   "port": "langfusePort", "group": "Web & automation", "docker": True,
     "hint": "bob services start", "desc": "tracing / observability"},
    {"name": "agent-api",  "port": "agentPort",   "group": "Agent", "hint": "bob agent serve",
     "desc": "bob agent serve — REST/SSE"},
]

# Derived views — kept as module constants so callers/tests read one canonical list, never a fresh copy.
_NAME_KILL = [p for s in SERVICES for p in s.get("procnames", ())]
_PS_SERVICES = [s["name"] for s in SERVICES if s.get("pidfile")]


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


def service_snapshot(config: dict) -> list:
    """Structured up/down snapshot of EVERY service — the ONE data source for both the plain-text
    dashboard (_service_health_lines / `bob status`) and the TUI's rich cockpit table (shell._render_
    dashboard), so the two can't drift. Ordered by the SERVICES registry (already grouped). Each row:
    name / label / group / port / up / url / hint / desc."""
    osenv = _osenv()
    from bob_core import _port
    rows = []
    for s in SERVICES:
        p = _port(config, s["port"])
        rows.append({
            "name": s["name"], "label": s.get("label", s["name"]), "group": s["group"],
            "port": p, "up": osenv.is_port_in_use(p), "url": f"http://localhost:{p}",
            "hint": s.get("hint", "bob up"), "desc": s["desc"],
        })
    return rows


def _service_health_lines(config: dict) -> list:
    """One-glance up/down for EVERY component (inference, voice, web/automation, agent), always shown —
    so `bob status` answers 'is SearXNG / n8n / WebUI actually running?' in one place. Renders the one
    service_snapshot; piper is labelled optional (CLI/voice TTS uses the binary directly, so a down
    :8083 server doesn't mean voice is broken)."""
    out, seen_group = ["", "Services"], None
    for r in service_snapshot(config):
        if r["group"] != seen_group:
            out.append(f"  {r['group']}:")
            seen_group = r["group"]
        mark = "UP  " if r["up"] else "down"
        # Actionable: a running service shows its URL (to open); a down one shows how to start it.
        detail = r["url"] if r["up"] else f"→ start: {r['hint']}"
        out.append(f"    {mark}  {r['label']:<10} :{str(r['port']):<5}  {detail:<26}  {r['desc']}")
    return out


def stack_status(config: dict) -> str:
    """The system dashboard: loaded models + VRAM (when the endpoint is up) AND a full up/down table for
    every service (inference / voice / web+automation / agent) — shown even when the endpoint is down, so
    the ops surface reads as one system, not a scatter of separate daemons."""
    import bob_models
    from bob_core import _port

    port = _port(config, "port")
    base = f"http://localhost:{port}/v1"
    try:
        data = _http_json(f"{base}/models", timeout=3)
    except (OSError, ValueError):
        data = None

    if data is None:
        lines = ["", f"Endpoint: {base}  [down]   start it: bob serve  (or bob up)"]
    else:
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

    lines += _service_health_lines(config)
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
    """Canonical teardown: name-kill the daemons that carry process names (survive stale pidfiles),
    tree-kill every pidfile-tracked service and clean its pidfile, then `docker compose down`. All three
    read the one SERVICES registry — no hardcoded per-service lists."""
    osenv = _osenv()
    killed = []

    # 1) By process name (survives a stale/missing pidfile). _NAME_KILL is derived from SERVICES.
    killed += osenv.stop_processes_by_name(_NAME_KILL)

    # 2) Every pidfile-tracked service: child-reaping tree kill (a no-op if step 1 already got it),
    #    then drop the pidfile. One loop over the registry replaces the old two hardcoded lists.
    for svc in _PS_SERVICES:
        pid = _read_pid(svc)
        if pid is not None and osenv.pid_alive(pid):
            osenv.stop_process_tree(pid)
            killed.append(svc)
        _pidfile(svc).unlink(missing_ok=True)

    # 3) Docker services.
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


def _swap_launch(config: dict) -> tuple:
    """The ONE llama-swap launch spec — (exe, argv, env_add, port) — shared by the background
    (_start_endpoint_bg) and foreground (serve_foreground) starts, so the exe path, llama-swap.yaml
    path, --listen address, and LLAMA_LOCAL_ROOT env can never drift between them. `env_add` is the
    environment ADDITIONS only: start_detached merges them into os.environ, and the foreground path
    merges them itself."""
    osenv = _osenv()
    from bob_core import _port

    exe = osenv.bin_exe("llama-swap")
    port = _port(config, "port")
    swap_cfg = REPO / "config" / "llama-swap.yaml"
    argv = [str(exe), "--config", str(swap_cfg), "--listen", f"127.0.0.1:{port}"]
    env_add = {"LLAMA_LOCAL_ROOT": str(REPO).replace("\\", "/")}
    return exe, argv, env_add, port


def _swap_missing_msg(exe) -> str:
    return f"{exe.name} missing — run: python -m bob.kernel build-swap (or drop the binary in bin/)."


def _start_endpoint_bg(config: dict) -> tuple:
    """Launch the CORE inference pair — llama-swap + LiteLLM — detached, then poll until the proxy the
    loop/clients actually call is reachable. Returns (ok, status-lines). CORE ONLY: whisper/WebUI/Docker
    are NOT started here — that's the caller's (stack_up's) concern, so 'start inference' means exactly
    one thing in exactly one place. Idempotent: a llama-swap already on :port short-circuits."""
    osenv = _osenv()
    from bob_core import _port

    exe, argv, env_add, port = _swap_launch(config)
    if not exe.exists():
        return (False, [_swap_missing_msg(exe)])
    lines = []
    if osenv.is_port_in_use(port):
        return (True, [f"Port {port} already in use — the endpoint is probably already running "
                       f"(bob stop to free it)."])
    pid = osenv.start_detached(
        argv, pidfile=_pidfile("llama-swap"), log_path=_logfile("llama-swap"), env=env_add)
    lines.append(f"Endpoint: http://localhost:{port}/v1 (PID {pid})")
    litellm_line = _start_litellm_bg(config)
    lines.append(litellm_line)
    # The loop/clients call the LiteLLM proxy, not llama-swap directly — so gate "ready" on :8081 too,
    # unless the proxy was skipped (no venv). LiteLLM/uvicorn boots seconds after launch; without this
    # we'd return while the proxy is still cold and the caller's first turn races it. TCP connect
    # (is_port_in_use), not an HTTP GET, because LiteLLM answers /v1/models with 401 when up.
    litellm_port = _port(config, "litellmPort")
    litellm_expected = "skipped" not in litellm_line

    def ready():
        if not (osenv.pid_alive(pid) and _http_ok(f"http://localhost:{port}/v1/models", timeout=2)):
            return False
        return osenv.is_port_in_use(litellm_port) if litellm_expected else True

    ok = _poll(ready, timeout=60, interval=0.3)
    lines.append("Endpoint ready." if ok else "Endpoint did not respond in 60s — check: bob logs")
    return (ok and osenv.pid_alive(pid), lines)


def ensure_inference(config: dict) -> tuple:
    """THE single 'make core inference reachable' operation. Everything that needs the LLM — the shell /
    `bob chat` / `bob agent` auto-start, `bob up`, `bob restart` — composes THIS instead of re-launching
    llama-swap+LiteLLM itself, so what-starts-inference lives in one place. No-op (already reachable) when
    the LiteLLM proxy answers a TCP connect. Returns (ok, status-lines)."""
    from bob_core import check_litellm
    if check_litellm(config):
        return (True, ["Inference already running."])
    return _start_endpoint_bg(config)


def stack_up(config: dict, open_browser: bool = True, with_services: bool = False) -> str:
    """The persistent 'bring up everything for outside-terminal use': core inference + (whisper if voice)
    + Open WebUI, then optionally open the browser and start Docker services. Composes ensure_inference
    (the one place that starts inference) rather than re-launching it — so `bob up` and the auto-start
    share identical core-start behaviour; `bob up` just adds the extras."""
    osenv = _osenv()
    from bob_core import _port

    err = _ensure_configs()
    if err:
        return err
    ok, lines = ensure_inference(config)
    if config.get("voice", {}).get("enabled"):    # STT is a voice extra, not part of core inference
        lines.append(_start_whisper_bg(config))

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
    """Background restart: bounce the endpoint + proxy + WebUI (core inference plus open-webui — voice
    servers survive), then bring core inference back via the one ensure_inference. The restart set is
    derived from the SERVICES registry, not another hardcoded list."""
    osenv = _osenv()
    restart = [s for s in SERVICES if s.get("core") or s["name"] == "open-webui"]
    osenv.stop_processes_by_name([p for s in restart for p in s.get("procnames", ())])
    for svc in [s["name"] for s in restart]:
        pid = _read_pid(svc)
        if pid is not None and osenv.pid_alive(pid):
            osenv.stop_process_tree(pid)
        _pidfile(svc).unlink(missing_ok=True)
    time.sleep(0.5)
    err = _ensure_configs()
    if err:
        return err
    ok, lines = ensure_inference(config)   # the one core-start op, same as auto-start / `bob up`
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


# The individually-controllable Bob daemons — the ONE table that drives service_control (start/stop/
# status), the per-service `bob <name>` verbs, and the per-service agent tools. Each entry carries its
# start fn + display label + port key + health-URL suffix, so litellm/whisper/piper are no longer three
# hand-written near-identical control functions (nor three hand-written CLI handlers / tool defs).
_DAEMON_CONTROL = {
    "litellm": {"start": _start_litellm_bg, "label": "LiteLLM",
                "port": "litellmPort", "url_suffix": "/v1", "desc": "the LiteLLM proxy (:8081)"},
    "whisper": {"start": _start_whisper_bg, "label": "whisper-server",
                "port": "sttPort", "url_suffix": "", "desc": "the whisper STT server"},
    "piper":   {"start": _start_piper_bg,   "label": "piper-server",
                "port": "ttsPort", "url_suffix": "", "desc": "the piper TTS server"},
}


def service_control(config: dict, name: str, action: str = "start") -> str:
    """Generic start/stop/status for a Bob-launched daemon (litellm/whisper/piper), driven off the
    _DAEMON_CONTROL table — the one place the per-service lifecycle logic lives. `bob litellm|whisper|
    piper`, the matching agent tools, provisioning smoke, and the shell's /voice preflight all route here."""
    from bob_core import _port
    d = _DAEMON_CONTROL.get(name)
    if d is None:
        return f"Unknown service '{name}'. Known: {', '.join(_DAEMON_CONTROL)}"
    if action == "stop":
        return _service_stop(name, d["label"])
    if action == "status":
        return _service_status(name, d["label"], _port(config, d["port"]), url_suffix=d["url_suffix"])
    return d["start"](config)


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

    err = _ensure_configs()
    if err:
        print(err, file=sys.stderr)
        return 1
    exe, argv, env_add, port = _swap_launch(config)   # same launch spec as the background start
    if not exe.exists():
        print(_swap_missing_msg(exe), file=sys.stderr)
        return 1
    print(_start_litellm_bg(config), file=sys.stderr)
    if config.get("voice", {}).get("enabled"):
        print(_start_whisper_bg(config), file=sys.stderr)
    if osenv.is_port_in_use(port):
        print(f"Port {port} already in use — the endpoint is probably already running (bob stop).",
              file=sys.stderr)
        return 0
    print(f"Endpoint: http://localhost:{port}/v1   (loopback only; Ctrl+C to stop)", file=sys.stderr)
    return subprocess.run(argv, env={**os.environ, **env_add}).returncode


def webui_foreground(config: dict) -> int:
    """`bob webui` — run Open WebUI in the foreground (opt-in, blocking)."""
    osenv = _osenv()
    from bob_core import _port

    webui = osenv.venv_exe("venv-webui", "open-webui")
    if not webui.exists():
        print("Open WebUI not installed (opt-in). Re-run setup with --with-webui", file=sys.stderr)
        return 1
    port = _port(config, "webuiPort")
    # If something is already serving :webuiPort (e.g. a `bob up` started WebUI in the background),
    # a foreground `serve` would just fail to bind. Point at the running instance instead of crashing.
    if osenv.is_port_in_use(port):
        url = f"http://localhost:{port}"
        print(f"Open WebUI is already running at {url} (port {port} in use — likely from `bob up`). "
              f"Opening it; run `bob stop` first if you want a fresh foreground instance.", file=sys.stderr)
        osenv.open_url(url)
        return 0
    return subprocess.run([str(webui), "serve", "--port", str(port)]).returncode


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


def _daemon_adapter(name: str):
    """One agent-tool adapter per daemon, bound to its name — generated, not written three times."""
    def adapter(action: str = "status") -> str:
        return service_control(_cfg, name, action=action)
    return adapter


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
        "name": "services_control",
        "description": "Manage optional Docker services (Langfuse / SearXNG / n8n): start|stop|status|logs.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["start", "stop", "status", "logs"],
                       "description": "Default status."}}}}},
] + [
    # The per-daemon control tools (litellm_control / whisper_control / piper_control) are generated
    # from the one _DAEMON_CONTROL table — same three names, no hand-repeated defs.
    {"type": "function", "function": {
        "name": f"{_n}_control",
        "description": f"Manage {_d['desc']}. start brings it up in the background.",
        "parameters": {"type": "object", "properties": {"action": _ACTION_ENUM}}}}
    for _n, _d in _DAEMON_CONTROL.items()
]

DISPATCH = {
    "stack_up": _stack_up, "stack_stop": _stack_stop, "stack_restart": _stack_restart,
    "stack_status": _stack_status, "stack_ps": _stack_ps, "stack_logs": _stack_logs,
    "services_control": _services_control,
    **{f"{_n}_control": _daemon_adapter(_n) for _n in _DAEMON_CONTROL},
}
