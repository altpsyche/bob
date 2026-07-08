"""Bob health / diagnostics capabilities — the read-only pre-flight verbs.

Functional grouping: one module, several related tool fns, each reached three ways (agent tool /
`bob <verb>` / `bob --run`) with no duplicated logic. Ports the former setup(check)/doctor/version/
diagnose cases + the health-check core.

  health_check(config, doctor=False)  <- `bob setup check` (deps/registration) and `bob doctor` (+runtime)
  version_info(config)                <- `bob version`  (Bob release + binary/submodule versions)
  diagnose(config)                    <- `bob diagnose` (GPU/VRAM/profile/endpoint/models/manifest)

`diagnose` reports the deep build-time OS discovery too — CUDA-toolkit resolution, system RAM, NUMA
topology, mlock privilege, and the Linux package manager — via the build-time osenv seams
(osenv.best_cuda_root / system_ram_gb / numa_node_count / mlock_status / linux_package_manager).

Two rows that used to degrade are now wired: the BobAgent scheduled-task check reads
osenv.agent_task_status() (the scheduler quartet), and doctor's versions.lock reproducibility
section reads bob.versions.check_reproducibility(). A missing lock or an unregistered task is
reported as informational (both are opt-in), not a failure."""
import sys
from pathlib import Path

_cfg: dict = {}

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO / "scripts"

_OK, _BAD, _PENDING = "✓", "✗", "○"  # check, cross, hollow circle (pending/deferred)
_SIZE_TOL_PCT = 0.10  # ±10% GGUF size tolerance
_DIAG_ROLES = ["planner", "coder", "chat", "fim", "embed"]


def configure(config: dict) -> None:
    global _cfg
    _cfg = config
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))


# --- discovery helpers ----------------------------------------------------------------------------

def gpu_arch():
    """{'CudaArch': int, 'Gen': str, 'MinCudaMajor': int} for GPU 0, or None. Delegates to the single
    source osenv.gpu_arch (the former per-module copies were consolidated here)."""
    import osenv
    return osenv.gpu_arch()


def _venv_python() -> Path:
    import osenv
    return osenv.venv_exe("venv-litellm", "python")


def _has_module(venv_py: Path, module: str) -> bool:
    import subprocess
    try:
        r = subprocess.run([str(venv_py), "-c", f"import {module}; print('ok')"],
                           capture_output=True, text=True, timeout=15)
        return r.stdout.strip() == "ok"
    except (OSError, subprocess.SubprocessError):
        return False


def _tool_load_errors() -> list:
    """Build the same ToolRegistry the loop builds and return its load errors [(name, kind, msg)].
    Honors agent.disabledTools; builds the registry in-process."""
    from tool_registry import ToolRegistry

    disabled_raw = _cfg.get("agent", {}).get("disabledTools", [])
    disabled = ({t.strip() for t in disabled_raw.split(",") if t.strip()}
                if isinstance(disabled_raw, str) else set(disabled_raw))
    return ToolRegistry.build(_cfg, disabled, quiet=True).errors


# --- setup(check) / doctor ------------------------------------------------------------------------

