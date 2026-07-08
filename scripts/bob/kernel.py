"""Tier-1 cold-start kernel — the fresh-machine bring-up, in Python.

"The one honest exception": an agent can't boot its own brain, so a non-conversational path survives.
It runs under the *system* python3 with `scripts/` on sys.path, BEFORE the venvs exist —
so it IMPORTS the same capability functions the agent and `bob --run` reach (provision.fetch_models,
generate.gen_all, build.build_llama/..., stack.stack_up, health.diagnose) rather than re-implementing or
subprocessing them. Only prereqs, the venv creation, and the *first* build are kernel-exclusive.

  python3 -m bob.kernel prereqs [--cpu]     # Tier 0 — toolchain + a venv-compatible Python
  python3 -m bob.kernel setup [flags]        # Tier 1 — the 12-step fresh-machine orchestrator
  python3 -m bob.kernel bootstrap [flags]    #          submodules -> build -> venvs -> gen -> fetch
  python3 -m bob.kernel venv <name...>       #          create tools/venv-<name> (litellm|aider|eval|webui)
  python3 -m bob.kernel build-swap           #          build the llama-swap proxy (Go)

Flags: --skip-models --skip-build --skip-voice --launch --profile <p> --with-webui --cpu
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import osenv  # noqa: E402

REPO = osenv.REPO


def _have(name: str) -> bool:
    import shutil
    return shutil.which(name) is not None


def _tools_on_path() -> None:
    tools = str(_SCRIPTS / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)


def _load_config() -> dict:
    from bob_core import load_config
    return load_config()


def _step(current: int, total: int, name: str, hint: str = "") -> None:
    print(f"\n=== Step {current}/{total}: {name} ===", file=sys.stderr)
    if hint:
        print(f"  ({hint})", file=sys.stderr)


# --- bootstrap -----------------------------------------------------------------------------------

def bootstrap(skip_models: bool = False, skip_build: bool = False, profile: str = None,
              with_webui: bool = False, cpu: bool = False) -> None:
    """Submodules -> build engine + proxy + fabric -> Python venvs -> gen configs -> fetch models.
    Re-runnable; heavy steps skippable. Imports the capability fns directly."""
    _tools_on_path()
    import bob_models
    import build
    import generate
    import models as models_mod
    import provision

    config = _load_config()

    # Profile selection. Explicit wins; otherwise suggest one from detected VRAM (never forces).
    if profile:
        print(f"\n=== Select profile '{profile}' ===", file=sys.stderr)
        bob_models.set_active_profile(profile)
    else:
        vram = osenv.gpu_vram_gb()
        sug = models_mod.suggested_profile(vram)
        active = bob_models.load_models_config().get("activeProfile")
        if sug and sug != active:
            print(f"\n=== VRAM check — auto-selecting profile ===\nDetected ~{vram} GB VRAM -> switching "
                  f"profile '{active}' -> '{sug}'", file=sys.stderr)
            bob_models.set_active_profile(sug)
        elif sug:
            print(f"VRAM ~{vram} GB -> profile '{active}' (good fit).", file=sys.stderr)

    gpu = osenv.gpu_arch()
    cuda_root = osenv.best_cuda_root(gpu["CudaArch"] if gpu else 120)

    print("\n=== Prereqs ===", file=sys.stderr)
    if not _have("git"):
        raise RuntimeError("git missing")
    print("git    : ok", file=sys.stderr)
    print("cmake  : " + ("ok" if _have("cmake") else "not on PATH (auto-install handles this)"), file=sys.stderr)
    if gpu:
        print(f"GPU    : {gpu['Gen']} (sm_{gpu['CudaArch']})", file=sys.stderr)
    print("CUDA   : " + (f"ok — {cuda_root}" if cuda_root else "MISSING — install CUDA 12.x before building"),
          file=sys.stderr)
    print("go     : " + ("ok" if _have("go") else "missing — will need a llama-swap release binary instead"),
          file=sys.stderr)
    py = osenv.bob_venv_python()
    print("python : " + (py if py else "MISSING — install Python 3.12 (or ensure uv is available)"),
          file=sys.stderr)

    # Submodules.
    print("\n=== Submodules ===", file=sys.stderr)
    if subprocess.run(["git", "-C", str(REPO), "submodule", "update", "--init", "--recursive"]).returncode != 0:
        raise RuntimeError("submodule init failed")

    # Build engine + proxy + fabric.
    build.configure(config)
    if not skip_build:
        if cuda_root and not cpu:
            label = f"{gpu['Gen']} sm_{gpu['CudaArch']}" if gpu else "sm_120 (default)"
            print(f"\n=== Build llama.cpp ({label}) ===", file=sys.stderr)
            build.build_llama(arch=(gpu["CudaArch"] if gpu else 0))
        else:
            # CPU tier: either forced (--cpu) or no CUDA toolkit detected — so a GPU-less box (or a user
            # who asked for it) still gets a working, if slower, llama-server.
            why = "--cpu requested" if cpu else "no CUDA toolkit found"
            print(f"\n=== Build llama.cpp (CPU-only — {why}) ===", file=sys.stderr)
            build.build_llama(cpu=True)

        print("\n=== Build llama-swap ===", file=sys.stderr)
        if _have("go"):
            build.build_llama_swap()
        else:
            print("Skipping llama-swap build — Go missing. Download the llama-swap release binary into bin/.",
                  file=sys.stderr)

        print("\n=== Build fabric ===", file=sys.stderr)
        if _have("go"):
            try:
                build.setup_fabric()
            except RuntimeError as e:
                print(f"  fabric build failed (optional): {e}", file=sys.stderr)
        else:
            print("Skipping fabric build — Go missing.", file=sys.stderr)
    else:
        print("Skipping builds (--skip-build)", file=sys.stderr)

    # Python tools: ISOLATED venvs (open-webui & aider have conflicting dep pins).
    print("\n=== Python venvs (3.12+) + tools ===", file=sys.stderr)
    if py:
        venvs = [("venv-aider", "aider-requirements"), ("venv-litellm", "litellm-requirements")]
        if with_webui:
            venvs.append(("venv-webui", "webui-requirements"))
        else:
            print("  skipping venv-webui (open-webui is opt-in — re-run with --with-webui to install)",
                  file=sys.stderr)
        for vname, base in venvs:
            osenv.new_bob_venv(vname, base, python=py)
    else:
        print("Skipping venvs — Python 3.12+ not found.", file=sys.stderr)

    # Runtime config (generated from the model registry; runs even with --skip-models).
    print("\n=== Generate llama-swap config ===", file=sys.stderr)
    generate.configure(config)
    generate.gen_all()

    # Models.
    if not skip_models:
        print("\n=== Fetch models (multi-GB) ===", file=sys.stderr)
        provision.configure(config)
        print(provision.fetch_models(), file=sys.stderr)
    else:
        print("Skipping model downloads (--skip-models). Run `bob fetch` later.", file=sys.stderr)

    print("\n=== Done ===\nNext: bob up   (endpoint :8080 + LiteLLM proxy :8081)", file=sys.stderr)


# --- post-build wiring (client configs / CLI install / onboarding) ------------------------------

def _wire(target: Path, link: Path) -> None:
    """Symlink `link` -> `target` (edits in the repo propagate live); fall back to a copy where symlinks
    aren't permitted. Leaves an existing link as-is."""
    import shutil
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.exists() or link.is_symlink():
        print(f"exists, left as-is: {link}  (delete it to re-wire)", file=sys.stderr)
        return
    try:
        link.symlink_to(target)
        print(f"linked  {link}  ->  {target}", file=sys.stderr)
    except (OSError, NotImplementedError):
        shutil.copy(target, link)
        print(f"copied  {target}  ->  {link}   (no symlink priv; re-run after editing the repo config)",
              file=sys.stderr)


