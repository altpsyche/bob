"""Tier-1 cold-start kernel: the fresh-machine bring-up, in Python.

"The one honest exception": an agent can't boot its own brain, so a non-conversational path survives.
It runs under the *system* python3 with `scripts/` on sys.path, BEFORE the venvs exist,
so it IMPORTS the same capability functions the agent and `bob --run` reach (provision.fetch_models,
generate.gen_all, build.build_llama/..., stack.stack_up, health.diagnose) rather than re-implementing or
subprocessing them. Only prereqs, the venv creation, and the *first* build are kernel-exclusive.

  python3 -m bob.kernel prereqs [--cpu] [--from-source] [--with-node]   # Tier 0: toolchain + Python
  python3 -m bob.kernel setup [flags]        # Tier 1: the 12-step fresh-machine orchestrator
  python3 -m bob.kernel bootstrap [flags]    #          submodules -> build -> venvs -> gen -> fetch
  python3 -m bob.kernel venv <name...>       #          create tools/venv-<name> (litellm|aider|eval|webui)
  python3 -m bob.kernel build-swap           #          install the llama-swap proxy (release binary or Go)
  python3 -m bob.kernel aider-setup          #          opt-in: create venv-aider from its lock

Flags: --skip-models --skip-build --skip-voice --launch --profile <p> --with-webui --with-aider
       --with-fabric --cpu --from-source
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


# --- config/user.json (the per-machine overlay) ---------------------------------------------------

def _user_config_path() -> Path:
    """The per-machine overlay file, resolved the way bob_config resolves it: env BOB_USER_CONFIG when
    set, else config/user.json in this checkout."""
    import os
    env = os.environ.get("BOB_USER_CONFIG")
    return Path(env) if env else REPO / "config" / "user.json"


def _read_user_config(path=None) -> dict:
    """The overlay as a dict; {} when it is absent, unreadable, or not a JSON object."""
    import json
    p = Path(path) if path else _user_config_path()
    try:
        cfg = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _write_user_config(cfg: dict, path=None) -> None:
    """Write the overlay atomically (temp file in the same dir, then os.replace), so an interrupted
    write never leaves a truncated user.json behind."""
    import json
    import os
    import tempfile
    p = Path(path) if path else _user_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".user-", suffix=".json.tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(cfg, indent=2) + "\n")
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --- the Bob venvs (one table: bootstrap, `venv <name>`, and aider-setup all read it) --------------

VENVS = {
    "litellm": ("venv-litellm", "litellm-requirements"),
    "aider":   ("venv-aider", "aider-requirements"),
    "eval":    ("venv-eval", "eval-requirements"),
    "webui":   ("venv-webui", "webui-requirements"),
}


def _profile_chosen() -> bool:
    """True when a profile was picked on purpose: env BOB_PROFILE, a persisted data/active-profile.json
    (`bob profile`, `--profile`, or an earlier auto-select), or activeProfile in the user overlay."""
    import os
    import bob_models
    if os.environ.get("BOB_PROFILE"):
        return True
    if bob_models._active_profile_file().exists():
        return True
    return "activeProfile" in _read_user_config()


# --- bootstrap -----------------------------------------------------------------------------------

def _select_profile(profile: str = None) -> None:
    """Profile selection for bootstrap. Explicit wins. Otherwise auto-select from detected VRAM only on a
    machine where no profile was ever chosen (_profile_chosen); once one was (by the user or an earlier
    setup), a re-run only prints the suggestion."""
    _tools_on_path()
    import bob_models
    import models as models_mod
    if profile:
        print(f"\n=== Select profile '{profile}' ===", file=sys.stderr)
        bob_models.set_active_profile(profile)
        return
    vram = osenv.gpu_vram_gb()
    sug = models_mod.suggested_profile(vram)
    active = bob_models.load_models_config().get("activeProfile")
    if sug and sug != active:
        if _profile_chosen():
            print(f"VRAM ~{vram} GB suggests profile '{sug}'; keeping your profile '{active}'. "
                  f"Switch with: bob profile {sug}", file=sys.stderr)
        else:
            print(f"\n=== VRAM check: auto-selecting profile ===\nDetected ~{vram} GB VRAM -> profile "
                  f"'{sug}' (change it any time with: bob profile <name>)", file=sys.stderr)
            bob_models.set_active_profile(sug)
    elif sug:
        print(f"VRAM ~{vram} GB -> profile '{active}' (good fit).", file=sys.stderr)


def bootstrap(skip_models: bool = False, skip_build: bool = False, profile: str = None,
              with_webui: bool = False, cpu: bool = False, from_source: bool = False,
              with_aider: bool = False) -> None:
    """Submodules -> engine + proxy -> Python venvs -> LiteLLM key -> gen configs -> fetch models.
    Re-runnable; heavy steps skippable. Imports the capability fns directly."""
    _tools_on_path()
    import build
    import generate
    import provision

    config = _load_config()

    _select_profile(profile)

    print("\n=== Prereqs ===", file=sys.stderr)
    if not _have("git"):
        raise RuntimeError("git missing")
    print("git    : ok", file=sys.stderr)
    print("cmake  : " + ("ok" if _have("cmake") else "not on PATH (needed only for a source build)"),
          file=sys.stderr)
    print("go     : " + ("ok" if _have("go") else "not on PATH (needed only for a source build of llama-swap)"),
          file=sys.stderr)
    py = osenv.bob_venv_python()
    print("python : " + (py if py else "MISSING (install Python 3.12, or ensure uv is available)"),
          file=sys.stderr)

    # Submodules.
    print("\n=== Submodules ===", file=sys.stderr)
    if subprocess.run(["git", "-C", str(REPO), "submodule", "update", "--init", "--recursive"]).returncode != 0:
        raise RuntimeError("submodule init failed")

    # Engine + proxy. The tier decision (GPU vs CPU, toolkit ensure, fail-loud on a GPU box with no toolkit)
    # lives in ONE place: lifecycle.ensure_engine, so setup cannot drift from `bob build` / `bob update`.
    # on_block='stop' means a fresh setup fails loud with the one-command route (a Fedora distrobox on atomic,
    # ./install_prereqs.sh on a mutable distro) rather than silently shipping a CPU-tier build on GPU
    # hardware; --cpu is the consent escape hatch.
    build.configure(config)
    if not skip_build:
        print("\n=== Build llama.cpp ===", file=sys.stderr)
        from bob import lifecycle
        result = lifecycle.ensure_engine(cpu=cpu, from_source=from_source, on_block="stop", config=config)
        print(f"  tier: {result['tier']} ({result['reason']})\n  {result['detail']}", file=sys.stderr)

        print("\n=== llama-swap ===", file=sys.stderr)
        try:
            print(f"  {build.build_llama_swap(from_source=from_source)}", file=sys.stderr)
        except RuntimeError as e:
            # Setup carries on (venvs, configs, models are all still useful), but the endpoint cannot start
            # without the proxy, so say exactly how to finish.
            print(f"  [x] llama-swap install failed: {e}\n      The pinned release binary needs network access; "
                  "a source build (--from-source, or a platform with no pinned release) needs Go "
                  "(./install_prereqs.sh --from-source installs it). Then re-run setup.",
                  file=sys.stderr)
    else:
        print("Skipping builds (--skip-build)", file=sys.stderr)

    # Python tools: ISOLATED venvs (open-webui & aider have conflicting dep pins). venv-litellm is the
    # runtime; webui and aider are opt-in.
    print("\n=== Python venvs (3.12+) + tools ===", file=sys.stderr)
    if py:
        names = ["litellm"] + (["webui"] if with_webui else []) + (["aider"] if with_aider else [])
        if not with_webui:
            print("  skipping venv-webui (open-webui is opt-in: re-run with --with-webui to install)",
                  file=sys.stderr)
        if not with_aider:
            print("  skipping venv-aider (aider is opt-in: bob aider-setup, or re-run with --with-aider)",
                  file=sys.stderr)
        for name in names:
            vname, base = VENVS[name]
            osenv.new_bob_venv(vname, base, python=py)
    else:
        print("Skipping venvs: Python 3.12+ not found.", file=sys.stderr)

    # The LiteLLM master key is a generated per-machine secret. Make sure it exists BEFORE any client config
    # is generated, so every generated file (litellm, Continue, dsh, aider) carries the same key.
    import bob_core
    bob_core._litellm_key(config)

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

def _is_bob_copy(target: Path, dest: Path) -> bool:
    """True when `dest` is a plain-file copy of the generated `target` (the copy fallback of _wire): its
    first line is the generated file's header, which no hand-written config carries."""
    try:
        head = target.read_text(encoding="utf-8").splitlines()[:1]
        return bool(head) and head[0].startswith("# GENERATED") and \
            dest.read_text(encoding="utf-8").splitlines()[:1] == head
    except (OSError, UnicodeDecodeError):
        return False