def health_check(config: dict, doctor: bool = False) -> str:
    """Shared pre-flight for `bob setup check` and `bob doctor`. doctor=True adds the runtime checks
    (endpoint reachable, GPU/VRAM, writable dirs, config parses)."""
    import osenv
    import requests
    from bob_core import _port

    lines: list = []

    def check(label: str, ok: bool, fix: str = "") -> None:
        sym = _OK if ok else _BAD
        line = f"  {sym}  {label}"
        if not ok and fix:
            line += f"  →  {fix}"
        lines.append(line)

    def pending(label: str, note: str) -> None:
        lines.append(f"  {_PENDING}  {label}  ({note})")

    title = "Bob doctor — full pre-flight" if doctor else "Bob agent setup check"
    lines += ["", title, "─" * 41]

    venv_py = _venv_python()
    venv_ok = venv_py.exists()
    check("venv-litellm exists", venv_ok, "bob setup")

    # Python packages
    if venv_ok:
        has_openai = _has_module(venv_py, "openai")
        has_requests = _has_module(venv_py, "requests")
        fix = "pip install openai" if not has_openai else ("pip install requests" if not has_requests else "")
        check("Python packages (openai, requests)", has_openai and has_requests, fix)
    else:
        check("Python packages (openai, requests)", False, "run bob setup first")

    # Config resolves the same way on every OS now: config/defaults.json + config/user.json via
    # bob_config.
    from bob_core import load_config
    resolved = False
    try:
        resolved = bool(load_config().get("persona"))
    except Exception:  # noqa: BLE001
        pass
    check("config resolves (config/defaults.json)", resolved,
          "check config/defaults.json is valid JSON")

    check("scripts/tools/ exists", (SCRIPTS / "tools").is_dir())

    # data/schedules.json — created empty if absent
    sched = REPO / "data" / "schedules.json"
    if not sched.exists():
        sched.parent.mkdir(parents=True, exist_ok=True)
        sched.write_text("[]\n", encoding="utf-8")
        lines.append("  →  data/schedules.json created (empty)")
    check("data/schedules.json exists", sched.exists())

    import shutil as _sh
    check("fabric on PATH", bool(_sh.which("fabric")), "bob fabric-setup")

    # SearXNG / n8n run as docker compose services. On a box with no docker they can't start at all,
    # so report the real reason (needs Docker + install hint) instead of a misleading "bob services
    # start" that would just fail. Docker present -> the normal reachability checks.
    searx_port = _port(config, "searxngPort")
    n8n_port = _port(config, "n8nPort")
    if not osenv.docker_present():
        pending("Docker (for SearXNG / n8n / langfuse)", f"not installed. {osenv.docker_install_hint()}")
        pending(f"SearXNG (:{searx_port})", "unavailable (needs Docker, not installed)")
        pending(f"n8n (:{n8n_port})", "unavailable (needs Docker, not installed)")
    else:
        check(f"SearXNG reachable (:{searx_port})", osenv.is_port_in_use(searx_port), "bob services start")
        n8n_ok = False
        try:
            n8n_ok = requests.get(f"http://localhost:{n8n_port}", timeout=8).status_code < 500
        except requests.RequestException:
            pass
        check(f"n8n reachable (:{n8n_port})", n8n_ok, "bob services start")

    litellm_port = _port(config, "litellmPort")
    check(f"LiteLLM proxy (:{litellm_port})", osenv.is_port_in_use(litellm_port), "bob litellm")

    # BobAgent scheduled task — the every-minute OS task (osenv scheduler quartet). Not-registered
    # is informational (scheduling is opt-in), so report state without failing the check.
    task = osenv.agent_task_status()
    if task.get("registered"):
        state = task.get("state") or "registered"
        nxt = f", next {task['next_run']}" if task.get("next_run") else ""
        check(f"BobAgent task registered ({state}{nxt})", True)
    else:
        pending("BobAgent task not registered", "optional — bob agent install to enable scheduling")

    # Agent model downloaded
    try:
        import bob_models
        roles = bob_models.profile_roles()
        agent = roles.get("agent")
        if agent:
            model_file = REPO / "models" / agent.get("gguf", "")
            check(f"Agent model ({agent.get('gguf', '')})", model_file.exists(), "bob fetch")
        else:
            check("Agent model in active profile", False, "add agent role to config/models.json")
    except Exception:
        check("Agent model (check failed)", False)

    # Tools load cleanly — reuse the loop's registry build (single source of discovery).
    if venv_ok:
        try:
            errs = _tool_load_errors()
            check("Agent tools load without error", not errs, "run: bob tools")
            for name, kind, msg in errs:
                lines.append(f"     load error [{name}/{kind}]: {msg}")
        except Exception as e:
            check("Agent tools load without error", False, f"loader failed: {e}")
    else:
        check("Agent tools load without error", False, "venv-litellm missing")

    check("config/litellm.yaml exists", (REPO / "config" / "litellm.yaml").exists(), "bob gen")

    if doctor:
        lines.append("  ── runtime ──")

        port = _port(config, "port")
        api_ok = False
        try:
            api_ok = bool(requests.get(f"http://localhost:{port}/v1/models", timeout=3).json().get("data"))
        except (requests.RequestException, ValueError):
            pass
        check(f"Inference endpoint reachable (http://localhost:{port}/v1)", api_ok, "bob serve")

        from models import gpu_vram_gb
        vram = gpu_vram_gb()
        if vram:
            check(f"GPU VRAM detected (~{vram} GB)", True)
        else:
            check("No GPU -> CPU backend (CPU tier)", True, "nvidia-smi absent or no NVIDIA GPU")

        for name, path in (("data", osenv.data_dir()), ("logs", osenv.cache_dir())):
            writable = False
            try:
                path.mkdir(parents=True, exist_ok=True)
                probe = path / f".write-test.{_pid()}"
                probe.write_text("x", encoding="utf-8")
                probe.unlink(missing_ok=True)
                writable = True
            except OSError:
                pass
            check(f"{name}/ writable", writable, f"check permissions on {path}")

        # Reproducibility (versions.lock) — compare the lock to the installed state via the Python
        # reader (bob.versions.check_reproducibility). A missing lock is a pending (it is generated), not
        # a failure; any drift row fails with a fix hint.
        lines.append("  ── reproducibility ──")
        try:
            from bob.versions import check_reproducibility, load_lock
            lock = load_lock()
            drift = check_reproducibility(lock=lock)
            if not drift:
                n_sub = len(lock.get("submodules") or {})
                n_mod = len(lock.get("models") or {})
                check(f"versions.lock reproducible (release {lock.get('release')}, "
                      f"{n_sub} submodules, {n_mod} models)", True)
            else:
                for item in drift:
                    fix = ("git submodule update --init, or bob lock if intentional"
                           if item["kind"] == "submodule" else "re-fetch (bob fetch), or bob lock if the pin moved")
                    check(f"{item['kind']} {item['name']}: locked {item['expected'][:12]} "
                          f"!= actual {item['actual'][:12]}", False, fix)
        except RuntimeError:
            pending("versions.lock reproducibility", "versions.lock missing — run: bob lock")
        except Exception as e:  # reader import/parse failure must not crash doctor
            pending("versions.lock reproducibility", f"check unavailable ({e})")

    lines.append("")
    return "\n".join(lines)