def setup_clients() -> None:
    """Point VS Code Continue + aider at the repo's config files (symlink, copy fallback). Generates the
    Continue config first so the symlink target exists."""
    _tools_on_path()
    import generate
    generate.configure(_load_config())
    generate.gen_continue()
    home = Path.home()
    _wire(REPO / "config" / "continue" / "config.yaml", home / ".continue" / "config.yaml")
    _wire(REPO / "config" / "aider" / ".aider.conf.yml", home / ".aider.conf.yml")

    aider = osenv.venv_exe("venv-aider", "aider")
    if aider.exists():
        print("  [OK] aider installed at tools/venv-aider/", file=sys.stderr)
    else:
        print("  [!] aider not found — run setup first.", file=sys.stderr)


def install_cli() -> None:
    """Install the `bob` command on PATH. POSIX: symlink the repo-root ./bob shim (+ fabric) into
    ~/.local/bin. Windows: a bob.cmd shim in scoop\\shims that points at `python -m bob`."""
    if osenv.os_name() == "windows":  # pragma: no cover — Windows path
        _install_cli_windows()
        return
    import os
    bindir = Path.home() / ".local" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    bob_link = bindir / "bob"
    if bob_link.exists() or bob_link.is_symlink():
        bob_link.unlink()
    bob_link.symlink_to(REPO / "bob")
    print(f"'bob' installed -> {bob_link}", file=sys.stderr)

    fabric_exe = osenv.bin_exe("fabric")
    if fabric_exe.exists():
        fabric_link = bindir / "fabric"
        if fabric_link.exists() or fabric_link.is_symlink():
            fabric_link.unlink()
        fabric_link.symlink_to(fabric_exe)
        print(f"'fabric' installed -> {fabric_link}", file=sys.stderr)
    else:
        print("'fabric' shim skipped — not built yet. Run: bob fabric-setup", file=sys.stderr)

    if str(bindir) not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"NOTE: {bindir} is not on PATH. fish: fish_add_path {bindir} | bash/zsh: add it to your rc, "
              "then open a new shell.", file=sys.stderr)
    print("Open a NEW terminal (with ~/.local/bin on PATH), then try:  bob help", file=sys.stderr)


