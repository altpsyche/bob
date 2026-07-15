"""Bob lifecycle capabilities — the local inference stack: llama-swap endpoint, LiteLLM
proxy, Open WebUI, whisper STT, piper TTS, and optional Docker services.

Functional grouping: one module, several related tool fns, each reached three ways with no
duplicated logic — the agent tool (DISPATCH), the `bob <verb>` cli handler (scripts/bob/cli.py), and
`bob --run <cap>`. Launches binaries DIRECTLY via osenv.start_detached and reaps via
the osenv process seams.

Config regeneration (`gen`) runs the Python generators (scripts/tools/generate.py) via the
single-sourced bob_models.regenerate_configs; bring-up does a best-effort regen, then requires
config/llama-swap.yaml + config/litellm.yaml (checked-in-locally, gitignored), erroring only if
they're absent."""
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

_cfg: dict = {}

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO / "scripts"

# THE single source of truth for every service Bob manages. Each op — status/health, `ps`, stop,
# name-kill, the grouped dashboard, and start (service_control) — reads from THIS list. One entry per
# service; the `start` callables are bound onto their entries just below their definitions (see
# _bind_start_fns), so the registry stays the one table and there is no parallel control map.
#   name      pidfile key + display name (logs/<name>.{pid,log})
#   label     short cockpit/status label (defaults to name)
#   port      config port key (bob_core._port)
#   group     dashboard grouping
#   desc      one-line description
#   kind      "native"   Bob-launched daemon tracked by logs/<name>.pid (appears in `ps`, tree-killed on
#                        stop); has a `start` fn (except llama-swap/open-webui, launched by stack_up)
#             "docker"   a docker-compose service (guided-install + `docker compose up`, no pidfile)
#             "external" managed by its own verb (agent-api via `bob agent serve`); port-checked only
#   requires  optional capability gate, e.g. "docker" (triggers the generic guided install on start)
#   policy    "core" (started by ensure_inference) or "lazy" (opt-in / on-demand; not started by `bob up`)
#   start     the launch fn (bound below for native daemons that service_control starts)
#   url_suffix path appended to the status URL (e.g. "/v1"); default ""
#   procnames process names for the pkill fallback — reaped by name so they die even with a stale/missing
#             pidfile (empty = pidfile-only teardown). open-webui is here: it's detached and a prior stop
#             unlinks its pidfile, so a name-kill is the only thing that can still find a reparented WebUI.
#   hint      the command that starts it (shown on a `down` line so the dashboard is actionable, and in
#             the TUI cockpit's toggle routing)
SERVICES = [
    {"name": "llama-swap", "label": "endpoint", "port": "port", "group": "Inference", "kind": "native",
     "procnames": ("llama-swap", "llama-server"), "core": True, "policy": "core", "hint": "bob up",
     "desc": "llama.cpp via llama-swap"},
    {"name": "litellm",    "label": "api", "port": "litellmPort", "group": "Inference", "kind": "native",
     "procnames": (), "core": True, "policy": "core", "url_suffix": "/v1", "agent_control": True,
     "hint": "bob up", "desc": "OpenAI-compatible proxy; point any client here"},
    {"name": "whisper",    "port": "sttPort", "group": "Voice", "kind": "native",
     "procnames": ("whisper-server",), "policy": "lazy", "agent_control": True, "hint": "bob whisper start",
     "desc": "speech-to-text"},
    {"name": "piper",      "port": "ttsPort", "group": "Voice", "kind": "native",
     "procnames": (), "policy": "lazy", "agent_control": True, "hint": "bob piper start",
     "desc": "text-to-speech server (optional; voice also works without it)"},
    {"name": "open-webui", "label": "webui", "port": "webuiPort", "group": "Web & automation", "kind": "native",
     "procnames": ("open-webui",), "policy": "lazy", "hint": "bob up", "desc": "Open WebUI, browser chat"},
    {"name": "n8n",        "port": "n8nPort",     "group": "Web & automation", "kind": "native",
     "procnames": ("n8n",), "policy": "lazy", "hint": "bob services n8n start",
     "desc": "workflow automation (native, opt-in)"},
    {"name": "searxng",    "port": "searxngPort", "group": "Web & automation", "kind": "docker",
     "requires": "docker", "policy": "lazy", "hint": "bob services searxng start",
     "desc": "private meta-search (optional; ddgs is the default)"},
    {"name": "langfuse",   "port": "langfusePort", "group": "Web & automation", "kind": "docker",
     "requires": "docker", "policy": "lazy", "hint": "bob services langfuse start",
     "desc": "tracing / observability (optional)"},
    {"name": "agent-api",  "port": "agentPort",   "group": "Agent", "kind": "external", "hint": "bob agent serve",
     "desc": "bob agent serve (REST/SSE)"},
]

