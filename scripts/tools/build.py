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
    """A cmake < 4.0 (llama.cpp rejects 4.x). Windows: PATH cmake if 3.x, else VS-bundled, else winget the
    pinned 3.31.7. Linux: osenv.linux_cmake3 (system 3.x or a cached Kitware pin) + require ninja."""
    import osenv
    import re
    if osenv.os_name() != "windows":
        cmake = osenv.linux_cmake3(REPO)
        if generator == "Ninja" and not shutil.which("ninja"):
            raise RuntimeError("Ninja not found. Install it: apt/dnf/pacman/zypper install ninja(-build).")
        return cmake
    # Windows  # pragma: no cover — exercised only on Windows
    path_cmake = shutil.which("cmake")
    if path_cmake:
        out = subprocess.run(["cmake", "--version"], capture_output=True, text=True)
        m = re.search(r"(\d+)\.(\d+)", out.stdout)
        if m and (int(m.group(1)), int(m.group(2))) < (4, 0):
            return "cmake"
        print("PATH cmake is 4.x — incompatible with llama.cpp; looking for VS-bundled cmake...", file=sys.stderr)
    vswhere = Path(r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe")
    if vswhere.exists():
        vs = subprocess.run([str(vswhere), "-latest", "-products", "*", "-requires",
                             "Microsoft.VisualStudio.Component.VC.CMake.Project", "-property", "installationPath"],
                            capture_output=True, text=True).stdout.strip()
        if vs:
            cand = Path(vs) / "Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin/cmake.exe"
            if cand.exists():
                return str(cand)
    print("Installing cmake 3.31.7 via winget...", file=sys.stderr)
    _run(["winget", "install", "Kitware.CMake", "--version", "3.31.7", "--silent",
          "--accept-package-agreements", "--accept-source-agreements"])
    cmake = shutil.which("cmake")
    if not cmake:
        raise RuntimeError("cmake still not found after install — open a new terminal and retry.")
    return cmake


# --- build llama.cpp (CUDA or CPU) ----------------------------------------------------------------

def build_llama(cpu: bool = False, arch: int = 0, force: bool = False, cuda_root: str = "") -> str:
    """(Re)build llama.cpp -> bin/llama-server. Auto-detects arch + CUDA root (osenv) unless given; CPU
    build with cpu=True. Atomic bin/ swap; Windows stages CUDA runtime DLLs."""
    import os
    import osenv

    exe = osenv.exe_name("llama-server")
    win = osenv.os_name() == "windows"
    flags = osenv.resolve_build_cmake_flags(cpu=cpu, arch=arch)

    if not force and (BIN / exe).exists():
        return f"{exe} already built — skipping (use --force to rebuild)."
    if not (SRC_LLAMA / "CMakeLists.txt").exists():
        raise RuntimeError(f"llama.cpp submodule not found at {SRC_LLAMA}. Run: git submodule update --init --recursive")

    cuda_host_cxx = None
    cuda_major = "12"
    lines = []
    if flags["Cuda"]:
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
                    raise RuntimeError("CUDA Toolkit >= 12.8 not found. Blackwell (sm_120) needs 12.8+ "
                                       "(12.8/12.9/13.x). Install it, pass cuda_root=..., or build --cpu.")
                raise RuntimeError(f"No compatible CUDA toolkit for sm_{arch}. Install CUDA 12.x, pass "
                                   "cuda_root=..., or build --cpu.")
        lines += [f"Architecture : sm_{arch}", f"CUDA toolkit : {cuda_root}"]
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

    if flags["Cuda"] and win:  # pragma: no cover
        cfg = [cmake, "-B", "build", "-G", "Visual Studio 17 2022", "-T", f"cuda={cuda_root}",
               "-DGGML_CUDA=ON", f"-DCMAKE_CUDA_ARCHITECTURES={arch}", "-DGGML_CUDA_FORCE_CUBLAS=OFF",
               f"-DCUDAToolkit_ROOT={cuda_root}"]
    elif flags["Cuda"]:
        nvcc = Path(cuda_root) / "bin" / osenv.exe_name("nvcc")
        cfg = [cmake, "-B", "build", "-G", flags["Generator"], "-DGGML_CUDA=ON",
               f"-DCMAKE_CUDA_COMPILER={nvcc}", f"-DCMAKE_CUDA_ARCHITECTURES={arch}",
               "-DGGML_CUDA_FORCE_CUBLAS=OFF", f"-DCUDAToolkit_ROOT={cuda_root}", "-DCMAKE_BUILD_TYPE=Release"]
        if cuda_host_cxx:
            cfg.append(f"-DCMAKE_CUDA_HOST_COMPILER={cuda_host_cxx}")
    elif win:  # pragma: no cover
        cfg = [cmake, "-B", "build", "-G", "Visual Studio 17 2022", "-DGGML_CUDA=OFF"]
    else:
        cfg = [cmake, "-B", "build", "-G", flags["Generator"], "-DGGML_CUDA=OFF", "-DCMAKE_BUILD_TYPE=Release"]

    print("\n".join(lines), file=sys.stderr)
    _run(cfg, cwd=SRC_LLAMA)
    _run([cmake, "--build", "build", "--config", "Release", "-j"], cwd=SRC_LLAMA)

    # Stage -> atomic swap into bin/. VS is multi-config (build/bin/Release); Ninja is single (build/bin).
    out_dir = build_dir / ("bin/Release" if win else "bin")
    tmp = BIN / "_build_tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    try:
        for f in out_dir.glob("*"):
            shutil.copy2(f, tmp / f.name)
        if flags["StageDlls"]:  # pragma: no cover — Windows CUDA DLLs
            for dll in (f"cublas64_{cuda_major}.dll", f"cublasLt64_{cuda_major}.dll", f"cudart64_{cuda_major}.dll"):
                srcdll = Path(cuda_root) / "bin" / dll
                if srcdll.exists():
                    shutil.copy2(srcdll, tmp / dll)
        if not (tmp / exe).exists():
            raise RuntimeError(f"{exe} missing from staged output — aborting swap")
        BIN.mkdir(parents=True, exist_ok=True)
        svr = BIN / exe
        bak = BIN / f"{exe}.bak"
        if svr.exists():
            svr.replace(bak)
        for f in tmp.glob("*"):
            shutil.copy2(f, BIN / f.name)
        bak.unlink(missing_ok=True)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    shutil.rmtree(tmp, ignore_errors=True)
    return f"Built. llama-server at: {BIN / exe}"


SRC_WHISPER = REPO / "external" / "whisper.cpp"


def build_whisper(force: bool = False, cpu_only: bool = False) -> str:
    """(Re)build whisper.cpp -> bin/whisper-server + whisper-cli (CUDA by default, CPU fallback). Shares the
    cmake resolution + CUDA seams with build_llama; skips shared libs already in bin/ (whisper + llama share
    GGML)."""
    import osenv

    win = osenv.os_name() == "windows"
    server = osenv.bin_exe("whisper-server")
    cli = osenv.bin_exe("whisper-cli")
    if not force and server.exists() and cli.exists():
        return f"{server.name} + {cli.name} already built — skipping (use --force to rebuild)."
    if not (SRC_WHISPER / "CMakeLists.txt").exists():
        raise RuntimeError(f"whisper.cpp submodule not found at {SRC_WHISPER}. Run: git submodule update --init --recursive")

    gen = "Visual Studio 17 2022" if win else "Ninja"
    gen_args = ["-G", gen] if win else ["-G", gen, "-DCMAKE_BUILD_TYPE=Release"]
    cmake = _resolve_cmake(gen)

    cuda_args = []
    if not cpu_only:
        gpu = osenv.gpu_arch()
        arch = gpu["CudaArch"] if gpu else 120
        root = osenv.best_cuda_root(arch)
        if root:
            cuda_args = ["-DWHISPER_CUDA=ON", f"-DCMAKE_CUDA_ARCHITECTURES={arch}", f"-DCUDAToolkit_ROOT={root}"]
            nvcc = Path(root) / "bin" / osenv.exe_name("nvcc")
            if not win:
                cuda_args.append(f"-DCMAKE_CUDA_COMPILER={nvcc}")
                host_cxx = osenv.cuda_host_compiler()
                if host_cxx:
                    cuda_args.append(f"-DCMAKE_CUDA_HOST_COMPILER={host_cxx}")
                osenv.assert_cuda_host_compiler_ok(nvcc, host_cxx)
            else:  # pragma: no cover
                import os
                os.environ["CUDA_PATH"] = root
            print(f"Building whisper.cpp (CUDA sm_{arch})...", file=sys.stderr)
        else:
            print("CUDA toolkit not found — falling back to CPU-only whisper build.", file=sys.stderr)
    if not cuda_args:
        cuda_args = ["-DWHISPER_CUDA=OFF"]

    build_dir = SRC_WHISPER / "build"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    _run([cmake, "-B", "build", *gen_args, *cuda_args, "-DWHISPER_BUILD_TESTS=OFF", "-DWHISPER_BUILD_EXAMPLES=ON"],
         cwd=SRC_WHISPER)
    _run([cmake, "--build", "build", "--config", "Release", "-j"], cwd=SRC_WHISPER)

    release_bin = build_dir / ("bin/Release" if win else "bin")
    if not release_bin.exists():
        found = next(SRC_WHISPER.glob(f"build/**/{server.name}"), None)
        if not found:
            raise RuntimeError(f"{server.name} not found in build output — build may have failed silently")
        release_bin = found.parent
    BIN.mkdir(parents=True, exist_ok=True)
    if not (release_bin / server.name).exists():
        raise RuntimeError(f"{server.name} missing from staged output — aborting")
    import re
    for f in release_bin.iterdir():
        dest = BIN / f.name
        is_shared = re.search(r"\.(dll|so)(\.\d+)*$", f.name)
        if is_shared and dest.exists():
            continue  # a compatible GGML lib from the llama build is already there (maybe loaded)
        try:
            shutil.copy2(f, dest)
        except OSError:
            if not is_shared:
                raise
    return f"Built. whisper-server at: {server}"


# --- build llama-swap (Go) ------------------------------------------------------------------------

def build_llama_swap(force: bool = False) -> str:
    """Build the llama-swap submodule (Go) -> bin/llama-swap."""
    import osenv
    out = osenv.bin_exe("llama-swap")
    if not force and out.exists():
        return f"{out.name} already built — skipping (use --force to rebuild)."
    if not SRC_SWAP.exists():
        raise RuntimeError(f"llama-swap submodule not found at {SRC_SWAP}. Run: git submodule update --init --recursive")
    if not shutil.which("go"):
        raise RuntimeError("Go not found. Install Go (pacman -S go / apt install golang-go / dnf install "
                           "golang; scoop install go on Windows), or drop a llama-swap release binary in bin/.")
    BIN.mkdir(parents=True, exist_ok=True)
    _run(["go", "build", "-o", str(out), "."], cwd=SRC_SWAP)
    return f"Built: {out}"


# --- fabric setup (Go build + ~/.config/fabric wiring) --------------------------------------------

def setup_fabric(force: bool = False) -> str:
    """Build fabric (Go) -> bin/fabric and wire ~/.config/fabric (.env + patterns symlink)."""
    import osenv
    from bob_core import _port

    out = osenv.bin_exe("fabric")
    lines = []
    if not (SRC_FABRIC / "go.mod").exists():
        lines.append("Initialising external/fabric submodule...")
        _run(["git", "-C", str(REPO), "submodule", "update", "--init", "--depth=1", "external/fabric"])
    if force or not out.exists():
        if not shutil.which("go"):
            raise RuntimeError("Go not found — install Go to build fabric.")
        BIN.mkdir(parents=True, exist_ok=True)
        lines.append("Building fabric...")
        _run(["go", "build", "-o", str(out), "./cmd/fabric/"], cwd=SRC_FABRIC)
        lines.append(f"  -> {out}")
    else:
        lines.append(f"{out} already built — skipping (pass --force to rebuild).")

    port = _port(_cfg, "litellmPort")
    fabric_dir = osenv.home_config_dir("fabric")
    fabric_dir.mkdir(parents=True, exist_ok=True)
    (fabric_dir / ".env").write_text(
        f"OPENAI_API_KEY=sk-local\nOPENAI_API_BASE_URL=http://localhost:{port}/v1\n"
        f"DEFAULT_VENDOR=OpenAI\nDEFAULT_MODEL=coder\n", encoding="utf-8")
    lines.append(f"Configured: coder @ http://localhost:{port}/v1")

    link = fabric_dir / "patterns"
    target = SRC_FABRIC / "data" / "patterns"
    if not link.exists():
        try:
            link.symlink_to(target, target_is_directory=True)
            lines.append(f"Linked patterns: {link} -> {target}")
        except OSError:
            if target.exists():
                shutil.copytree(target, link)
                lines.append(f"Copied patterns to {link}")
    else:
        lines.append("patterns link already exists — skipping.")
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


def update_stack(tag: str = None) -> int:
    """Release-aware update with rollback: fetch/checkout (default: ff the current branch; tag= a release),
    submodule sync, venv reinstall, then rebuild EVERY compiled submodule that actually moved (llama.cpp,
    whisper.cpp, llama-swap, fabric) under one bin/ snapshot with per-binary verify + rollback on failure,
    relock, fetch any newly-added models (resume + skip-present, so code-only updates download nothing),
    then doctor. Returns 0 on success, 1 on a handled failure. CLI-only + long."""
    import osenv

    cpu = osenv.gpu_info() is None
    # (name, source dir, produced binary, verify-by-running-`--version`, rebuild fn). Every native
    # component the update can rebuild; a submodule is rebuilt only when its committed commit moved, so a
    # code-only update stays a no-op and a llama-swap/fabric bump no longer leaves a stale binary behind.
    components = [
        ("llama.cpp",   SRC_LLAMA,   "llama-server",  True,  lambda: build_llama(force=True, cpu=cpu)),
        ("whisper.cpp", SRC_WHISPER, "whisper-server", False, lambda: build_whisper(force=True, cpu_only=cpu)),
        ("llama-swap",  SRC_SWAP,    "llama-swap",    True,  lambda: build_llama_swap(force=True)),
        ("fabric",      SRC_FABRIC,  "fabric",        True,  lambda: setup_fabric(force=True)),
    ]
    before = {name: _git_head(src) for name, src, _, _, _ in components}

    print("Fetching updates...", file=sys.stderr)
    _run(["git", "-C", str(REPO), "fetch", "--tags", "--quiet"])
    if tag:
        print(f"Checking out release '{tag}'...", file=sys.stderr)
        _run(["git", "-C", str(REPO), "checkout", tag])
    else:
        print("Fast-forwarding the current branch...", file=sys.stderr)
        _run(["git", "-C", str(REPO), "pull", "--ff-only"])
    print("Syncing submodules to the pinned commits...", file=sys.stderr)
    _run(["git", "-C", str(REPO), "submodule", "update", "--init", "--recursive"])

    _reinstall_venv()

    moved = [c for c in components if before[c[0]] != _git_head(c[1])]
    if not moved:
        print("Submodules unchanged, no rebuild needed.", file=sys.stderr)
    else:
        summary = ", ".join(f"{n} {_short(before[n])} to {_short(_git_head(s))}" for n, s, _, _, _ in moved)
        print(f"Rebuilding moved submodules: {summary} (bin/ snapshotted for rollback)...", file=sys.stderr)
        bak = osenv.backup_build_output(BIN)
        ok = True
        for name, _src, binname, use_version, fn in moved:
            try:
                print(fn(), file=sys.stderr)
            except RuntimeError as e:
                print(f"  {name} build failed: {e}", file=sys.stderr)
                ok = False
                break
            exe = osenv.bin_exe(binname)
            if not (exe.exists() and (_verify_binary(exe) if use_version else True)):
                print(f"  {name}: {binname} missing or failed verification.", file=sys.stderr)
                ok = False
                break
        if not ok:
            print("Update verification failed, rolling back the build output.", file=sys.stderr)
            if osenv.restore_build_output(BIN, bak):
                print("Rolled bin/ back to the previous build; the endpoint keeps running on it. The "
                      "source tree and venv were already advanced to the new revisions; re-run "
                      "`bob update` once the build issue is resolved to finish the move.", file=sys.stderr)
            return 1
        osenv.remove_build_output_backup(BIN, bak)
        print("Rebuild verified.", file=sys.stderr)

    from bob import versions
    versions.write_lock()

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

    print("Running bob doctor...", file=sys.stderr)
    import health
    health.configure(_cfg)
    print(health.health_check(_cfg, doctor=True))
    return 0


# CLI-only module (long native builds): no agent tools. Declared empty so the auto-discovering
# tool_loader treats it as a valid tool module with zero tools rather than a missing-TOOL_DEFS error.
TOOL_DEFS: list = []
DISPATCH: dict = {}