def _wire(target: Path, link: Path) -> None:
    """Symlink `link` -> `target` (edits in the repo propagate live); fall back to a copy where symlinks
    aren't permitted. Never clobbers the user's own file: an existing symlink or a hand-written file is
    left as-is. A stale plain-file copy Bob made earlier (same generated header) is refreshed, so a
    regenerated key reaches it. Prints exactly what it wrote where."""
    import shutil
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        try:
            same = link.resolve() == target.resolve()
        except OSError:
            same = False
        print(f"  {'already linked' if same else 'exists, left as-is'}: {link}"
              + ("" if same else "  (delete it to re-wire)"), file=sys.stderr)
        return
    if link.exists():
        if _is_bob_copy(target, link):
            shutil.copy(target, link)
            print(f"  refreshed Bob's copy  {target}  ->  {link}", file=sys.stderr)
        else:
            print(f"  exists, left as-is: {link}  (your own file; delete it to re-wire)", file=sys.stderr)
        return
    try:
        link.symlink_to(target)
        print(f"  linked  {link}  ->  {target}", file=sys.stderr)
    except (OSError, NotImplementedError):
        shutil.copy(target, link)
        print(f"  copied  {target}  ->  {link}   (no symlink privilege; setup refreshes this copy)",
              file=sys.stderr)