def _install_cli_windows() -> None:  # pragma: no cover — Windows path
    """Windows: a bob.cmd shim -> `python -m bob`. Drops the shim into
    scoop\\shims (or ~/scoop/shims) so `bob` resolves in any shell."""
    import shutil
    shim_dir = None
    sc = shutil.which("scoop")
    if sc:
        shim_dir = Path(sc).parent
    if not shim_dir or not shim_dir.exists():
        shim_dir = Path.home() / "scoop" / "shims"
    if not shim_dir.exists():
        raise RuntimeError(f"No scoop\\shims dir at {shim_dir}. Add {REPO}\\bob to PATH manually instead.")
    py = osenv.venv_exe("venv-litellm", "python")
    py = str(py) if Path(py).exists() else "python"
    cmd_path = shim_dir / "bob.cmd"
    cmd_path.write_text(
        f'@echo off\r\nset "PYTHONPATH={REPO / "scripts"}"\r\n"{py}" -m bob %*\r\n', encoding="ascii")
    print(f"'bob' installed -> {cmd_path}", file=sys.stderr)
    fabric_exe = osenv.bin_exe("fabric")
    if fabric_exe.exists():
        (shim_dir / "fabric.cmd").write_text(f'@echo off\r\n"{fabric_exe}" %*\r\n', encoding="ascii")
        print(f"'fabric' installed -> {shim_dir / 'fabric.cmd'}", file=sys.stderr)
    print("Open a NEW terminal, then try:  bob help", file=sys.stderr)


def _has_profile_rows() -> bool:
    """True if the memory DB already holds a durable identity (type='profile') row. Stdlib sqlite3 only
    (no venv deps) — this runs on the kernel path. A missing DB/table means no profile yet."""
    import sqlite3
    db = osenv.data_dir() / "bob.db"
    if not db.exists():
        return False
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return con.execute("SELECT count(*) FROM memories WHERE type='profile'").fetchone()[0] > 0
        finally:
            con.close()
    except sqlite3.Error:
        return False


