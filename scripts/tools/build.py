"""Bob native-build capabilities — the toolchain half of provisioning.

Builds llama.cpp + llama-swap + fabric from source in Python — the CUDA-root / cmake-flags /
host-compiler resolution lives in the osenv seam; cmake / nvcc / go stay subprocess. Native-from-source
is the default and is exercised only in the non-gating GPU/release-tag CI tier, so this heavy code never
gates a per-PR merge.

CLI-only + long (`bob build`, `bob fabric-setup`) — not agent tools. Import-clean under a bare system
python (no requests / venv-only deps) so the cold-start kernel calls build_llama() directly
before the venv exists. Each fn returns a status string and raises RuntimeError on failure (the cli
handler prints + exits non-zero)."""
import shutil
import subprocess
import sys
from pathlib import Path

_cfg: dict = {}

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO / "scripts"
SRC_LLAMA = REPO / "external" / "llama.cpp"
SRC_SWAP = REPO / "external" / "llama-swap"
SRC_FABRIC = REPO / "external" / "fabric"
BIN = REPO / "bin"


def configure(config: dict) -> None:
    global _cfg
    _cfg = config
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))


def _run(argv, **kw) -> None:
    """Run a build subprocess (inherits stdio for live output), raising RuntimeError on non-zero."""
    rc = subprocess.run([str(a) for a in argv], **kw).returncode
    if rc != 0:
        raise RuntimeError(f"command failed (exit {rc}): {' '.join(str(a) for a in argv)}")


# --- cmake resolution (the Windows VS/winget dance vs Linux cmake<4) -------------------------------