def _aider_conf() -> Path:
    """The generated aider config `bob aider` runs with (config/aider/.aider.conf.yml)."""
    return REPO / "config" / "aider" / ".aider.conf.yml"


def _remove_legacy_aider_link(home: Path = None) -> None:
    """Remove ~/.aider.conf.yml ONLY when it is a symlink into this checkout's config/aider (what earlier
    setups created). `bob aider` passes --config explicitly, so the home-dir link is no longer needed. A
    real file, or a link that points anywhere else, is the user's and is never touched."""
    import os
    link = (home or Path.home()) / ".aider.conf.yml"
    if not link.is_symlink():
        return
    raw = Path(os.readlink(link))
    raw = raw if raw.is_absolute() else link.parent / raw
    dest = Path(os.path.abspath(raw))
    aider_dir = REPO / "config" / "aider"
    inside = dest.is_relative_to(aider_dir) or dest.is_relative_to(aider_dir.resolve())
    if inside:
        link.unlink()
        print(f"  removed the legacy symlink {link} (bob aider passes --config {_aider_conf()})",
              file=sys.stderr)


def setup_clients() -> None:
    """Point VS Code Continue at the repo's generated config (symlink, copy fallback) and merge the
    DeepSeek Harness drop-ins. Generates the configs first so the link targets exist. Only Bob-owned
    entries are written, an existing user file is never clobbered, and every write is printed. dsh owns
    its own settings document, so that one is merged rather than linked, and skips when dsh is not
    installed. aider is opt-in (bob aider-setup) and runs with an explicit --config, so nothing is wired
    into the home dir for it."""
    _tools_on_path()
    import generate
    generate.configure(_load_config())
    generate.gen_continue()
    home = Path.home()
    _wire(REPO / "config" / "continue" / "config.yaml", home / ".continue" / "config.yaml")
    _remove_legacy_aider_link(home)

    generate.gen_dsh()
    print(generate.install_dsh(), file=sys.stderr)

    if not _have("node"):
        print("  Node.js not found: Continue's npx-based MCP servers and n8n need it. Install Node.js "
              "(./install_prereqs.sh --with-node) to use them; everything else works without it.",
              file=sys.stderr)


def setup_aider(force: bool = False) -> str:
    """Opt-in aider: create tools/venv-aider from its pinned lock and generate config/aider/.aider.conf.yml.
    Idempotent (new_bob_venv reuses an in-range venv). Also removes a legacy ~/.aider.conf.yml symlink
    into this repo. Returns a status line; raises RuntimeError when the venv cannot be built."""
    _tools_on_path()
    import generate
    vname, base = VENVS["aider"]
    py = osenv.new_bob_venv(vname, base, force=force)
    generate.configure(_load_config())
    gen = getattr(generate, "gen_aider", None)
    (gen or generate.gen_all)()
    _remove_legacy_aider_link()
    return (f"aider ready: {osenv.venv_exe(vname, 'aider')} (python {py})\n"
            f"  config: {_aider_conf()}\n  run it with: bob aider")