def _pid() -> int:
    import os
    return os.getpid()


# --- version --------------------------------------------------------------------------------------

def version_info(config: dict) -> str:
    """Bob release (VERSION + versions.lock release) + binary versions + submodule commits. Port of the
    `version` case. Binary paths via the osenv seam (.exe only on Windows)."""
    import subprocess

    import osenv

    lines = []
    version_file = REPO / "VERSION"
    bob_ver = version_file.read_text(encoding="utf-8").strip() if version_file.exists() else "0.0.0"
    lines.append(f"Bob {bob_ver}")

    lock = REPO / "versions.lock"
    if lock.exists():
        try:
            import json
            rel = json.loads(lock.read_text(encoding="utf-8")).get("release")
            if rel:
                lines.append(f"  versions.lock release: {rel}")
        except (OSError, ValueError):
            pass

    def _bin_version(base: str) -> str:
        exe = osenv.bin_exe(base)
        if not exe.exists():
            return "(not built)"
        try:
            r = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=15)
            out = (r.stdout or r.stderr).strip().splitlines()
            return out[0].strip() if out else "(unknown)"
        except (OSError, subprocess.SubprocessError):
            return "(unknown)"

    def _commit(subdir: str) -> str:
        try:
            r = subprocess.run(["git", "-C", str(REPO / "external" / subdir), "rev-parse", "--short", "HEAD"],
                               capture_output=True, text=True, timeout=10)
            return r.stdout.strip() if r.returncode == 0 else "?"
        except (OSError, subprocess.SubprocessError):
            return "?"

    lines.append(f"llama-swap:   {_bin_version('llama-swap')}  ({_commit('llama-swap')})")
    lines.append(f"llama-server: {_bin_version('llama-server')}  ({_commit('llama.cpp')})")
    return "\n".join(lines)


# --- diagnose (registry + light discovery + deep OS discovery) ------------------------------------