def _resolve_cmake(generator: str) -> str:
    """A cmake inside osenv.CMAKE_RANGE (llama.cpp rejects 4.x). Windows: PATH cmake if in range, else
    VS-bundled, else winget the pinned osenv.CMAKE_PIN. Linux: osenv.linux_cmake3 (system cmake or a cached
    Kitware pin) + require ninja."""
    import osenv
    if osenv.os_name() != "windows":
        cmake = osenv.linux_cmake3(REPO)
        if generator == "Ninja" and not shutil.which("ninja"):
            raise RuntimeError("Ninja not found. Install it: apt/dnf/pacman/zypper install ninja(-build).")
        return cmake
    # Windows  # pragma: no cover — exercised only on Windows
    path_cmake = shutil.which("cmake")
    if path_cmake:
        out = subprocess.run(["cmake", "--version"], capture_output=True, text=True)
        if osenv.cmake_in_range(out.stdout):
            return "cmake"
        print("PATH cmake is out of range for llama.cpp; looking for VS-bundled cmake...", file=sys.stderr)
    vswhere = Path(r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe")
    if vswhere.exists():
        vs = subprocess.run([str(vswhere), "-latest", "-products", "*", "-requires",
                             "Microsoft.VisualStudio.Component.VC.CMake.Project", "-property", "installationPath"],
                            capture_output=True, text=True).stdout.strip()
        if vs:
            cand = Path(vs) / "Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin/cmake.exe"
            if cand.exists():
                return str(cand)
    print(f"Installing cmake {osenv.CMAKE_PIN} via winget...", file=sys.stderr)
    _run(["winget", "install", "Kitware.CMake", "--version", osenv.CMAKE_PIN, "--silent",
          "--accept-package-agreements", "--accept-source-agreements"])
    cmake = shutil.which("cmake")
    if not cmake:
        raise RuntimeError("cmake still not found after install — open a new terminal and retry.")
    return cmake


# --- build llama.cpp (CUDA or CPU) ----------------------------------------------------------------

def build_llama(cpu: bool = False, arch: int = 0, force: bool = False, cuda_root: str = "",
                cuda_archs: str = "", portable: bool = False) -> str:
    """(Re)build llama.cpp -> bin/llama-server. Auto-detects arch + CUDA root (osenv) unless given; CPU
    build with cpu=True. Installed into bin/ by per-file replace; Windows stages CUDA runtime DLLs.

    cuda_archs (e.g. '75;80;89;120') builds a FAT distribution binary that runs on every listed NVIDIA gen,
    with NO local GPU required (only the CUDA toolkit) — the mode the CI publish job uses to produce the
    prebuilt asset. It implies a CUDA build and bypasses nvidia-smi arch detection.

    portable=True compiles the CPU code for a fixed x86-64 baseline (-DGGML_NATIVE=OFF: SSE4.2, AVX, AVX2,
    FMA, F16C, BMI2; no AVX-512) instead of the build machine's own instruction set. A published engine runs
    on machines other than the one that built it, and a native build crashes with an illegal instruction on
    a CPU that lacks what the builder had. A local build stays native: it runs where it was built."""
    import os
    import osenv

    if cuda_archs:
        cpu = False   # a distribution CUDA build is GPU-tier by definition
    exe = osenv.exe_name("llama-server")
    win = osenv.os_name() == "windows"
    flags = osenv.resolve_build_cmake_flags(cpu=cpu, arch=arch)

    if not force and (BIN / exe).exists():
        # "Exists" is not "current": after a submodule bump the binary on disk is from the OLD revision, and
        # skipping on presence alone leaves a stale engine while reporting success. Compare what we built
        # from against what the checkout pins (the same test the prebuilt path applies to a published
        # asset). An unknown commit (a marker from before this field, or a hand-placed binary) is treated
        # as current, so this can never turn an existing working install into a surprise rebuild.
        built = (osenv.build_tier_marker(BIN) or {}).get("commit")
        pinned = _git_head(SRC_LLAMA)
        if built and pinned and built != pinned:
            print(f"{exe} was built from {built[:8]} but this checkout pins {pinned[:8]}; rebuilding.",
                  file=sys.stderr)
        else:
            return f"{exe} already built — skipping (use --force to rebuild)."
    if not (SRC_LLAMA / "CMakeLists.txt").exists():
        raise RuntimeError(f"llama.cpp submodule not found at {SRC_LLAMA}. Run: git submodule update --init --recursive")
    # The Ninja generator needs the MSVC toolchain (cl.exe) + Ninja on PATH; osenv.ensure_msvc_env folds the
    # VS environment in (like a Developer Command Prompt) so a plain `bob build` works from any shell. Only
    # matters once we're actually going to build (after the already-built short-circuit above).
    if win and not osenv.ensure_msvc_env():  # pragma: no cover — Windows only
        raise RuntimeError("MSVC toolchain not found. Install Visual Studio 2022 with the 'Desktop "
                           "development with C++' workload (./install_prereqs.bat), then re-run.")

    cuda_host_cxx = None
    cuda_major = "12"
    lines = []
    arch_cmake = ""   # the value handed to -DCMAKE_CUDA_ARCHITECTURES (single arch, or a fat ';'-list)
    if flags["Cuda"]:
        if cuda_archs:
            # Distribution build: an explicit arch list, no GPU detection. Needs the toolkit, not a GPU.
            arch_cmake = cuda_archs
            lines.append(f"Distribution build: CUDA archs [{cuda_archs}] (no nvidia-smi detection)")
            if not cuda_root:
                cuda_root = osenv.best_cuda_root(120) or ""
                if not cuda_root:
                    raise RuntimeError("distribution CUDA build needs a CUDA toolkit (>= 12.8); none found. "
                                       "Install one, pass cuda_root=..., or run in a CUDA devel container.")
        else:
            if arch == 0:
                gpu = osenv.gpu_arch()
                if gpu:
                    arch = gpu["CudaArch"]
                    lines.append(f"Detected GPU: {gpu['Gen']} (sm_{arch})")
                else:
                    lines.append("Could not detect GPU via nvidia-smi — defaulting to sm_120 (Blackwell). "
                                 "Pass arch=... to override, or use --cpu.")
                    arch = 120
            if not cuda_root:
                cuda_root = osenv.best_cuda_root(arch) or ""
                if not cuda_root:
                    if arch >= 120:
                        raise RuntimeError(osenv.cuda_missing_message())
                    raise RuntimeError(f"No compatible CUDA toolkit for sm_{arch}. Install CUDA 12.x, pass "
                                       "cuda_root=..., or build --cpu.")
            arch_cmake = str(arch)
        lines += [f"CUDA archs   : {arch_cmake}", f"CUDA toolkit : {cuda_root}"]
        nvcc = Path(cuda_root) / "bin" / osenv.exe_name("nvcc")
        if not win:
            cuda_host_cxx = osenv.cuda_host_compiler()
            if cuda_host_cxx:
                lines.append(f"CUDA host g++: {cuda_host_cxx}")
            osenv.assert_cuda_host_compiler_ok(nvcc, cuda_host_cxx)  # fail fast before the long build
        else:  # pragma: no cover — Windows CUDA env wiring
            os.environ["CUDA_PATH"] = cuda_root
            ver_tag = Path(cuda_root).name.lstrip("v").replace(".", "_")
            os.environ[f"CUDA_PATH_V{ver_tag}"] = cuda_root
        import re
        m = re.match(r"^v?(\d+)", Path(cuda_root).name)
        cuda_major = m.group(1) if m else "12"
    else:
        lines.append("CPU build (-DGGML_CUDA=OFF) — no GPU / CUDA toolkit required.")

    build_dir = SRC_LLAMA / "build"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    cmake = _resolve_cmake(flags["Generator"])
    lines.append(f"cmake       : {cmake}")

    # One recipe for both OSes: the single-config Ninja generator. nvcc's host compiler is cl.exe on Windows
    # (from ensure_msvc_env) and cuda_host_cxx on Linux (set only in the `if not win` block above).
    if flags["Cuda"]:
        nvcc = Path(cuda_root) / "bin" / osenv.exe_name("nvcc")
        cfg = [cmake, "-B", "build", "-G", flags["Generator"], "-DGGML_CUDA=ON",
               f"-DCMAKE_CUDA_COMPILER={nvcc}", f"-DCMAKE_CUDA_ARCHITECTURES={arch_cmake}",
               "-DGGML_CUDA_FORCE_CUBLAS=OFF", f"-DCUDAToolkit_ROOT={cuda_root}", "-DCMAKE_BUILD_TYPE=Release"]
        if cuda_archs:
            # Distribution build only: NCCL is a multi-GPU collective library that costs ~350 MB in the
            # published archive and does nothing on the single-GPU machines the prebuilt exists for.
            # llama.cpp keeps working without it (its own words: "performance for multiple CUDA GPUs will
            # be suboptimal"), and a multi-GPU owner builds from source, where NCCL stays on by default.
            cfg.append("-DGGML_CUDA_NCCL=OFF")
            lines.append("Distribution build: NCCL off (multi-GPU collectives; ~350 MB of download)")
        if cuda_host_cxx:
            cfg.append(f"-DCMAKE_CUDA_HOST_COMPILER={cuda_host_cxx}")
    else:
        cfg = [cmake, "-B", "build", "-G", flags["Generator"], "-DGGML_CUDA=OFF", "-DCMAKE_BUILD_TYPE=Release"]
    if portable:
        cfg.append("-DGGML_NATIVE=OFF")
        lines.append("Portable CPU code: x86-64 AVX2 baseline, not this machine's instruction set")

    print("\n".join(lines), file=sys.stderr)
    _run(cfg, cwd=SRC_LLAMA)
    _run([cmake, "--build", "build", "--config", "Release", "-j"], cwd=SRC_LLAMA)

    # Stage into bin/. Ninja is single-config on both OSes, so binaries land in build/bin. install_files
    # os.replace()s each file from a staging dir, so a running llama-server keeps its own (old) inode and
    # mmapped libs instead of having them rewritten underneath it; SONAME symlinks stay symlinks and an older
    # version of each shared lib is pruned, so bin/ does not grow on every rebuild.
    out_dir = build_dir / "bin"
    entries = {f.name: f for f in out_dir.glob("*") if f.is_file() or f.is_symlink()}
    if flags["StageDlls"]:  # pragma: no cover — Windows CUDA DLLs
        for dll in (f"cublas64_{cuda_major}.dll", f"cublasLt64_{cuda_major}.dll", f"cudart64_{cuda_major}.dll"):
            srcdll = Path(cuda_root) / "bin" / dll
            if srcdll.exists():
                entries[dll] = srcdll
    if exe not in entries:
        raise RuntimeError(f"{exe} missing from build output ({out_dir}); aborting the install into bin/")
    osenv.install_files(entries, BIN)
    # Record the tier bin/ was built at so update/diagnose/status can notice a GPU box on a CPU engine.
    # Marker path derives from BIN (this module's, which tests patch), so the write stays inside that tree.
    osenv.write_build_tier_marker(tier=("gpu" if flags["Cuda"] else "cpu"),
                                  arch=(arch if flags["Cuda"] else 0),
                                  cuda=(cuda_major if flags["Cuda"] else None), source="source", bin_dir=BIN,
                                  commit=_git_head(SRC_LLAMA))
    return f"Built. llama-server at: {BIN / exe}"


# --- llama-swap: the pinned release binary, or a Go build from the submodule ------------------------

def _install_llama_swap_release() -> str:
    """Download the llama-swap release binary pinned in versions.lock (binaries.llama-swap) for this OS/arch,
    SHA-256 verify it, and install it into bin/. Returns a status line, or '' when no usable pin exists: no
    asset for this platform, an empty sha256 (never run an unverified binary), or a submodule that has moved
    past the commit the release was cut from. Raises on a download or verification failure."""
    import tarfile
    import tempfile
    import zipfile

    import osenv
    from bob import versions

    key = f"{osenv.os_name()}-{osenv.normalized_cpu_arch()}"
    pin = versions.pinned_binary("llama-swap", key)
    if not pin or not (pin.get("sha256") or "").strip():
        return ""
    pinned = versions.submodule_commits(REPO).get(pin.get("submodule") or "external/llama-swap")
    built = pin.get("builtFromCommit")
    if built and pinned and built != pinned:
        print(f"llama-swap release {pin.get('version')} was cut from {built[:8]} but this checkout pins "
              f"{pinned[:8]}; building from source instead.", file=sys.stderr)
        return ""
    exe = osenv.exe_name("llama-swap")
    tmp = Path(tempfile.mkdtemp(prefix="bob-llama-swap-"))
    try:
        url = pin["url"]
        print(f"Downloading llama-swap {pin.get('version')} ({key})...", file=sys.stderr)
        archive = osenv.download(url, tmp / Path(url).name, sha256=pin["sha256"], timeout=60, require_sha=True)
        out = tmp / "x"
        out.mkdir()
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as z:
                z.extractall(out)
        else:
            with tarfile.open(archive) as t:
                t.extractall(out, filter="data")
        found = next((p for p in out.rglob(exe) if p.is_file()), None)
        if found is None:
            raise RuntimeError(f"{exe} not found in {Path(url).name}")
        if osenv.os_name() != "windows":
            found.chmod(0o755)
        osenv.install_files({exe: found}, BIN, prune_libs=False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return f"Installed llama-swap {pin.get('version')} release binary -> {BIN / exe}"


def build_llama_swap(force: bool = False, from_source: bool = False) -> str:
    """Put llama-swap in bin/: the sha-verified release binary pinned in versions.lock by default (no Go
    needed), or a Go build of the external/llama-swap submodule with from_source, when this platform has no
    pinned asset, or when the pinned release no longer matches the submodule commit."""
    import osenv
    out = BIN / osenv.exe_name("llama-swap")
    if not force and out.exists():
        return f"{out.name} already present — skipping (use --force to reinstall)."
    if not from_source:
        try:
            done = _install_llama_swap_release()
            if done:
                return done
        except (RuntimeError, OSError, ValueError) as e:
            print(f"llama-swap release download failed ({e}); building from source.", file=sys.stderr)
    if not SRC_SWAP.exists():
        raise RuntimeError(f"llama-swap submodule not found at {SRC_SWAP}. Run: git submodule update --init --recursive")
    if not shutil.which("go"):
        raise RuntimeError("Go not found, and no pinned llama-swap release applies here. Install Go (pacman -S go "
                           "/ apt install golang-go / dnf install golang; scoop install go on Windows), or drop a "
                           "llama-swap release binary in bin/.")
    BIN.mkdir(parents=True, exist_ok=True)
    tmp = BIN / f".llama-swap-build-{__import__('os').getpid()}"
    tmp.mkdir(exist_ok=True)
    try:
        built = tmp / out.name
        _run(["go", "build", "-o", str(built), "."], cwd=SRC_SWAP)
        osenv.install_files({out.name: built}, BIN, prune_libs=False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return f"Built: {out}"


# --- fabric setup (Go build + ~/.config/fabric wiring) --------------------------------------------

# fabric's .env keys Bob owns. fabric's built-in "LiteLLM" vendor reads LITELLM_API_KEY/LITELLM_API_BASE_URL,
# so pointing fabric at Bob never touches the user's own OpenAI (or any other vendor) keys.
_FABRIC_VENDOR = "LiteLLM"
_FABRIC_MODEL = "coder"


def _read_env_file(path: Path) -> list:
    """The lines of a dotenv file ([] when absent), kept verbatim so a merge preserves comments/order."""
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []


def fabric_env_is_legacy(current: dict) -> bool:
    """True when fabric's .env still holds the OPENAI_* pair earlier Bob setups wrote (OPENAI_API_KEY=sk-local
    pointed at a localhost proxy) instead of the LiteLLM vendor keys."""
    return current.get("OPENAI_API_KEY") == "sk-local" and \
        current.get("OPENAI_API_BASE_URL", "").startswith("http://localhost:")


def merge_fabric_env(path: Path, port: int, key: str) -> list:
    """Merge Bob's settings into fabric's .env and return the keys it changed. Only Bob-owned keys are
    written (LITELLM_API_KEY, LITELLM_API_BASE_URL); DEFAULT_VENDOR/DEFAULT_MODEL are set only when unset or
    when they still hold what an earlier Bob setup wrote, so a user's chosen default vendor wins. The exact
    OPENAI_* pair earlier setups wrote (sk-local at localhost:<port>) is removed; any other OPENAI_* value is
    the user's and is kept. Every other line is preserved as-is. Written atomically, mode 0600 on POSIX."""
    import os
    import tempfile

    base = f"http://localhost:{port}/v1"
    lines = _read_env_file(path)
    current = {}
    for ln in lines:
        k, sep, v = ln.partition("=")
        if sep and not ln.lstrip().startswith("#"):
            current[k.strip()] = v.strip()

    legacy_openai = fabric_env_is_legacy(current)
    ours_default = current.get("DEFAULT_VENDOR") in (None, "", _FABRIC_VENDOR) or \
        (legacy_openai and current.get("DEFAULT_VENDOR") == "OpenAI")
    want = {"LITELLM_API_KEY": key, "LITELLM_API_BASE_URL": base}
    if ours_default:
        want["DEFAULT_VENDOR"] = _FABRIC_VENDOR
        if current.get("DEFAULT_MODEL") in (None, "", _FABRIC_MODEL) or legacy_openai:
            want["DEFAULT_MODEL"] = _FABRIC_MODEL
    drop = {"OPENAI_API_KEY", "OPENAI_API_BASE_URL"} if legacy_openai else set()

    out, seen, changed = [], set(), []
    for ln in lines:
        k = ln.partition("=")[0].strip()
        if "=" in ln and not ln.lstrip().startswith("#"):
            if k in drop:
                changed.append(f"-{k}")
                continue
            if k in want:
                seen.add(k)
                if current.get(k) != want[k]:
                    changed.append(k)
                out.append(f"{k}={want[k]}")
                continue
        out.append(ln)
    for k, v in want.items():
        if k not in seen:
            out.append(f"{k}={v}")
            changed.append(k)

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".env-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out) + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return changed


def setup_fabric(force: bool = False) -> str:
    """Opt-in fabric: build it (Go) -> bin/fabric once (again only with force), then point it at Bob by
    merging Bob's keys into ~/.config/fabric/.env (merge_fabric_env) and linking the patterns dir. The key is
    the LiteLLM master key (bob_core._litellm_key), the port the configured litellmPort (defaults.json)."""
    import osenv
    from bob_core import _litellm_key, _port

    out = osenv.bin_exe("fabric")
    lines = []
    if not (SRC_FABRIC / "go.mod").exists():
        lines.append("Initialising external/fabric submodule...")
        _run(["git", "-C", str(REPO), "submodule", "update", "--init", "--depth=1", "external/fabric"])
    if force or not out.exists():
        if not shutil.which("go"):
            raise RuntimeError("Go not found: fabric is built from source. Install Go (pacman -S go / apt install "
                               "golang-go / dnf install golang; scoop install go on Windows), then re-run "
                               "bob fabric-setup.")
        BIN.mkdir(parents=True, exist_ok=True)
        lines.append("Building fabric...")
        _run(["go", "build", "-o", str(out), "./cmd/fabric/"], cwd=SRC_FABRIC)
        lines.append(f"  -> {out}")
    else:
        lines.append(f"{out} already built, skipping (pass --force to rebuild).")

    port = _port(_cfg, "litellmPort")
    fabric_dir = osenv.home_config_dir("fabric")
    env_path = fabric_dir / ".env"
    changed = merge_fabric_env(env_path, port, _litellm_key(_cfg))
    lines.append(f"Configured: {_FABRIC_VENDOR} vendor @ http://localhost:{port}/v1 in {env_path}"
                 + (f" (updated: {', '.join(changed)})" if changed else " (already current)"))

    link = fabric_dir / "patterns"
    target = SRC_FABRIC / "data" / "patterns"
    if not link.exists() and not link.is_symlink():
        try:
            link.symlink_to(target, target_is_directory=True)
            lines.append(f"Linked patterns: {link} -> {target}")
        except OSError:
            if target.exists():
                shutil.copytree(target, link)
                lines.append(f"Copied patterns to {link}")
    else:
        lines.append(f"{link} already exists, left as-is.")
    return "\n".join(lines)


# --- update (release-aware, cross-platform, with build rollback) ------------------------------

def _git_head(path: Path) -> str:
    try:
        r = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _short(sha: str) -> str:
    return sha[:8] if sha else "(none)"


def _verify_binary(exe: Path) -> bool:
    try:
        return subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _reinstall_venv() -> None:
    """Ensure the runtime venv matches the (possibly updated) requirements lock, via the shared
    osenv.new_bob_venv provisioner (the cold-start kernel and `update` share one venv path).
    Best-effort: a reinstall failure warns but doesn't abort the update."""
    import osenv
    print("Ensuring the Python runtime venv matches the lock...", file=sys.stderr)
    try:
        osenv.new_bob_venv("venv-litellm", "litellm-requirements", quiet=True)
    except RuntimeError as e:
        print(f"  (venv reinstall skipped: {e})", file=sys.stderr)
    # aider is opt-in: keep its venv on the lock only where it was installed (bob aider-setup).
    if osenv.venv_exe("venv-aider", "python").exists():
        try:
            osenv.new_bob_venv("venv-aider", "aider-requirements", quiet=True)
        except RuntimeError as e:
            print(f"  (venv-aider reinstall skipped: {e})", file=sys.stderr)


def _restart_running_endpoint() -> None:
    """Restart the endpoint if the update owns it, so one `bob update` is the whole move. A stack that kept
    serving through the update is still running the pre-update binaries, and still on the generated config
    from before the pull: llama-swap.yaml is regenerated by the stack bring-up (stack._ensure_configs), so a
    models.json change only reaches the running server on a start.

    Owns means the background start wrote a pidfile this stack can see (stack.endpoint_tracked_pid). A
    foreground `bob serve` writes none: it belongs to a terminal session the update does not own, and
    restarting it would kill that terminal's process and silently move the endpoint into the background. So
    an untracked endpoint is reported, not restarted, and the operator finishes the move themselves. Same
    branch covers an orphan from a crashed start. A stack that was already down stays down. Best-effort: the
    update is verified by the time this runs, so a restart hiccup only warns."""
    try:
        import stack
        stack.configure(_cfg)
        if not any(r["core"] and r["up"] for r in stack.service_snapshot(_cfg)):
            return
        if stack.endpoint_tracked_pid() is None:
            print("The endpoint is running but untracked (a foreground `bob serve`, or an orphan from a "
                  "crashed start), so this update left it alone. It is still on the pre-update build and "
                  "config: Ctrl+C that terminal and run `bob serve` again, or `bob restart` to bring the "
                  "endpoint up in the background.", file=sys.stderr)
            return
        print("Restarting the endpoint onto the updated build and config...", file=sys.stderr)
        print(stack.stack_restart(_cfg), file=sys.stderr)
    except Exception as e:  # noqa: BLE001 — advisory; never fail a verified update over a restart
        print(f"endpoint restart skipped ({e}); run `bob restart` to pick up the update.", file=sys.stderr)


def _prune_orphan_models() -> None:
    """After an update, offer to delete models/*.gguf that versions.lock no longer references (e.g. a coder a
    release dropped) to reclaim disk. Opt-in and TTY-only: it lists the orphans and asks before deleting;
    in a non-interactive/agent context it lists them and skips (never blocks on stdin, never deletes
    without a yes). Keeps referenced GGUFs and their mmproj sidecars, and only touches top-level *.gguf, so
    the faster-whisper CT2 model dir is left alone. Guarded: skips the
    prune entirely while any current-profile model is still missing (e.g. the new coder failed to
    download), so it can never strip a role down to no model."""
    import json

    models_dir = REPO / "models"
    lock_path = REPO / "versions.lock"
    if not models_dir.exists() or not lock_path.exists():
        return
    try:
        manifest = json.loads(lock_path.read_text(encoding="utf-8")).get("models", {})
    except (OSError, ValueError):
        return
    referenced = set(manifest) | {m["mmproj"] for m in manifest.values() if m.get("mmproj")}
    orphans = sorted(p for p in models_dir.glob("*.gguf") if p.name not in referenced)
    if not orphans:
        return

    # Safety: only reconcile once disk holds the new set. If the active profile's models aren't all
    # present yet, leave everything in place (deleting the old coder before the new one lands would
    # leave that role with nothing to serve).
    try:
        import provision
        _, current = provision.resolve_fetch_set(None)
        pending = [m["gguf"] for m in current if not (models_dir / m["gguf"]).exists()]
    except Exception:  # noqa: BLE001 — if we can't confirm the current set, don't risk a prune
        pending = ["?"]
    if pending:
        print("Prune skipped — some current-profile models aren't present yet (run `bob fetch`); "
              "old files left in place.", file=sys.stderr)
        return

    total_gb = sum(p.stat().st_size for p in orphans) / 1e9
    print(f"\n{len(orphans)} model file(s) are no longer referenced by versions.lock "
          f"({total_gb:.1f} GB total):", file=sys.stderr)
    for p in orphans:
        print(f"  {p.name}  ({p.stat().st_size / 1e9:.1f} GB)", file=sys.stderr)
    if not sys.stdin.isatty():
        print("Prune skipped (non-interactive). Delete them yourself, or re-run `bob update` in a "
              "terminal to be prompted.", file=sys.stderr)
        return
    try:
        ans = input("Delete these old model files to reclaim space? [y/N] ").strip().lower()
    except EOFError:
        ans = "n"
    if ans not in ("y", "yes"):
        print("Kept the old model files.", file=sys.stderr)
        return
    freed = 0
    for p in orphans:
        try:
            sz = p.stat().st_size
            p.unlink()
            freed += sz
        except OSError as e:  # noqa: PERF203 — report the one that failed, keep pruning the rest
            print(f"  could not delete {p.name}: {e}", file=sys.stderr)
    print(f"Pruned {freed / 1e9:.1f} GB of old models.", file=sys.stderr)


def _latest_release_tag() -> str:
    """The newest v* release tag by version order, or '' if there are none. Powers `--channel stable`.
    The tag listing itself lives in the lifecycle seam (shared with the prebuilt-manifest lookup)."""
    from bob import lifecycle
    return lifecycle._latest_release_tag() or ""


def _pending_rebuild_path():
    """Where update_stack records components whose rebuild is still owed. It lives under the data dir (NOT
    bin/), so a bin/ rollback can't erase it — that survival is the whole point."""
    import osenv
    return osenv.data_dir() / "update-pending.json"


def _read_pending_rebuild() -> list:
    """Component names a prior `bob update` advanced the tree for but did not finish rebuilding (empty when
    none). See update_stack: after a mid-rebuild failure the tree/venv are already on the new revisions while
    bin/ was rolled back, so git shows nothing 'moved' on the next run — this marker is what makes the re-run
    actually rebuild instead of falsely reporting 'no rebuild needed' and stranding a stale engine."""
    import json
    try:
        data = json.loads(_pending_rebuild_path().read_text(encoding="utf-8"))
        return [c for c in data if isinstance(c, str)] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _write_pending_rebuild(names) -> None:
    import json
    p = _pending_rebuild_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(sorted(set(names))), encoding="utf-8")
    except OSError:
        pass  # advisory only; a missing marker just means the next update re-derives from git


def _clear_pending_rebuild() -> None:
    _pending_rebuild_path().unlink(missing_ok=True)


def _on_branch() -> bool:
    """True if HEAD is on a branch (a dev on main / the latest channel), False on a detached checkout (a
    stable user sitting on a release tag, where `git pull` has no upstream to fast-forward)."""
    try:
        return subprocess.run(["git", "-C", str(REPO), "symbolic-ref", "-q", "HEAD"],
                              capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _tracking_branch() -> str:
    """The branch the 'latest' channel tracks: origin's default branch (usually 'main'), or 'main' as a
    fallback. Used when a user explicitly switches from a detached release tag back to the latest channel."""
    try:
        r = subprocess.run(["git", "-C", str(REPO), "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD"],
                           capture_output=True, text=True, timeout=10)
        ref = r.stdout.strip()
        if ref.startswith("origin/"):
            return ref[len("origin/"):]
    except (OSError, subprocess.SubprocessError):
        pass
    return "main"


def _head_is_release_tag() -> bool:
    """True when HEAD is exactly a v* release tag (a 'stable' checkout). Lets the channel be INFERRED from the
    git state: a fresh install that checked out a tag tracks stable; a dev on a branch tracks latest. No
    separate persisted setting to drift from the actual checkout."""
    from bob import lifecycle
    return lifecycle._current_release_tag() is not None


def resolve_update_channel(explicit: str = None) -> str:
    """The effective release channel: 'stable' (track v* release tags, which carry the tested prebuilt engines)
    or 'latest' (track the branch, i.e. main, source-built bleeding edge). An explicit choice wins; otherwise
    it is inferred from the checkout (a release tag -> stable, a branch -> latest), so it never disagrees with
    the git state and needs no separate persisted flag."""
    if explicit in ("stable", "latest"):
        return explicit
    return "stable" if _head_is_release_tag() else "latest"


def _stable_target_tag() -> str:
    """The tag `--channel stable` should move to, or '' to stay put. The newest v* release whose engines.json
    is actually published (so a stable user is never moved onto a still-publishing tag and forced into a source
    build), UNLESS HEAD already contains it (an ancestor) — never downgrade a checkout already at/ahead. Falls
    back to the newest tag if the readiness probe is unavailable (offline / no origin)."""
    try:
        from bob import lifecycle
        tag = lifecycle.latest_ready_release_tag() or _latest_release_tag()
    except Exception:  # noqa: BLE001 — readiness probe is best-effort; never block an update on it
        tag = _latest_release_tag()
    if not tag:
        return ""
    try:
        rc = subprocess.run(["git", "-C", str(REPO), "merge-base", "--is-ancestor", tag, "HEAD"],
                            capture_output=True, timeout=10).returncode
        return "" if rc == 0 else tag   # rc==0: tag is an ancestor of HEAD (already at/ahead) -> stay
    except (OSError, subprocess.SubprocessError):
        return tag


def update_stack(tag: str = None, from_source: bool = False, channel: str = None,
                 restart: bool = True) -> int:
    """Release-aware update with rollback: fetch/checkout, submodule sync, venv reinstall, then rebuild EVERY
    compiled submodule that actually moved (the llama-server rebuild goes through the prebuilt-first lifecycle
    seam, so a release update is a fast driver-only binary swap) under one bin/ snapshot with per-binary verify
    + rollback on failure, fetch newly-added models, offer to prune dropped ones, provision voice, then
    doctor. Channel (explicit, else inferred from the checkout) selects what to move to when no explicit tag:
    'stable' = the latest v* release tag (which carries the tested prebuilt engines), 'latest' = fast-forward
    the current branch (source-built bleeding edge). from_source forces a source engine build. Finally restarts
    a running endpoint (restart=False to leave it alone) so one `bob update` is the whole move: an endpoint that
    kept serving through the update is still running the pre-update binaries and the pre-update generated
    config. Returns 0 on success, 1 on a handled failure. CLI-only + long."""
    import osenv
    from bob import lifecycle

    if channel is not None and channel not in ("stable", "latest"):
        print(f"unknown --channel '{channel}'; valid values are 'stable' or 'latest'. Using the channel "
              "inferred from the checkout instead.", file=sys.stderr)
    explicit_channel = channel in ("stable", "latest")
    channel = resolve_update_channel(channel)
    on_tag = _head_is_release_tag()

    # Fetch FIRST, so tag selection below sees newly-published release tags (fetching after would make a
    # stable user miss a fresh release until their next run). Best-effort: offline / no origin / a non-github
    # remote must not hard-fail — an already-current box is then a clean no-op, and a genuinely-needed newer
    # tag simply isn't found (nothing to move to).
    print("Fetching updates...", file=sys.stderr)
    try:
        _run(["git", "-C", str(REPO), "fetch", "--tags", "--quiet"])
    except RuntimeError as e:
        print(f"  (fetch skipped: {e}; proceeding with the local git state)", file=sys.stderr)

    # Channel transitions actually move the checkout. Without this an explicit --channel was a near no-op:
    # 'latest' on a detached tag did nothing, and 'stable' on a branch just fast-forwarded the branch.
    switch_branch = None
    if not tag and channel == "stable":
        if explicit_channel and not on_tag:
            # Switching latest -> stable: jump to the newest release tag (a deliberate channel change, not a
            # downgrade, so the no-downgrade guard that keeps a stable user put does not apply here).
            tag = _latest_release_tag()
            print(f"channel 'stable' -> switching to release {tag or '(none published)'}", file=sys.stderr)
        else:
            tag = _stable_target_tag()   # staying on stable: newest tag, or '' if already at/ahead (no downgrade)
            if tag:
                print(f"channel 'stable' -> moving to release {tag}", file=sys.stderr)
            else:
                print("channel 'stable': already at or ahead of the latest release; fast-forwarding.",
                      file=sys.stderr)
    elif not tag and channel == "latest" and explicit_channel and on_tag:
        # Switching stable -> latest: leave the detached release tag for the tracking branch (bleeding edge).
        switch_branch = _tracking_branch()
        print(f"channel 'latest' -> switching to branch '{switch_branch}'", file=sys.stderr)

    # The tier decision is single-sourced through lifecycle.resolve_build_tier (shared with setup + `bob
    # build`), never re-derived from hardware here. This provisional read is cheap (self_heal=False installs
    # nothing); it's refined with a self-healing, warn-policy decision below only if a CUDA component moved.
    cpu = lifecycle.resolve_build_tier(self_heal=False)["tier"] == "cpu"
    # llama-server's build tier, late-bound (the lambda reads it at call time; it is refined below once we know
    # whether llama.cpp actually moved). llama-server is prebuilt-first: a driver-only GPU prebuilt needs no
    # toolkit, so a missing toolkit must NOT flip it to CPU while a GPU prebuilt is available.
    cpu_llama = cpu
    # (name, source dir, produced binary, verify-by-running-`--version`, rebuild fn). Every native
    # component the update can rebuild; a submodule is rebuilt only when its committed commit moved, so a
    # code-only update stays a no-op and a llama-swap/fabric bump no longer leaves a stale binary behind.
    # The llama-server rebuild goes through the single lifecycle seam (prebuilt-first, source fallback), so an
    # update is a fast driver-only binary swap when a prebuilt exists and identical to setup otherwise.
    # self_heal=False so ensure_engine never re-blocks; on_block='warn' is belt-and-braces.
    components = [
        ("llama.cpp",   SRC_LLAMA,   "llama-server",  True,
         lambda: lifecycle.ensure_engine(cpu=cpu_llama, from_source=from_source, force=True, on_block="warn",
                                         self_heal=False, config=_cfg)["detail"]),
        ("llama-swap",  SRC_SWAP,    "llama-swap",    True,
         lambda: build_llama_swap(force=True, from_source=from_source)),
        ("fabric",      SRC_FABRIC,  "fabric",        True,  lambda: setup_fabric(force=True)),
    ]
    # fabric is opt-in: rebuild it on a submodule move only where it was installed (bob fabric-setup).
    if not osenv.bin_exe("fabric").exists():
        components = [c for c in components if c[0] != "fabric"]
    before = {name: _git_head(src) for name, src, _, _, _ in components}

    if tag:
        print(f"Checking out release '{tag}'...", file=sys.stderr)
        _run(["git", "-C", str(REPO), "checkout", tag])
    elif switch_branch:
        _run(["git", "-C", str(REPO), "checkout", switch_branch])
        _run(["git", "-C", str(REPO), "pull", "--ff-only"])
    else:
        # Stable users sit on a DETACHED HEAD at a release tag, where `git pull` has no upstream. Only
        # fast-forward when actually on a branch (devs on main / the latest channel); otherwise there is
        # nothing newer to move to, so it's a clean no-op rather than a pull error.
        if _on_branch():
            print("Fast-forwarding the current branch...", file=sys.stderr)
            _run(["git", "-C", str(REPO), "pull", "--ff-only"])
        else:
            print("On a detached release checkout with nothing newer — already up to date.", file=sys.stderr)
    print("Syncing submodules to the pinned commits...", file=sys.stderr)
    _run(["git", "-C", str(REPO), "submodule", "update", "--init", "--recursive"])

    _reinstall_venv()

    # A component is rebuilt when its commit moved OR when a prior update left its rebuild owed (the tree
    # advanced but bin/ was rolled back after a build failure). The second set is what makes a re-run finish
    # the move instead of seeing an unchanged tree and skipping — the failure the marker exists to heal.
    owed = set(_read_pending_rebuild())
    moved = [c for c in components if before[c[0]] != _git_head(c[1]) or c[0] in owed]
    if not moved:
        print("Submodules unchanged, no rebuild needed.", file=sys.stderr)
        _clear_pending_rebuild()
    else:
        summary = ", ".join(f"{n} {_short(before[n])} to {_short(_git_head(s))}" for n, s, _, _, _ in moved)
        # Single tier decision (shared with setup + `bob build`), self-healing the toolkit on a mutable
        # distro. on_block='warn' keeps a running box alive: a GPU box that lost its toolkit (e.g. an atomic
        # host with read-only /usr) does NOT hard-fail the update. It warns loudly, records CPU in the tier
        # marker, and rebuilds CPU so the update completes, while `bob diagnose` keeps flagging the idle GPU.
        # The rebuild lambda reads cpu_llama at call time, so refining it here re-tiers the rebuild.
        if any(n == "llama.cpp" for n, *_ in moved):
            decision = lifecycle.apply_block_policy(lifecycle.resolve_build_tier(), on_block="warn")
            # llama-server: a GPU prebuilt makes the toolkit unnecessary, so a toolkit-driven CPU downgrade
            # must NOT force llama-server to CPU when a matching GPU prebuilt is available. Only follow the
            # downgrade when there is genuinely no GPU prebuilt to fall back on (then it's a real source build).
            # --from-source ignores prebuilts, so the guard only applies to the default (prebuilt) path — else
            # a --from-source rebuild on a toolkit-less GPU box would take the GPU source path and crash
            # instead of doing the intended CPU fallback.
            keep_gpu_via_prebuilt = not from_source and lifecycle.prebuilt_available(cpu=False)
            cpu_llama = decision["tier"] == "cpu" and not keep_gpu_via_prebuilt
        print(f"Rebuilding moved submodules: {summary} (bin/ snapshotted for rollback)...", file=sys.stderr)
        # Record the owed rebuilds BEFORE touching bin/, so a crash or a rolled-back failure leaves a marker
        # that the next run honors. Cleared only once every rebuild verifies.
        _write_pending_rebuild(n for n, *_ in moved)
        bak = osenv.backup_build_output(BIN)
        ok = False
        try:
            for name, _src, binname, use_version, fn in moved:
                # Any failure counts, not just RuntimeError: a download can die with OSError / IncompleteRead
                # / a tarfile error, and every one of them must still reach the rollback below.
                try:
                    print(fn(), file=sys.stderr)
                except Exception as e:  # noqa: BLE001
                    print(f"  {name} build failed: {e}", file=sys.stderr)
                    break
                exe = osenv.bin_exe(binname)
                if not (exe.exists() and (_verify_binary(exe) if use_version else True)):
                    print(f"  {name}: {binname} missing or failed verification.", file=sys.stderr)
                    break
            else:
                ok = True
        finally:
            if not ok:
                print("Update verification failed, rolling back the build output.", file=sys.stderr)
                if osenv.restore_build_output(BIN, bak):
                    print("Rolled bin/ back to the previous build; the endpoint keeps running on it. The source "
                          "tree and venv were already advanced to the new revisions; the owed rebuild is "
                          "recorded, so re-running `bob update` once the build issue is resolved WILL finish the "
                          "move.", file=sys.stderr)
                elif bak is not None:
                    print(f"WARNING: could not fully restore bin/ from {bak}; the snapshot is kept there. Stop "
                          "the stack (bob stop) and re-run `bob update`.", file=sys.stderr)
        if not ok:
            return 1  # pending-rebuild marker intentionally kept so the re-run rebuilds
        osenv.remove_build_output_backup(BIN, bak)
        _clear_pending_rebuild()
        print("Rebuild verified.", file=sys.stderr)

    # No relock here: versions.lock is a tracked file that arrived with the checkout above, already pinning
    # exactly these revisions. Regenerating it on this machine would bake local state into it and dirty the tree,
    # which blocks the next update's checkout.

    # Pull any models a release just added to the active profile (e.g. the rerank model). Resume +
    # SHA256-verify; already-present GGUFs are skipped, so this only downloads what's genuinely new —
    # a code-only update stays a no-op here. Best-effort: a download hiccup must not fail the whole
    # update (the endpoint still runs; `bob fetch` retries; an absent optional model just loud-fails
    # to its fallback).
    print("Fetching any new models for the active profile...", file=sys.stderr)
    try:
        import provision
        provision.configure(_cfg)
        print(provision.fetch_models(), file=sys.stderr)
    except Exception as e:  # noqa: BLE001 — advisory; never fail the update over a model download
        print(f"model fetch skipped ({e}); run `bob fetch` to pull any new models.", file=sys.stderr)

    # Reconcile disk to the new lock: offer to reclaim space from models a release dropped (e.g. the old
    # coder a release replaced). Opt-in, guarded, never fatal.
    _prune_orphan_models()

    # Provision voice assets for the configured backend (STT model + piper voice + audio deps) so an
    # update leaves a fully working default, identical to a fresh `bob setup` (which runs setup_voice at
    # step 10). Idempotent (skip-present) and best-effort — voice is optional and must never fail update.
    if _cfg.get("voice", {}).get("enabled"):
        print("Provisioning voice assets...", file=sys.stderr)
        try:
            import provision
            provision.configure(_cfg)
            print(provision.setup_voice(smoke=False), file=sys.stderr)
        except Exception as e:  # noqa: BLE001 — voice is optional; never fail the update over it
            print(f"voice provisioning skipped ({e}); run `bob setup-voice`.", file=sys.stderr)

    if restart:
        _restart_running_endpoint()

    # Closing doctor is informational — never let it fail (or, via a non-RuntimeError, crash) a verified update.
    print("Running bob doctor...", file=sys.stderr)
    try:
        import health
        health.configure(_cfg)
        print(health.health_check(_cfg, doctor=True))
    except Exception as e:  # noqa: BLE001 — advisory; the update already succeeded
        print(f"doctor skipped ({e}); run `bob doctor`.", file=sys.stderr)
    return 0


# CLI-only module (long native builds): no agent tools. Declared empty so the auto-discovering
# tool_loader treats it as a valid tool module with zero tools rather than a missing-TOOL_DEFS error.
TOOL_DEFS: list = []
DISPATCH: dict = {}