def run_aider(args: list) -> int:
    """Run the venv's aider in the current folder with Bob's generated config (--config), unless the caller
    passed its own --config. The LiteLLM key reaches aider through the environment (AIDER_OPENAI_API_KEY,
    which aider ranks above its config file), never on the command line."""
    import os
    exe = osenv.venv_exe(VENVS["aider"][0], "aider")
    if not Path(exe).exists():
        print("aider is not installed (it is opt-in). Run: bob aider-setup", file=sys.stderr)
        return 1
    argv = [str(exe)]
    if not any(a == "--config" or a.startswith("--config=") or a == "-c" for a in args):
        conf = _aider_conf()
        if not conf.exists():
            print(f"aider config missing: {conf}. Run: bob aider-setup (or bob gen)", file=sys.stderr)
            return 1
        argv += ["--config", str(conf)]
    env = dict(os.environ)
    if not env.get("AIDER_OPENAI_API_KEY"):
        import bob_core
        env["AIDER_OPENAI_API_KEY"] = bob_core._litellm_key(_load_config())
    return subprocess.run(argv + list(args), env=env).returncode


def _is_bob_link(link: Path, rel: str) -> bool:
    """True when `link` is a symlink whose target is `<a Bob checkout>/<rel>` (this one or an older clone),
    i.e. a link an earlier install_cli made. Works for a dangling link too (the clone was moved)."""
    import os
    if not link.is_symlink():
        return False
    raw = Path(os.readlink(link))
    raw = Path(os.path.abspath(raw if raw.is_absolute() else link.parent / raw))
    parts = Path(rel).parts
    if tuple(raw.parts[-len(parts):]) != parts:
        return False
    root = raw
    for _ in parts:
        root = root.parent
    return root == REPO or (root / "scripts" / "bob").is_dir() or not raw.exists()


def _link_cli(link: Path, target: Path, rel: str, label: str) -> None:
    """Point `link` at `target`, replacing only a symlink Bob made (_is_bob_link). Anything else at that
    path is the user's (their own install of the tool, a script) and is left alone with a note."""
    if link.is_symlink() and not _is_bob_link(link, rel):
        print(f"'{label}' left as-is: {link} is your own symlink (-> {link.readlink()})", file=sys.stderr)
        return
    if link.exists() and not link.is_symlink():
        print(f"'{label}' left as-is: {link} is your own file, not a Bob link", file=sys.stderr)
        return
    if link.is_symlink():
        link.unlink()
    link.symlink_to(target)
    print(f"'{label}' installed: {link} -> {target}", file=sys.stderr)


def install_cli() -> None:
    """Install the `bob` command on PATH. POSIX: symlink the repo-root ./bob shim (+ fabric, once built)
    into ~/.local/bin, replacing only links Bob made. Windows: a bob.cmd shim in scoop\\shims that points
    at `python -m bob`."""
    if osenv.os_name() == "windows":  # pragma: no cover — Windows path
        _install_cli_windows()
        return
    import os
    bindir = Path.home() / ".local" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    _link_cli(bindir / "bob", REPO / "bob", "bob", "bob")

    fabric_exe = osenv.bin_exe("fabric")
    if fabric_exe.exists():
        _link_cli(bindir / "fabric", fabric_exe, f"bin/{fabric_exe.name}", "fabric")

    if str(bindir) not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"NOTE: {bindir} is not on PATH. fish: fish_add_path {bindir} | bash/zsh: add it to your rc, "
              "then open a new shell.", file=sys.stderr)
    print("Open a NEW terminal (with ~/.local/bin on PATH), then try:  bob help", file=sys.stderr)


# The first line of every .cmd shim install_cli writes, so a re-run can tell its own shim from the user's.
_CMD_MARKER = "REM bob-shim"