# Derived views — kept as module constants so callers/tests read one canonical list, never a fresh copy.
_NAME_KILL = [p for s in SERVICES for p in s.get("procnames", ())]
_PS_SERVICES = [s["name"] for s in SERVICES if s.get("kind") == "native"]


def _svc(name: str):
    """The one SERVICES lookup by name (None if unknown)."""
    return next((s for s in SERVICES if s["name"] == name), None)


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
    return _osenv().cache_dir()  # repo logs/ by default; BOB_DATA_DIR/logs when overridden


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
    which runs the Python generators directly."""
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
    docker_ok = osenv.docker_present()   # checked once; docker services can't run at all without it
    rows = []
    for s in SERVICES:
        p = _port(config, s["port"])
        is_docker = s.get("kind") == "docker"
        rows.append({
            "name": s["name"], "label": s.get("label", s["name"]), "group": s["group"],
            "port": p, "up": osenv.is_port_in_use(p), "url": f"http://localhost:{p}",
            "hint": s.get("hint", "bob up"), "desc": s["desc"],
            "core": bool(s.get("core")), "docker": is_docker,
            # A docker service on a box with no docker isn't "down" (startable) -- it's unavailable
            # until docker is installed. Surfacing that stops the misleading "start: bob services".
            "unavailable": s.get("requires") == "docker" and not docker_ok,
        })
    return rows


def _service_health_lines(config: dict) -> list:
    """One-glance up/down for EVERY component (inference, voice, web/automation, agent), always shown —
    so `bob status` answers 'is SearXNG / n8n / WebUI actually running?' in one place. Renders the one
    service_snapshot; piper is labelled optional (CLI/voice TTS uses the binary directly, so a down
    :8083 server doesn't mean voice is broken)."""
    osenv = _osenv()
    out, seen_group = ["", "Services"], None
    for r in service_snapshot(config):
        if r["group"] != seen_group:
            out.append(f"  {r['group']}:")
            seen_group = r["group"]
        # Actionable: a running service shows its URL (to open); a down one shows how to start it; a
        # docker service on a box with no docker is n/a with the reason + install hint.
        if r.get("unavailable"):
            mark, detail = "n/a ", f"needs Docker: {osenv.docker_install_hint()}"
        elif r["up"]:
            mark, detail = "UP  ", r["url"]
        else:
            mark, detail = "down", f"→ start: {r['hint']}"
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

    # Engine build-tier badge — surfaces a GPU box running a CPU-only engine right on the dashboard.
    marker = _osenv().build_tier_marker()
    if marker:
        tier = marker.get("tier", "?")
        gpu = _osenv().gpu_arch()
        if tier == "cpu" and gpu:
            lines.append(f"Engine:   CPU-only build  (GPU {gpu['Gen']} idle -- see: bob diagnose)")
        else:
            lines.append(f"Engine:   {tier} build ({marker.get('source', 'source')})")

    lines += _service_health_lines(config)
    lines.append("")
    return "\n".join(lines)


# canonical role order (mirrors models.py)
_ROLE_ORDER = ["ponder", "coder", "chat", "fim", "embed"]


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
        return "LiteLLM venv not found; proxy skipped (run: python -m bob.kernel venv litellm)."
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
        return f"whisper-server missing ({exe.name}). run: bob setup-voice"
    if not model.exists():
        return f"Whisper model not found at {model}. run: bob setup-voice"
    osenv.stop_processes_by_name("whisper-server")  # reap stale (VRAM leak guard)
    _pidfile("whisper").unlink(missing_ok=True)
    pid = osenv.start_detached(
        [str(exe), "--model", str(model), "--port", str(stt_port), "--host", "127.0.0.1"],
        pidfile=_pidfile("whisper"), log_path=_logfile("whisper"))
    ready = _poll(lambda: osenv.is_port_in_use(stt_port), timeout=30, interval=0.5)
    tail = "ready" if ready else "may not be ready yet — check logs/whisper.log"
    return f"whisper-server: http://localhost:{stt_port} (PID {pid}), {tail}"


def _stt_health_ok(port: int) -> bool:
    """True when the STT server answers /health with a loaded model. Accurate readiness: the model loads
    before the port binds and /health reports 'ok' only once loading finished, so this can't false-positive
    on a still-loading (or an outgoing, refusing) server the way a bare port-open check can."""
    import json as _json
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/health", timeout=2) as r:  # noqa: S310
            return _json.loads(r.read().decode("utf-8", "replace")).get("status") == "ok"
    except Exception:  # noqa: BLE001 — refused/other == not ready yet
        return False


def _start_faster_whisper_bg(config: dict) -> str:
    """Start the faster-whisper (CTranslate2) STT server under venv-litellm. Same sttPort + /inference
    contract as whisper.cpp, so the client and lifecycle are engine-agnostic. The model loads once before
    the port binds, and a GPU cold-load can take several seconds, so readiness is a /health poll (not a
    bare port check) after waiting for any prior server to release the port."""
    osenv = _osenv()
    from bob_core import _port

    stt_port = _port(config, "sttPort")
    voice = config.get("voice", {})
    size = voice.get("sttModel", "small")
    model_dir = REPO / "models" / "faster-whisper" / size
    py = osenv.venv_exe("venv-litellm", "python")
    server = SCRIPTS / "faster_whisper_server.py"
    if not py.exists():
        return "venv-litellm not found. run: python -m bob.kernel venv litellm"
    pid = _read_pid("whisper")
    if pid is not None and osenv.pid_alive(pid):
        return f"faster-whisper already running (PID {pid})."
    osenv.stop_processes_by_name("whisper-server")  # reap a stale whisper.cpp server on engine switch
    _pidfile("whisper").unlink(missing_ok=True)
    # Wait for a prior STT server to release the port so a restart never overlaps (and the readiness
    # check below can't see the outgoing server).
    _poll(lambda: not osenv.is_port_in_use(stt_port), timeout=10, interval=0.3)
    new_pid = osenv.start_detached(
        [str(py), str(server)], pidfile=_pidfile("whisper"), log_path=_logfile("whisper"),
        env={"STT_PORT": str(stt_port), "STT_MODEL": size, "STT_MODEL_DIR": str(model_dir),
             "STT_COMPUTE_TYPE": voice.get("sttComputeType", "auto")})
    ready = _poll(lambda: _stt_health_ok(stt_port), timeout=90, interval=0.5)
    tail = "ready" if ready else "may not be ready yet — check logs/whisper.log"
    return f"faster-whisper: http://localhost:{stt_port} (PID {new_pid}, model={size}), {tail}"


def _start_stt_bg(config: dict) -> str:
    """Start the STT server for the configured backend. voice.sttEngine picks faster-whisper (the 1.2
    default, in venv-litellm) or whisper.cpp (the compiled bin/whisper-server fallback)."""
    engine = (config.get("voice", {}) or {}).get("sttEngine", "faster-whisper")
    if engine == "whisper.cpp":
        return _start_whisper_bg(config)
    return _start_faster_whisper_bg(config)


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
        return "piper binary not found. run: bob setup-voice"
    if not voice_path.exists():
        return f"Voice model not found at {voice_path}. run: bob setup-voice"
    if not py.exists():
        return "venv-litellm not found. run: python -m bob.kernel venv litellm"
    pid = _read_pid("piper")
    if pid is not None and osenv.pid_alive(pid):
        return f"piper-server already running (PID {pid})."
    _pidfile("piper").unlink(missing_ok=True)
    new_pid = osenv.start_detached(
        [str(py), str(server)], pidfile=_pidfile("piper"), log_path=_logfile("piper"),
        env={"PIPER_EXE": str(piper_exe), "PIPER_VOICE": str(voice_path), "PIPER_PORT": str(tts_port)})
    ready = _poll(lambda: osenv.is_port_in_use(tts_port), timeout=20, interval=0.5)
    tail = "ready" if ready else "may not be ready — check logs/piper.log"
    return f"piper-server: http://localhost:{tts_port} (PID {new_pid}, voice={voice}), {tail}"


# n8n runs native (pure Node, cross-OS) so a default install never needs Docker for it. It needs Node
# 20.19–24.x; the guard skips a broken install on an out-of-range node instead of failing (n8n is opt-in).
_N8N_NODE_MIN = (20, 19)
_N8N_NODE_MAX_MAJOR = 24


def _node_version() -> tuple:
    """(major, minor) of the `node` on PATH, or None if absent/unparseable."""
    try:
        out = subprocess.run(["node", "--version"], check=False,
                             capture_output=True, text=True).stdout.strip()
    except OSError:
        return None
    m = re.match(r"v?(\d+)\.(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _n8n_exe() -> Path:
    osenv = _osenv()
    binname = "n8n.cmd" if osenv.os_name() == "windows" else "n8n"
    return REPO / "tools" / "n8n" / "node_modules" / ".bin" / binname


def _start_n8n_bg(config: dict) -> str:
    """Start n8n natively, installing it on demand into tools/n8n on first use. Node-version guarded:
    an out-of-range node skips cleanly with an upgrade hint rather than installing a broken n8n."""
    osenv = _osenv()
    from bob_core import _port

    ver = _node_version()
    if ver is None:
        return "Node.js not found. Install Node 20.19–24.x, then: bob services n8n start"
    if ver < _N8N_NODE_MIN or ver[0] > _N8N_NODE_MAX_MAJOR:
        return (f"n8n needs Node 20.19–24.x; found v{ver[0]}.{ver[1]}. "
                "Upgrade Node, then: bob services n8n start")
    port = _port(config, "n8nPort")
    if osenv.is_port_in_use(port):
        return f"n8n already running on :{port}."
    exe = _n8n_exe()
    if not exe.exists():
        pin = (config.get("agent", {}) or {}).get("n8nVersion") or "latest"
        print(f"Installing n8n@{pin} (first run; this can take a minute)...", file=sys.stderr)
        if not osenv.npm_local_install(f"n8n@{pin}", REPO / "tools" / "n8n") or not exe.exists():
            return "n8n install failed (npm). Check Node/npm, then retry: bob services n8n start"
    _pidfile("n8n").unlink(missing_ok=True)
    data = REPO / "tools" / "n8n-data"
    data.mkdir(parents=True, exist_ok=True)
    pid = osenv.start_detached(
        [str(exe), "start"], pidfile=_pidfile("n8n"), log_path=_logfile("n8n"),
        env={"N8N_USER_FOLDER": str(data), "N8N_PORT": str(port), "N8N_HOST": "localhost",
             "N8N_PROTOCOL": "http", "WEBHOOK_URL": f"http://localhost:{port}",
             "N8N_ENCRYPTION_KEY": config.get("n8nEncryptionKey") or "local-bob-n8n-key",
             "GENERIC_TIMEZONE": config.get("n8nTimezone") or "UTC"})
    ready = _poll(lambda: osenv.is_port_in_use(port), timeout=90, interval=1.0)
    tail = "ready" if ready else "still starting — check logs/n8n.log"
    return f"n8n: http://localhost:{port} (PID {pid}), {tail}"


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
    return f"{exe.name} missing. run: python -m bob.kernel build-swap (or drop the binary in bin/)."


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
        return (True, [f"Port {port} already in use; the endpoint is probably already running "
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
    lines.append("Endpoint ready." if ok else "Endpoint did not respond in 60s; check: bob logs")
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


def ensure_deps(config: dict, inference: bool = False, stt: bool = False, search: bool = False) -> tuple:
    """The ONE 'bring up exactly the services this command needs' seam. inference → core LLM
    (ensure_inference, the shell/chat/agent auto-start); stt → the whisper STT server (the /voice
    preflight); search → the SearXNG container (web_search / music_play). Returns (all_ok,
    status-lines). Idempotent: each need no-ops when its service is already reachable, so callers can
    invoke it unconditionally."""
    from bob_core import _port
    osenv = _osenv()
    ok, lines = True, []
    if inference:
        i_ok, i_lines = ensure_inference(config)
        ok = ok and i_ok
        lines += i_lines
    if stt:
        if osenv.is_port_in_use(_port(config, "sttPort")):
            lines.append("whisper already running.")
        else:
            lines.append(service_control(config, "whisper", "start"))
            ok = ok and osenv.is_port_in_use(_port(config, "sttPort"))
    if search:
        s_ok, s_msg = ensure_searxng(config)
        ok = ok and s_ok
        lines.append(s_msg)
    return ok, lines


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
        lines.append(_start_stt_bg(config))

    # Open WebUI (opt-in; detached background process).
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
                lines.append(f"Open WebUI didn't respond; open manually: http://localhost:{webui_port}")
    else:
        lines.append("open-webui not installed (opt-in); skipping. (re-run setup with --with-webui)")

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


# Bind the native start fns onto their SERVICES entries. This is done here (below the fn definitions,
# where the old _DAEMON_CONTROL table lived) because the entries are declared at the top of the file
# before these fns exist. After this, SERVICES is the single control source: no parallel table.
# llama-swap / open-webui are native but launched by stack_up (not service_control), so they carry no
# `start`; agent-api is external.
def _bind_start_fns() -> None:
    for _name, _fn in (("litellm", _start_litellm_bg), ("whisper", _start_stt_bg),
                       ("piper", _start_piper_bg), ("n8n", _start_n8n_bg)):
        _svc(_name)["start"] = _fn


_bind_start_fns()


def _guided_docker_install(config: dict) -> tuple:
    """Ensure Docker is present, installing it through the package-manager seam if the user agrees.
    Returns (ok, message). Only prompts on a TTY: in agent/non-interactive contexts it returns
    (False, hint) so a search/tool call never blocks on stdin."""
    osenv = _osenv()
    if osenv.docker_present():
        return True, ""
    if not sys.stdin.isatty():
        return False, ("Docker is required for this service and is not installed. "
                       "Run it in a terminal to be walked through the Docker install: "
                       f"{osenv.docker_install_hint()}")
    print("This service needs Docker, which isn't installed.")
    try:
        ans = input("Install Docker now via your package manager? [Y/n] ").strip().lower()
    except EOFError:
        ans = "n"
    if ans not in ("", "y", "yes"):
        return False, f"Skipped. Install Docker yourself, then retry: {osenv.docker_install_hint()}"
    try:
        osenv.install_package("docker")
    except Exception as e:   # loud-fail: report, don't crash
        return False, f"Docker install failed: {e}. See {osenv.docker_install_hint()}"
    if osenv.os_name() != "windows" and shutil.which("systemctl"):
        subprocess.run(["sudo", "systemctl", "start", "docker"], check=False, capture_output=True)
    if not osenv.docker_present():
        return False, ("Docker installed but the daemon isn't ready yet "
                       "(a reboot or WSL2 setup may be needed on Windows). Start Docker, then retry.")
    return True, ""


def _docker_start(config: dict, svc: dict) -> str:
    """Start one docker-compose service, running the generic guided Docker install first if `requires:
    docker` and Docker is absent. Idempotent (a service already on its port short-circuits)."""
    from bob_core import _port
    osenv = _osenv()
    name = svc["name"]
    port = _port(config, svc["port"])
    if osenv.is_port_in_use(port):
        return f"{name} already running on :{port}."
    if svc.get("requires") == "docker":
        ok, msg = _guided_docker_install(config)
        if not ok:
            return msg
    base, err = _compose_base(config)
    if err:
        return err
    _write_compose_env(config)
    _prepare_docker_service(name)
    r = subprocess.run(base + ["up", "-d", name], check=False, capture_output=True, text=True)
    if r.returncode != 0:
        return f"{name} failed to start:\n" + (r.stderr or r.stdout).strip()
    ready = _poll(lambda: osenv.is_port_in_use(port), timeout=40, interval=0.5)
    return (f"{name} ready: http://localhost:{port}" if ready
            else f"{name} starting on :{port} (still warming up; retry shortly).")


def service_control(config: dict, name: str, action: str = "start") -> str:
    """The ONE per-service start/stop/status, routing by the registry entry's `kind`. Native daemons
    (litellm/whisper/piper/n8n) run their `start` fn / pidfile tree-kill / pidfile status; docker services
    (searxng/langfuse) go through the compose path with the generic guided Docker install. `bob litellm|
    whisper|piper`, `bob services <name> <action>`, the agent tools, provisioning smoke, and the shell's
    /voice preflight all route here."""
    from bob_core import _port
    svc = _svc(name)
    if svc is None:
        controllable = [s["name"] for s in SERVICES if s.get("start") or s.get("kind") == "docker"]
        return f"Unknown service '{name}'. Known: {', '.join(controllable)}"
    label = svc.get("label", name)
    if svc.get("kind") == "docker":
        if action == "stop":
            base, err = _compose_base(config)
            if err:
                return err
            subprocess.run(base + ["stop", name], check=False, capture_output=True)
            return f"{name} stopped."
        if action == "status":
            up = _osenv().is_port_in_use(_port(config, svc["port"]))
            return f"{name} {'running' if up else 'not running'} (:{_port(config, svc['port'])})"
        return _docker_start(config, svc)
    # native daemon
    if action == "stop":
        return _service_stop(name, label)
    if action == "status":
        return _service_status(name, label, _port(config, svc["port"]),
                               url_suffix=svc.get("url_suffix", ""))
    start = svc.get("start")
    if start is None:
        return f"'{name}' has no direct start (use: {svc.get('hint', 'bob up')})."
    return start(config)


def _compose_base(config: dict) -> tuple:
    """(base_cmd, '') for `docker compose -f <file>`, or (None, message) if docker/compose is
    unavailable. Shared by services_control + ensure_searxng."""
    compose = REPO / "tools" / "compose" / "docker-compose.yml"
    if not shutil.which("docker"):
        return None, "Docker not found. Install docker, then re-run setup (it provisions the compose services)."
    if not compose.exists():
        return None, f"No compose file at {compose}."
    return ["docker", "compose", "-f", str(compose)], ""


def _write_compose_env(config: dict) -> None:
    """Write tools/compose/.env with the resolved ports (compose reads it for up). Only SearXNG and
    Langfuse remain in compose (n8n is native), so only their ports are written."""
    from bob_core import _port
    env = REPO / "tools" / "compose" / ".env"
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text(
        f"REPO_PATH={REPO}\n"
        f"LANGFUSE_PORT={_port(config, 'langfusePort')}\n"
        f"SEARXNG_PORT={_port(config, 'searxngPort')}\n", encoding="utf-8")


def _prepare_docker_service(name: str) -> None:
    """Per-service on-demand prep (moved off the old eager setup_docker): write the SearXNG settings the
    container bind-mounts, and ensure Langfuse's Postgres data dir exists. Idempotent."""
    if name == "searxng":
        sx_cfg = REPO / "config" / "searxng" / "settings.yml"
        if not sx_cfg.exists():
            sx_cfg.parent.mkdir(parents=True, exist_ok=True)
            sx_cfg.write_text(
                'use_default_settings: true\nserver:\n  secret_key: "bob-searxng"\n'
                '  bind_address: "0.0.0.0:8080"\nsearch:\n  safe_search: 0\n  default_lang: "en"\n'
                '  formats:\n    - html\n    - json\n', encoding="utf-8")
    elif name == "langfuse":
        (REPO / "tools" / "langfuse-data").mkdir(parents=True, exist_ok=True)


def _lazy_service_names() -> list:
    """Opt-in add-ons (policy: lazy) that the group `bob services` verb operates on when no name is given."""
    return [s["name"] for s in SERVICES if s.get("policy") == "lazy"
            and s.get("kind") in ("native", "docker") and s["name"] not in ("whisper", "piper", "open-webui")]


def services_control(config: dict, action: str = "status", service: str = None) -> str:
    """The opt-in add-on lifecycle verb (`bob services [<name>] <start|stop|status|logs>`). Every named
    service, native (n8n) or docker (searxng/langfuse), routes through the one service_control; with no
    name it applies the action across the lazy add-ons. `logs` for docker services still uses compose."""
    if action not in ("start", "stop", "status", "logs"):
        return "Usage: bob services [<name>] start|stop|status|logs"
    names = [service] if service else _lazy_service_names()
    unknown = [n for n in names if _svc(n) is None]
    if unknown:
        return f"Unknown service(s): {', '.join(unknown)}. Known: {', '.join(_lazy_service_names())}"
    if action == "logs":
        svc = _svc(service) if service else None
        if svc is not None and svc.get("kind") == "docker":
            base, err = _compose_base(config)
            if err:
                return err
            r = subprocess.run(base + ["logs", "--tail=50", service], check=False,
                               capture_output=True, text=True)
            return r.stdout.strip()
        # native daemons log to logs/<name>.log
        target = service or (names[0] if names else "")
        if not target:
            return "Specify a service: bob services <name> logs"
        lf = _logfile(target)
        if not lf.exists():
            return f"No log yet for {target} (start it: {(_svc(target) or {}).get('hint', 'bob up')})."
        return "\n".join(lf.read_text(encoding="utf-8", errors="replace").splitlines()[-50:])
    return "\n".join(f"{n}: {service_control(config, n, action)}" for n in names)


def ensure_service(config: dict, name: str) -> tuple:
    """On-demand 'make this service reachable': no-op if its port already answers, else start it via the
    one service_control (native or docker). Best-effort: returns (ok, msg) so callers fall back
    gracefully. Every service-specific value comes from the SERVICES entry, so adding a service is a
    registry property, not hand-written code."""
    from bob_core import _port
    osenv = _osenv()
    svc = _svc(name)
    if svc is None:
        return False, f"Unknown service '{name}'."
    port = _port(config, svc["port"])
    if osenv.is_port_in_use(port):
        return True, f"{name} already running."
    msg = service_control(config, name, "start")
    return osenv.is_port_in_use(port), msg


def ensure_searxng(config: dict) -> tuple:
    """Thin alias over the generic ensure_service — 'make the SearXNG service reachable' (used when the
    user has selected SearXNG as their search provider). See ensure_service for behavior."""
    return ensure_service(config, "searxng")


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
        print(_start_stt_bg(config), file=sys.stderr)
    if osenv.is_port_in_use(port):
        print(f"Port {port} already in use; the endpoint is probably already running (bob stop).",
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
        print(f"Open WebUI is already running at {url} (port {port} in use, likely from `bob up`). "
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


def _services_control(action: str = "status", service: str = None) -> str:
    return services_control(_cfg, action=action, service=service)


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
        "description": ("Manage opt-in add-on services (n8n native; SearXNG / Langfuse via Docker): "
                        "start|stop|status|logs, optionally scoped to one service by name."),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["start", "stop", "status", "logs"],
                       "description": "Default status."},
            "service": {"type": "string", "description": "Optional: one service (n8n|searxng|langfuse)."}}}}},
] + [
    # The per-daemon control tools (litellm_control / whisper_control / piper_control) are generated
    # from the SERVICES entries flagged `agent_control` — the one registry, no hand-repeated defs.
    {"type": "function", "function": {
        "name": f"{_s['name']}_control",
        "description": f"Manage {_s['desc']}. start brings it up in the background.",
        "parameters": {"type": "object", "properties": {"action": _ACTION_ENUM}}}}
    for _s in SERVICES if _s.get("agent_control")
]

DISPATCH = {
    "stack_up": _stack_up, "stack_stop": _stack_stop, "stack_restart": _stack_restart,
    "stack_status": _stack_status, "stack_ps": _stack_ps, "stack_logs": _stack_logs,
    "services_control": _services_control,
    **{f"{_s['name']}_control": _daemon_adapter(_s["name"]) for _s in SERVICES if _s.get("agent_control")},
}