def _cuda_installed() -> list:
    """Installed CUDA toolkits for display (names, versioned where known): versioned <prefix><maj.min>
    dirs under Base + canonical/fixed roots with their on-disk version."""
    import osenv
    c = osenv.resolve_cuda_root_candidates(0)
    import re
    out = []
    base = Path(c["Base"])
    if base.exists():
        pat = re.compile(rf"^{re.escape(c['DirPrefix'])}(\d+)\.(\d+)$")
        try:
            out += sorted(d.name for d in base.iterdir() if d.is_dir() and pat.match(d.name))
        except OSError:
            pass
    for fx in c.get("Fixed", []):
        if fx and Path(fx).exists():
            v = osenv.cuda_toolkit_version(fx)
            out.append(f"{Path(fx).name}" + (f" ({v[0]}.{v[1]})" if v else ""))
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def diagnose(config: dict) -> str:
    """System + model readiness — the full report: GPU arch/VRAM,
    system RAM, active-profile fit, endpoint, Linux package manager, CUDA toolkit resolution, mlock
    privilege, NUMA topology, model files on disk, manifest coverage. Deep discovery reads the
    build-time osenv seams."""
    import bob_models
    import osenv
    from bob_core import _port

    sys.path.insert(0, str(REPO / "scripts" / "tools"))
    from models import gpu_vram_gb, suggested_profile

    lines = ["", "System check", "-" * 52]
    issues = 0

    def row(label: str, value: str) -> None:
        lines.append(f"  {label:<10}  {value}")

    # GPU + VRAM
    gpu = gpu_arch()
    vram = gpu_vram_gb()
    if gpu:
        fa = "  (WARNING: flash-attn needs sm_75+; disable flashAttn)" if gpu["CudaArch"] < 75 else ""
        row("GPU", f"{gpu['Gen']}  (sm_{gpu['CudaArch']}){fa}")
        row("VRAM", f"{vram} GB")
    else:
        row("GPU", "not detected  (nvidia-smi not found or no NVIDIA GPU)")
        row("VRAM", "unknown")

    # System RAM (informational)
    ram = osenv.system_ram_gb()
    if ram:
        free = f"  ({ram['FreeGB']} GB free)" if ram.get("FreeGB") is not None else ""
        row("RAM", f"{ram['TotalGB']} GB total{free}")
    else:
        row("RAM", "unknown")

    mcfg = bob_models.load_models_config()
    defaults = mcfg.get("defaults", {})
    active = bob_models.resolve_profile_name(config=mcfg)
    sug = suggested_profile(vram, mcfg)
    if sug and sug == active:
        row("Profile", f"{active}  (good fit for {vram} GB VRAM)")
    elif sug and sug != active:
        row("Profile", f"{active}  (suggested '{sug}' for this VRAM — switch: bob profile auto)")
    else:
        row("Profile", active)

    port = _port(config, "port")
    row("Endpoint", f"http://localhost:{port}/v1  ({'up' if osenv.is_port_in_use(port) else 'not running'})")

    # Linux package manager (Windows uses winget; only the Linux mapping can be "missing")
    if osenv.os_name() != "windows":
        mgr = osenv.linux_package_manager()
        fam = osenv.linux_os_family() or "unknown"
        if mgr:
            row("Package", f"{mgr}  (family: {fam})  — supported")
        else:
            row("Package", "no supported manager (apt/dnf/pacman/zypper) — install the toolchain manually")
            issues += 1

    # CUDA toolkit resolution (deep — best_cuda_root ranks installed toolkits vs the arch floor)
    installed = _cuda_installed()
    if gpu:
        best = osenv.best_cuda_root(gpu["CudaArch"])
        if best:
            row("CUDA", f"{Path(best).name}  ok")
        else:
            need = "12.8 (required for Blackwell)" if gpu["CudaArch"] >= 120 else "12.x"
            found = f"found: {', '.join(installed)}" if installed else "none installed"
            row("CUDA", f"needs {need}  ({found})  — setup will install")
            issues += 1
    else:
        label = (sorted(installed)[-1] + "  (no GPU detected)" if installed
                 else "not installed  (no GPU detected — skipping)")
        row("CUDA", label)

    # mlock privilege
    st = osenv.mlock_status()
    mlock_enabled = defaults.get("mlockBig") is True
    if mlock_enabled and not st["granted"]:
        row("mlock", f"mlockBig=true but NOT granted — run: bob mlock --grant  ({st['detail']})")
        issues += 1
    elif mlock_enabled and st["granted"]:
        row("mlock", f"granted (--mlock active)  ({st['detail']})")
    else:
        row("mlock", f"not enabled  ({st['detail']})")

    # NUMA topology vs config
    nodes = osenv.numa_node_count()
    numa_cfg = defaults.get("numa") or ""
    if numa_cfg:
        if nodes <= 1:
            row("NUMA", f"config '--numa {numa_cfg}' but the OS reports {nodes} node — flag is a no-op; "
                       "set numa='' in user config")
            issues += 1
        else:
            row("NUMA", f"{nodes} nodes  — '--numa {numa_cfg}' active")
    else:
        row("NUMA", (f"{nodes} nodes detected — consider numa='isolate' for CPU-offload gains"
                     if nodes > 1 else f"{nodes} NUMA node  (disabled, correct for this topology)"))

    # Models present on disk (size-validated) + manifest coverage
    roles = bob_models.profile_roles(active, mcfg)
    mdir = REPO / "models"
    present = total = 0
    bad = []
    for role in _DIAG_ROLES:
        spec = roles.get(role)
        if not spec:
            continue
        total += 1
        f = mdir / spec.get("gguf", "")
        if not f.exists():
            continue
        present += 1
        if (mdir / f"{spec.get('gguf', '')}.part").exists():
            bad.append(f"{spec['gguf']}  (partial download — delete and re-run: bob fetch)")
            continue
        exp = float(spec.get("sizeGB", 0) or 0)
        act = f.stat().st_size / (1024 ** 3)
        if exp and (act < exp * (1 - _SIZE_TOL_PCT) or act > exp * (1 + _SIZE_TOL_PCT)):
            bad.append(f"{spec['gguf']}  (size {round(act, 1)} GB, expected ~{exp} GB — re-download: bob fetch)")

    if bad:
        row("Models", f"{present} / {total} present  — {len(bad)} corrupt")
        for b in bad:
            lines.append(f"             {b}")
        issues += 1
    elif present:
        row("Models", f"{present} / {total} present  (profile: {active})")
    else:
        row("Models", f"none downloaded yet  (profile: {active})  — setup will fetch")

    manifest_file = mdir / "manifest.json"
    manifest = {}
    if manifest_file.exists():
        try:
            import json
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {}
    m_covered = m_total = 0
    for role in _DIAG_ROLES:
        spec = roles.get(role)
        if not spec or not (mdir / spec.get("gguf", "")).exists():
            continue
        m_total += 1
        if manifest.get(spec.get("gguf", "")):
            m_covered += 1
    if m_total:
        row("Manifest", f"{m_covered} / {m_total} SHA256 recorded  (bob fetch to populate)")

    lines.append("-" * 52)
    lines.append(f"  {issues} issue(s) noted above. Setup will attempt to resolve them."
                 if issues else "  All checks passed.")
    lines.append("")
    return "\n".join(lines)


# --- agent tool adapters --------------------------------------------------------------------------

def _doctor() -> str:
    return health_check(_cfg, doctor=True)


def _diagnose() -> str:
    return diagnose(_cfg)


def _version_info() -> str:
    return version_info(_cfg)


def test() -> str:
    return version_info(_cfg)


TOOL_DEFS = [
    {"type": "function", "function": {
        "name": "doctor",
        "description": ("Full pre-flight health check: dependency/registration checks (venv, packages, "
                        "config, ports, agent model, tool loading) plus runtime checks (endpoint, GPU/VRAM, "
                        "writable dirs). Read-only. Use to diagnose why Bob isn't working."),
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "diagnose",
        "description": ("System + model readiness: GPU generation/VRAM, active profile fit, endpoint state, "
                        "which model files are on disk (size-validated), and manifest coverage. Read-only."),
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "version_info",
        "description": ("Report the Bob release plus llama-swap/llama-server binary versions and their "
                        "submodule commits. Read-only."),
        "parameters": {"type": "object", "properties": {}}}},
]

DISPATCH = {"doctor": _doctor, "diagnose": _diagnose, "version_info": _version_info}