def _write_cmd_shim(path: Path, body: str, legacy_hint: str) -> None:  # pragma: no cover (Windows path)
    """Write a .cmd shim unless a user-owned file is already there. Ours carries _CMD_MARKER; one written
    before the marker existed is recognised by `legacy_hint`."""
    if path.exists():
        try:
            old = path.read_text(encoding="ascii", errors="replace")
        except OSError:
            old = ""
        if _CMD_MARKER not in old and legacy_hint not in old:
            print(f"left as-is: {path} is not a Bob shim", file=sys.stderr)
            return
    path.write_text(f"@echo off\r\n{_CMD_MARKER}\r\n{body}", encoding="ascii")
    print(f"installed: {path}", file=sys.stderr)


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
    _write_cmd_shim(shim_dir / "bob.cmd", f'set "PYTHONPATH={REPO / "scripts"}"\r\n"{py}" -m bob %*\r\n',
                    legacy_hint="-m bob")
    fabric_exe = osenv.bin_exe("fabric")
    if fabric_exe.exists():
        _write_cmd_shim(shim_dir / "fabric.cmd", f'"{fabric_exe}" %*\r\n', legacy_hint="\\bin\\fabric")
    print("Open a NEW terminal, then try:  bob help", file=sys.stderr)


# Reset copy + confirm word — one place, shared by `bob reset` (CLI) and the shell's /reset, so the
# danger text and the type-to-confirm keyword can't drift between the two front doors.
RESET_WARNING = ("This deletes ALL Bob data on this machine (chats, memory, keys, schedules) and "
                 "returns to first-run.")
RESET_CONFIRM = "reset"


def reset_done_line(removed: int) -> str:
    """The one-line confirmation shown after a successful reset (both front doors)."""
    return f"reset complete ({removed} item(s) removed). Run `bob` to begin fresh."


def reset_all_data(data_dir=None, user_cfg=None) -> int:
    """Factory reset: delete every local data store and clear the onboarding markers so the next launch
    is a fresh first-run. The inverse of onboard(). Removes everything under `data_dir()` (sessions.db,
    bob.db memory, secrets.json, schedules.json, .onboarded, shell-history, WAL/SHM sidecars, caches)
    and strips the `bob` onboarding key from config/user.json (which, with the wiped memory profile,
    re-arms onboarding). Best-effort per item so one locked/missing file doesn't abort the wipe. Paths
    are injectable so tests never touch real data. Returns the count of items removed. The SINGLE wipe
    core — both `bob reset` (CLI) and the shell's `/reset` call it."""
    import shutil
    removed = 0
    d = Path(data_dir) if data_dir else osenv.data_dir()
    for p in list(d.glob("*")):
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink()
            removed += 1
        except OSError:
            pass
    cfg_path = Path(user_cfg) if user_cfg else _user_config_path()
    cfg = _read_user_config(cfg_path)
    if "bob" in cfg:
        cfg.pop("bob", None)
        try:
            _write_user_config(cfg, cfg_path)
            removed += 1
        except OSError:
            pass
    return removed


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
    marked = "bob" in _read_user_config()
    return (not marked) or (not _has_profile_rows())


def _onboard_declined() -> bool:
    """True if the user has already declined the shell's onboarding offer (config bob.onboardDeclined),
    so a bare `bob` never re-nags. Setup's own onboarding is unaffected."""
    bob = _read_user_config().get("bob")
    return bool(isinstance(bob, dict) and bob.get("onboardDeclined"))


def _record_onboard_declined() -> None:
    cfg = _read_user_config()
    if not isinstance(cfg.get("bob"), dict):
        cfg["bob"] = {}
    cfg["bob"]["onboardDeclined"] = True
    _write_user_config(cfg)


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
    if not sys.stdin.isatty():
        print("Bob: onboarding skipped (non-interactive). Run `bob memory init-profile` later.",
              file=sys.stderr)
        return

    def bob(msg: str) -> None:
        print(f"Bob: {msg}")

    # A re-onboard (marked but the profile never seeded) shouldn't re-nag for a key already on file.
    _existing = _read_user_config()
    has_key = bool(((_existing.get("peers") or {}).get("deepseek") or {}).get("apiKey"))

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

    cfg = _read_user_config()
    if not isinstance(cfg.get("bob"), dict):
        cfg["bob"] = {}
    key_added = False
    if api_key:
        cfg.setdefault("peers", {}).setdefault("deepseek", {})
        if cfg["peers"]["deepseek"].get("apiKey") != api_key:
            cfg["peers"]["deepseek"]["apiKey"] = api_key
            key_added = True
    _write_user_config(cfg)

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