def _needs_onboard() -> bool:
    """Onboard when we've never recorded the user OR the durable profile was never seeded. The real
    signal is a profile row in memory — the config `bob` marker alone is NOT enough: onboard() writes
    that marker even if the profile save failed (venv not built yet, or the subprocess errored), which
    left machines marked-but-unknown ("Bob doesn't know me"). Keying on profile presence self-heals: a
    failed seed re-triggers onboarding next run."""
    import json
    user_cfg = REPO / "config" / "user.json"
    marked = False
    if user_cfg.exists():
        try:
            marked = "bob" in json.loads(user_cfg.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            marked = False
    return (not marked) or (not _has_profile_rows())


def _onboard_declined() -> bool:
    """True if the user has already declined the shell's onboarding offer (config bob.onboardDeclined),
    so a bare `bob` never re-nags. Setup's own onboarding is unaffected."""
    import json
    user_cfg = REPO / "config" / "user.json"
    if not user_cfg.exists():
        return False
    try:
        cfg = json.loads(user_cfg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(isinstance(cfg, dict) and cfg.get("bob", {}).get("onboardDeclined"))


def _record_onboard_declined() -> None:
    import json
    user_cfg = REPO / "config" / "user.json"
    try:
        cfg = json.loads(user_cfg.read_text(encoding="utf-8")) if user_cfg.exists() else {}
        if not isinstance(cfg, dict):
            cfg = {}
    except (OSError, ValueError):
        cfg = {}
    cfg.setdefault("bob", {})["onboardDeclined"] = True
    user_cfg.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def offer_onboard() -> None:
    """Onboarding reach: on a fresh INTERACTIVE `bob` (the shell front door, not just `bob setup`),
    offer to seed a profile so Bob knows the user from the very first chat. No-op when a profile
    already exists, on a non-TTY, or after the user declined once (recorded so we never nag again).
    A 'yes' runs the same onboard() that setup uses; the seeded profile is injected on the next chat."""
    if not sys.stdin.isatty() or not _needs_onboard() or _onboard_declined():
        return
    print("Bob: I don't know you yet. Want to set up your profile now? [Y/n]")
    try:
        ans = input("> ").strip().lower()
    except EOFError:
        return
    if ans in ("", "y", "yes"):
        onboard()
    else:
        _record_onboard_declined()
        print("Bob: no problem. Run `bob memory init-profile` anytime to set it up.", file=sys.stderr)


def onboard() -> None:
    """First-run onboarding: name, work context, optional DeepSeek key -> SQLite profile + config/user.json.
    Interactive — SKIPS cleanly on a non-TTY (CI/piped) so the kernel never hangs."""
    import json
    if not sys.stdin.isatty():
        print("Bob: onboarding skipped (non-interactive). Run `bob memory init-profile` later.",
              file=sys.stderr)
        return

    def bob(msg: str) -> None:
        print(f"Bob: {msg}")

    # A re-onboard (marked but the profile never seeded) shouldn't re-nag for a key already on file.
    user_cfg = REPO / "config" / "user.json"
    try:
        _existing = json.loads(user_cfg.read_text(encoding="utf-8")) if user_cfg.exists() else {}
    except (OSError, ValueError):
        _existing = {}
    has_key = bool(isinstance(_existing, dict)
                   and _existing.get("peers", {}).get("deepseek", {}).get("apiKey"))

    print()
    bob("Hi. Let me set up your profile.")
    print()
    bob("What's your name?")
    user_name = (input("> ").strip() or "User")
    bob("What kind of work do you do most? (e.g. game dev, web, writing)")
    user_work = (input("> ").strip() or "software development")
    if has_key:
        api_key = ""   # already configured — don't ask again on a re-onboard
    else:
        bob("Got a DeepSeek API key? Enables cloud-quality answers when you want them. (Enter to skip)")
        api_key = input("> ").strip()

    # Save the profile to SQLite. Memory needs venv-only deps (sqlite-utils + requests), but onboarding
    # runs under the *system* python (the kernel) — so shell out to the venv-litellm interpreter, which
    # has them (mirrors how the loop's memory tool runs). Best-effort: a failure just skips persistence.
    venv_py = osenv.venv_exe("venv-litellm", "python")
    if Path(venv_py).exists():
        db = osenv.data_dir() / "bob.db"
        rc = subprocess.run([str(venv_py), str(_SCRIPTS / "bob_memory.py"), "--db", str(db),
                             "init-profile", "--name", user_name, "--work", user_work]).returncode
        if rc != 0:
            print("  (couldn't save the profile to memory — run `bob memory init-profile` later.)",
                  file=sys.stderr)
    else:
        print("  (venv-litellm not built yet — run `bob memory init-profile` after setup.)", file=sys.stderr)

    try:
        cfg = json.loads(user_cfg.read_text(encoding="utf-8")) if user_cfg.exists() else {}
        if not isinstance(cfg, dict):
            cfg = {}
    except (OSError, ValueError):
        cfg = {}
    cfg.setdefault("bob", {})
    key_added = False
    if api_key:
        cfg.setdefault("peers", {}).setdefault("deepseek", {})
        if cfg["peers"]["deepseek"].get("apiKey") != api_key:
            cfg["peers"]["deepseek"]["apiKey"] = api_key
            key_added = True
    user_cfg.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")

    if key_added:
        print("Regenerating config with API key...", file=sys.stderr)
        try:
            _tools_on_path()
            import generate
            generate.configure(_load_config())
            generate.gen_all()
        except Exception:  # noqa: BLE001 — best-effort
            pass

    print()
    bob(f"Ready, {user_name}. Type 'bob chat' to start.")
    print()


# --- docker services (best-effort, optional) -----------------------------------------------------

def _docker_ready() -> bool:
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def setup_docker() -> None:
    """Provision + start the optional compose services (Langfuse/SearXNG/n8n): wait for the daemon, write
    the compose .env + SearXNG settings + data dirs, then `docker compose pull && up -d`. Best-effort —
    the kernel only calls this when docker is already present."""
    from bob_core import _port
    _tools_on_path()
    import bob_models

    if not _docker_ready():
        if osenv.os_name() != "windows" and _have("systemctl"):
            print("  Starting docker daemon (systemctl)...", file=sys.stderr)
            subprocess.run(["sudo", "systemctl", "start", "docker"], capture_output=True)
        for _ in range(18):  # up to ~90s
            if _docker_ready():
                break
            time.sleep(5)
    if not _docker_ready():
        print("  Docker daemon did not respond — skipping compose services. Start docker and re-run.",
              file=sys.stderr)
        return

    config = _load_config()
    d = bob_models.load_models_config().get("defaults", {})
    lf = d.get("langfusePort") or _port(config, "langfusePort")
    sx = d.get("searxngPort") or _port(config, "searxngPort")
    n8n = d.get("n8nPort") or _port(config, "n8nPort")
    tz = d.get("n8nTimezone") or "UTC"

    env_file = REPO / "tools" / "compose" / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(f"REPO_PATH={REPO}\nLANGFUSE_PORT={lf}\nSEARXNG_PORT={sx}\nN8N_PORT={n8n}\n"
                        f"N8N_TIMEZONE={tz}\n", encoding="utf-8")
    print(f"  Ports: Langfuse={lf}  SearXNG={sx}  n8n={n8n}  Timezone={tz}", file=sys.stderr)

    for sub in ("langfuse-data", "n8n-data"):
        (REPO / "tools" / sub).mkdir(parents=True, exist_ok=True)

    sx_cfg = REPO / "config" / "searxng" / "settings.yml"
    if not sx_cfg.exists():
        sx_cfg.parent.mkdir(parents=True, exist_ok=True)
        sx_cfg.write_text(
            'use_default_settings: true\nserver:\n  secret_key: "bob-searxng"\n'
            '  bind_address: "0.0.0.0:8080"\nsearch:\n  safe_search: 0\n  default_lang: "en"\n'
            '  formats:\n    - html\n    - json\n', encoding="utf-8")

    compose = REPO / "tools" / "compose" / "docker-compose.yml"
    print("Pulling images (first run may take a few minutes)...", file=sys.stderr)
    subprocess.run(["docker", "compose", "-f", str(compose), "pull"])
    print("Starting services...", file=sys.stderr)
    subprocess.run(["docker", "compose", "-f", str(compose), "up", "-d"])
    print(f"\nServices running:\n  Langfuse:  http://localhost:{lf}\n  SearXNG:   http://localhost:{sx}\n"
          f"  n8n:       http://localhost:{n8n}\nManage: bob services start|stop|status|logs", file=sys.stderr)


# --- setup (the 12-step orchestrator) ------------------------------------------------------------

def setup(skip_models: bool = False, skip_build: bool = False, skip_voice: bool = False,
          launch: bool = False, profile: str = None, with_webui: bool = False, cpu: bool = False) -> int:
    """The fresh-machine orchestrator. Idempotent; safe to re-run. Prerequisites must be installed first
    via `python3 -m bob.kernel prereqs`."""
    _tools_on_path()
    import build
    import health
    import provision

    start = time.monotonic()
    is_win = osenv.os_name() == "windows"
    total = 12
    config = _load_config()

    _step(1, total, "System check")
    try:
        print(health.diagnose(config), file=sys.stderr)
    except Exception as e:  # noqa: BLE001 — diagnose is informational
        print(f"diagnose: {e}", file=sys.stderr)

    _step(2, total, "Core tooling")
    if not _have("git"):
        raise RuntimeError("git not found. Install Git, then re-run setup.")
    if is_win and not _have("scoop"):  # pragma: no cover
        raise RuntimeError("scoop not found. Install it (irm get.scoop.sh | iex), then re-run.")
    print("git ok", file=sys.stderr)

    _step(3, total, "Prerequisite check")
    missing = []
    if not _have("node"):
        missing.append("Node.js")
    if not _have("go"):
        missing.append("Go")
    if is_win:  # pragma: no cover
        if not _have("uvx"):
            missing.append("uv")
    else:
        if not _have("python3"):
            missing.append("Python 3.12")
    if missing:
        prereq = "install_prereqs.bat" if is_win else "./install_prereqs.sh"
        print(f"\nMissing prerequisites: {', '.join(missing)}\nRun {prereq} first, then re-run setup.",
              file=sys.stderr)
        return 1
    print("  Prerequisites ok.", file=sys.stderr)

    _step(4, total, "C++ toolchain (compiler required for llama.cpp build)")
    server_exe = osenv.bin_exe("llama-server")
    if not skip_build and not server_exe.exists():
        if is_win:  # pragma: no cover
            print("  (Windows: ensure VS2022 'Desktop development with C++' is installed)", file=sys.stderr)
        elif _have("g++") or _have("gcc") or _have("cc"):
            print("  gcc/g++ ok", file=sys.stderr)
        else:
            raise RuntimeError("No C++ compiler found — run ./install_prereqs.sh, then re-run. "
                               "(Pass --skip-build if you have a prebuilt bin/llama-server.)")
    else:
        print("  C++ toolchain check skipped (build not needed)", file=sys.stderr)

    _step(5, total, "cmake 3.x (cmake 4.x excluded by llama.cpp version range)")
    if not is_win:
        try:
            print(f"  cmake 3.x ready: {osenv.linux_cmake3(REPO)}", file=sys.stderr)
        except (RuntimeError, OSError) as e:
            print(f"  cmake 3.x provisioning failed: {e}", file=sys.stderr)
    else:  # pragma: no cover
        print("  (Windows: winget/VS-bundled cmake handled by install_prereqs)", file=sys.stderr)

    _step(6, total, "Bootstrap: submodules -> build -> venvs+tools -> models", "first build takes 5-15 min")
    bootstrap(skip_models=skip_models, skip_build=skip_build, profile=profile, with_webui=with_webui, cpu=cpu)

    _step(7, total, "Wire clients (Continue + aider)")
    try:
        setup_clients()
    except Exception as e:  # noqa: BLE001
        print(f"  client wiring failed (non-fatal): {e}", file=sys.stderr)

    _step(8, total, "fabric (shell AI patterns)")
    build.configure(config)
    if _have("go"):
        try:
            build.setup_fabric()
        except RuntimeError as e:
            print(f"  fabric setup failed (non-fatal): {e}", file=sys.stderr)
    else:
        print("  Go missing — skipping fabric.", file=sys.stderr)

    _step(9, total, "Install 'bob' CLI command")
    try:
        install_cli()
    except Exception as e:  # noqa: BLE001
        print(f"  CLI install failed (non-fatal): {e}", file=sys.stderr)

    _step(10, total, "Voice + Vision setup", "builds whisper.cpp, downloads STT model + TTS voice")
    if skip_voice:
        print("  Skipped (--skip-voice).", file=sys.stderr)
    else:
        provision.configure(config)
        try:
            print(provision.setup_voice(), file=sys.stderr)
        except Exception as e:  # noqa: BLE001 — voice is optional Phase-2; never sink a good build
            print(f"  voice setup failed (non-fatal): {e}", file=sys.stderr)

    _step(11, total, "Memory lock (mlock)")
    st = osenv.mlock_status()
    print("  mlock: " + ("granted" if st["granted"] else "not granted") + f" — {st['detail']}", file=sys.stderr)
    if not is_win and not st["granted"]:
        print("  Linux: raise 'ulimit -l' (memlock) if you enable mlockBig. See: bob mlock --grant",
              file=sys.stderr)

    _step(12, total, "Docker services (Langfuse + SearXNG + n8n)")
    if _have("docker"):
        try:
            setup_docker()
        except Exception as e:  # noqa: BLE001 — docker is optional
            print(f"  Docker services skipped (non-fatal): {e}", file=sys.stderr)
    else:
        print("  Docker not installed — skipping. Install docker + re-run to add the compose services.",
              file=sys.stderr)

    mins = int((time.monotonic() - start) // 60)
    secs = int((time.monotonic() - start) % 60)
    print(f"\nSetup complete in {mins}m{secs}s.", file=sys.stderr)
    print("Open a new terminal, then:  bob up   (or  bob help  for all commands)", file=sys.stderr)

    if _needs_onboard():
        onboard()

    if launch:
        _tools_on_path()
        import stack
        stack.configure(config)
        print(stack.stack_up(config, open_browser=True), file=sys.stderr)
    return 0


# --- single-venv + swap helpers (the CI granular provisioning steps) -----------------------------

_VENV_REQ = {
    "litellm": ("venv-litellm", "litellm-requirements"),
    "aider":   ("venv-aider", "aider-requirements"),
    "eval":    ("venv-eval", "eval-requirements"),
    "webui":   ("venv-webui", "webui-requirements"),
}


def make_venv(name: str) -> str:
    """Create one Bob venv by short name (litellm|aider|eval|webui) — used for CI's granular
    runtime-venv step."""
    if name not in _VENV_REQ:
        raise RuntimeError(f"unknown venv '{name}' — one of {', '.join(_VENV_REQ)}")
    vname, base = _VENV_REQ[name]
    return osenv.new_bob_venv(vname, base)


def build_swap() -> str:
    """Build the llama-swap proxy (Go) -> bin/llama-swap (used by CI)."""
    _tools_on_path()
    import build
    build.configure(_load_config())
    return build.build_llama_swap()


# --- CLI dispatch --------------------------------------------------------------------------------

# Back-compat: the documented entry (`./setup.sh -SkipModels`, `./install_prereqs.sh --cpu`) and older
# muscle memory used PowerShell-style switches. Normalize them to the argparse `--kebab` form.
_FLAG_ALIASES = {
    "-skipmodels": "--skip-models", "-skipbuild": "--skip-build", "-skipvoice": "--skip-voice",
    "-launch": "--launch", "-withwebui": "--with-webui", "-cpu": "--cpu", "--cpu": "--cpu",
    "-profile": "--profile",
}


def _normalize_argv(argv: list) -> list:
    return [_FLAG_ALIASES.get(a.lower(), a) for a in argv]


def main(argv=None) -> int:
    argv = _normalize_argv(list(sys.argv[1:] if argv is None else argv))
    p = argparse.ArgumentParser(prog="python -m bob.kernel", description="Bob cold-start kernel (Tier 0/1).")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("prereqs", help="install toolchain + a venv-compatible Python (Tier 0)")
    sp.add_argument("--cpu", action="store_true", help="CPU-only tier (skip the CUDA toolkit)")

    for cmdname in ("setup", "bootstrap"):
        s = sub.add_parser(cmdname, help="fresh-machine bring-up (Tier 1)")
        s.add_argument("--skip-models", action="store_true")
        s.add_argument("--skip-build", action="store_true")
        s.add_argument("--skip-voice", action="store_true")
        s.add_argument("--launch", action="store_true")
        s.add_argument("--with-webui", action="store_true")
        s.add_argument("--cpu", action="store_true", help="force the CPU build tier (skip CUDA)")
        s.add_argument("--profile", default=None)

    sv = sub.add_parser("venv", help="create tools/venv-<name> (litellm|aider|eval|webui)")
    sv.add_argument("names", nargs="+")

    sub.add_parser("build-swap", help="build the llama-swap proxy (Go)")

    args = p.parse_args(argv)
    try:
        if args.cmd == "prereqs":
            from bob import install_prereqs
            return install_prereqs.install_prereqs(cpu=args.cpu)
        if args.cmd == "setup":
            return setup(skip_models=args.skip_models, skip_build=args.skip_build,
                         skip_voice=args.skip_voice, launch=args.launch, profile=args.profile,
                         with_webui=args.with_webui, cpu=args.cpu)
        if args.cmd == "bootstrap":
            bootstrap(skip_models=args.skip_models, skip_build=args.skip_build, profile=args.profile,
                      with_webui=args.with_webui, cpu=args.cpu)
            return 0
        if args.cmd == "venv":
            for n in args.names:
                print(make_venv(n), file=sys.stderr)
            return 0
        if args.cmd == "build-swap":
            print(build_swap(), file=sys.stderr)
            return 0
    except RuntimeError as e:
        print(f"kernel {args.cmd} failed: {e}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