# The optional Docker services (SearXNG / Langfuse) are no longer provisioned eagerly at setup: they are
# opt-in and start on demand via `bob services <name> start` (scripts/tools/stack.py), which prepares the
# compose .env + per-service files and runs the guided Docker install if Docker is missing.


def verify_install() -> int:
    """The 'verifies against versions.lock' step the one-command installer runs after setup. Reuses the
    existing readers: check_reproducibility (installed submodules/models vs the lock) + check_sync (the
    lock is in sync with its sources). Prints a green/red summary; returns 0 iff both pass."""
    from bob import versions
    ok = True
    try:
        drift = versions.check_reproducibility()
    except Exception as e:  # noqa: BLE001 — a missing lock / git is a red, not a crash
        print(f"  [x] reproducibility check failed: {e}", file=sys.stderr)
        drift, ok = ["reproducibility check errored"], False
    if drift:
        ok = False
        print("  [x] versions.lock drift:", file=sys.stderr)
        for d in drift:
            print(f"        - {d}", file=sys.stderr)
        print("      fix: git submodule update --init --recursive  (or: bob fetch / bob lock)",
              file=sys.stderr)
    else:
        print("  [OK] installed submodules + models match versions.lock", file=sys.stderr)
    if versions.check_sync() != 0:
        ok = False   # check_sync prints its own STALE message
    else:
        print("  [OK] versions.lock is in sync with its sources", file=sys.stderr)
    print(("Verify: PASS" if ok else "Verify: FAIL"), file=sys.stderr)
    return 0 if ok else 1


# --- setup (the 12-step orchestrator) ------------------------------------------------------------

def setup(skip_models: bool = False, skip_build: bool = False, skip_voice: bool = False,
          launch: bool = False, profile: str = None, with_webui: bool = False, cpu: bool = False,
          from_source: bool = False, with_aider: bool = False, with_fabric: bool = False) -> int:
    """The fresh-machine orchestrator. Idempotent; safe to re-run. Prerequisites must be installed first
    via `python3 -m bob.kernel prereqs`. aider and fabric are opt-in (--with-aider / --with-fabric, or
    later `bob aider-setup` / `bob fabric-setup`)."""
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
    except Exception as e:  # noqa: BLE001 (diagnose is informational)
        print(f"diagnose: {e}", file=sys.stderr)

    _step(2, total, "Core tooling")
    if not _have("git"):
        raise RuntimeError("git not found. Install Git, then re-run setup.")
    if is_win and not _have("scoop"):  # pragma: no cover
        print("  scoop not found: setup continues, but the `bob` command shim goes into scoop\\shims, so "
              "install scoop (irm get.scoop.sh | iex) and re-run setup to get `bob` on PATH.", file=sys.stderr)
    print("git ok", file=sys.stderr)

    _step(3, total, "Prerequisite check")
    missing = []
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
    # Optional tools: each one only gates a feature, never the setup.
    if not _have("node"):
        print("  Node.js not found (optional): needed only for n8n and Continue's npx MCP servers.",
              file=sys.stderr)
    if with_fabric and not _have("go"):
        print("  Go not found: --with-fabric needs it to build fabric (./install_prereqs.sh --from-source "
              "installs Go).", file=sys.stderr)
    print("  Prerequisites ok.", file=sys.stderr)

    _step(4, total, "C++ toolchain (compiler required only for a source build)")
    server_exe = osenv.bin_exe("llama-server")
    from bob import lifecycle
    # A driver-only prebuilt engine needs NO compiler (it's downloaded, not built), so don't demand one when a
    # matching prebuilt is available on the default path: this lets an atomic host (compiler layered but not
    # active until the next boot) run setup straight away instead of being forced to reboot for a build it
    # won't do. --from-source always needs the compiler.
    prebuilt_ok = (not from_source) and lifecycle.prebuilt_available(cpu=cpu)
    needs_compile = not skip_build and not server_exe.exists() and not prebuilt_ok
    if needs_compile:
        if is_win:  # pragma: no cover
            print("  (Windows: ensure VS2022 'Desktop development with C++' is installed)", file=sys.stderr)
        elif _have("g++") or _have("gcc") or _have("cc"):
            print("  gcc/g++ ok", file=sys.stderr)
        else:
            raise RuntimeError("No C++ compiler found. Run ./install_prereqs.sh --from-source, then re-run. "
                               "(Pass --skip-build if you have a prebuilt bin/llama-server.)")
    elif prebuilt_ok and not server_exe.exists():
        print("  C++ toolchain not required (a driver-only prebuilt engine is available)", file=sys.stderr)
    else:
        print("  C++ toolchain check skipped (build not needed)", file=sys.stderr)

    _step(5, total, "cmake (only for a source build; llama.cpp needs a cmake inside osenv.CMAKE_RANGE)")
    if not needs_compile:
        print("  skipped (no source build on this run)", file=sys.stderr)
    elif not is_win:
        try:
            print(f"  cmake ready: {osenv.linux_cmake3(REPO)}", file=sys.stderr)
        except (RuntimeError, OSError) as e:
            print(f"  cmake provisioning failed: {e}", file=sys.stderr)
    else:  # pragma: no cover
        print("  (Windows: winget/VS-bundled cmake handled by the build)", file=sys.stderr)

    _step(6, total, "Bootstrap: submodules -> engine -> venvs -> configs -> models", "first build takes 5-15 min")
    bootstrap(skip_models=skip_models, skip_build=skip_build, profile=profile, with_webui=with_webui, cpu=cpu,
              from_source=from_source, with_aider=with_aider)

    _step(7, total, "Wire clients (Continue + dsh; aider when opted in)")
    try:
        setup_clients()
        if with_aider:
            print(setup_aider(), file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"  client wiring failed (non-fatal): {e}", file=sys.stderr)

    _step(8, total, "fabric (opt-in)")
    if with_fabric:
        build.configure(config)
        try:
            print(build.setup_fabric(), file=sys.stderr)
        except RuntimeError as e:
            print(f"  fabric setup failed (non-fatal): {e}", file=sys.stderr)
    else:
        print("  skipped (opt-in: bob fabric-setup, or re-run setup with --with-fabric)", file=sys.stderr)

    _step(9, total, "Install 'bob' CLI command")
    try:
        install_cli()
    except Exception as e:  # noqa: BLE001
        print(f"  CLI install failed (non-fatal): {e}", file=sys.stderr)

    _step(10, total, "Voice + Vision setup", "faster-whisper STT model + audio deps, piper TTS voice")
    if skip_voice:
        print("  Skipped (--skip-voice).", file=sys.stderr)
    else:
        provision.configure(config)
        try:
            print(provision.setup_voice(), file=sys.stderr)
        except Exception as e:  # noqa: BLE001 (voice is optional; never sink a good build)
            print(f"  voice setup failed (non-fatal): {e}", file=sys.stderr)

    _step(11, total, "Memory lock (mlock)")
    st = osenv.mlock_status()
    print("  mlock: " + ("granted" if st["granted"] else "not granted") + f": {st['detail']}", file=sys.stderr)
    if not is_win and not st["granted"]:
        print("  Linux: raise 'ulimit -l' (memlock) if you enable mlockBig. See: bob mlock --grant",
              file=sys.stderr)

    _step(12, total, "Optional add-on services")
    print("  Docker-free by default. Web search uses the built-in ddgs provider (no service needed).",
          file=sys.stderr)
    print("  Opt-in add-ons, started on demand: bob services n8n start (native, needs Node.js), "
          "bob services searxng|langfuse start (Docker, guided install on first start).", file=sys.stderr)
    print("  Opt-in tools: bob aider-setup (aider), bob fabric-setup (fabric).", file=sys.stderr)

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

def make_venv(name: str) -> str:
    """Create one Bob venv by short name (litellm|aider|eval|webui), used for CI's granular
    runtime-venv step."""
    if name not in VENVS:
        raise RuntimeError(f"unknown venv '{name}': one of {', '.join(VENVS)}")
    vname, base = VENVS[name]
    return osenv.new_bob_venv(vname, base)


def build_swap(from_source: bool = False) -> str:
    """Install the llama-swap proxy -> bin/llama-swap: the pinned release binary, or a Go build with
    from_source (used by CI)."""
    _tools_on_path()
    import build
    build.configure(_load_config())
    return build.build_llama_swap(from_source=from_source)


# --- CLI dispatch --------------------------------------------------------------------------------

# Back-compat: the documented entry (`./setup.sh -SkipModels`, `./install_prereqs.sh --cpu`) and older
# muscle memory used PowerShell-style switches. Normalize them to the argparse `--kebab` form.
_FLAG_ALIASES = {
    "-skipmodels": "--skip-models", "-skipbuild": "--skip-build", "-skipvoice": "--skip-voice",
    "-launch": "--launch", "-withwebui": "--with-webui", "-cpu": "--cpu", "--cpu": "--cpu",
    "-profile": "--profile", "-fromsource": "--from-source", "-withaider": "--with-aider",
    "-withfabric": "--with-fabric", "-withnode": "--with-node",
}


def _normalize_argv(argv: list) -> list:
    return [_FLAG_ALIASES.get(a.lower(), a) for a in argv]


def main(argv=None) -> int:
    argv = _normalize_argv(list(sys.argv[1:] if argv is None else argv))
    p = argparse.ArgumentParser(prog="python -m bob.kernel", description="Bob cold-start kernel (Tier 0/1).")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("prereqs", help="install toolchain + a venv-compatible Python (Tier 0)")
    sp.add_argument("--cpu", action="store_true", help="CPU-only tier (skip the CUDA toolkit)")
    sp.add_argument("--from-source", action="store_true",
                    help="also install the build toolchain (compiler, cmake, ninja, Go) and, on a GPU box, the "
                         "CUDA Toolkit for a source build (default is the driver-only prebuilt)")
    sp.add_argument("--with-node", action="store_true",
                    help="also install Node.js + npm (optional: n8n, Continue's npx MCP servers)")

    for cmdname in ("setup", "bootstrap"):
        s = sub.add_parser(cmdname, help="fresh-machine bring-up (Tier 1)")
        s.add_argument("--skip-models", action="store_true")
        s.add_argument("--skip-build", action="store_true")
        s.add_argument("--skip-voice", action="store_true")
        s.add_argument("--launch", action="store_true")
        s.add_argument("--with-webui", action="store_true", help="also install Open WebUI (opt-in)")
        s.add_argument("--with-aider", action="store_true", help="also install aider (opt-in)")
        s.add_argument("--cpu", action="store_true", help="force the CPU build tier (skip CUDA)")
        s.add_argument("--from-source", action="store_true",
                       help="build the engine from source instead of the prebuilt binary")
        s.add_argument("--profile", default=None)
        if cmdname == "setup":
            s.add_argument("--with-fabric", action="store_true", help="also build + configure fabric (opt-in)")

    sv = sub.add_parser("venv", help="create tools/venv-<name> (litellm|aider|eval|webui)")
    sv.add_argument("names", nargs="+")

    sw = sub.add_parser("build-swap", help="install the llama-swap proxy (pinned release binary, or Go build)")
    sw.add_argument("--from-source", action="store_true", help="build it from the submodule with Go")
    sa = sub.add_parser("aider-setup", help="opt-in: create venv-aider from its lock + generate its config")
    sa.add_argument("--force", action="store_true", help="recreate the venv")
    sub.add_parser("verify-install", help="verify installed submodules/models against versions.lock")

    args = p.parse_args(argv)
    try:
        if args.cmd == "prereqs":
            from bob import install_prereqs
            return install_prereqs.install_prereqs(cpu=args.cpu, from_source=args.from_source,
                                                   with_node=args.with_node)
        if args.cmd == "setup":
            return setup(skip_models=args.skip_models, skip_build=args.skip_build,
                         skip_voice=args.skip_voice, launch=args.launch, profile=args.profile,
                         with_webui=args.with_webui, cpu=args.cpu, from_source=args.from_source,
                         with_aider=args.with_aider, with_fabric=args.with_fabric)
        if args.cmd == "bootstrap":
            bootstrap(skip_models=args.skip_models, skip_build=args.skip_build, profile=args.profile,
                      with_webui=args.with_webui, cpu=args.cpu, from_source=args.from_source,
                      with_aider=args.with_aider)
            return 0
        if args.cmd == "venv":
            for n in args.names:
                print(make_venv(n), file=sys.stderr)
            return 0
        if args.cmd == "build-swap":
            print(build_swap(from_source=args.from_source), file=sys.stderr)
            return 0
        if args.cmd == "aider-setup":
            print(setup_aider(force=args.force), file=sys.stderr)
            return 0
        if args.cmd == "verify-install":
            return verify_install()
    except RuntimeError as e:
        print(f"kernel {args.cmd} failed: {e}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
