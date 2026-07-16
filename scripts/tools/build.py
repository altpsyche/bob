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

def build_llama(cpu: bool = False, arch: int = 0, force: bool = False, cuda_root: str = "",
                cuda_archs: str = "") -> str:
    """(Re)build llama.cpp -> bin/llama-server. Auto-detects arch + CUDA root (osenv) unless given; CPU
    build with cpu=True. Atomic bin/ swap; Windows stages CUDA runtime DLLs.

    cuda_archs (e.g. '75;80;89;120') builds a FAT distribution binary that runs on every listed NVIDIA gen,
    with NO local GPU required (only the CUDA toolkit) — the mode the CI publish job uses to produce the
    prebuilt asset. It implies a CUDA build and bypasses nvidia-smi arch detection."""
    import os
    import osenv

    if cuda_archs:
        cpu = False   # a distribution CUDA build is GPU-tier by definition
    exe = osenv.exe_name("llama-server")
    win = osenv.os_name() == "windows"
    flags = osenv.resolve_build_cmake_flags(cpu=cpu, arch=arch)

    if not force and (BIN / exe).exists():
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
        if cuda_host_cxx:
            cfg.append(f"-DCMAKE_CUDA_HOST_COMPILER={cuda_host_cxx}")
        if win:  # pragma: no cover — hosted Windows MSVC (VS 18) is newer than CUDA 12.8's supported range,
            cfg.append("-DCMAKE_CUDA_FLAGS=-allow-unsupported-compiler")  # so nvcc refuses it without this
    else:
        cfg = [cmake, "-B", "build", "-G", flags["Generator"], "-DGGML_CUDA=OFF", "-DCMAKE_BUILD_TYPE=Release"]

    print("\n".join(lines), file=sys.stderr)
    _run(cfg, cwd=SRC_LLAMA)
    _run([cmake, "--build", "build", "--config", "Release", "-j"], cwd=SRC_LLAMA)

    # Stage -> atomic swap into bin/. Ninja is single-config on both OSes, so binaries land in build/bin.
    out_dir = build_dir / "bin"
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
    # Record the tier bin/ was built at so update/diagnose/status can notice a GPU box on a CPU engine.
    # Marker path derives from BIN (this module's, which tests patch), so the write stays inside that tree.
    osenv.write_build_tier_marker(tier=("gpu" if flags["Cuda"] else "cpu"),
                                  arch=(arch if flags["Cuda"] else 0),
                                  cuda=(cuda_major if flags["Cuda"] else None), source="source", bin_dir=BIN)
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

    if win and not osenv.ensure_msvc_env():  # pragma: no cover — Windows only; Ninja needs cl.exe on PATH
        raise RuntimeError("MSVC toolchain not found. Install Visual Studio 2022 with the 'Desktop "
                           "development with C++' workload (./install_prereqs.bat), then re-run.")
    gen = "Ninja"
    gen_args = ["-G", gen, "-DCMAKE_BUILD_TYPE=Release"]
    cmake = _resolve_cmake(gen)

    cuda_args = []
    if not cpu_only:
        gpu = osenv.gpu_arch()
        arch = gpu["CudaArch"] if gpu else 120
        root = osenv.best_cuda_root(arch)
        if root:
            cuda_args = ["-DWHISPER_CUDA=ON", f"-DCMAKE_CUDA_ARCHITECTURES={arch}", f"-DCUDAToolkit_ROOT={root}"]
            nvcc = Path(root) / "bin" / osenv.exe_name("nvcc")
            cuda_args.append(f"-DCMAKE_CUDA_COMPILER={nvcc}")   # Ninja needs it explicitly (both OSes)
            if not win:
                host_cxx = osenv.cuda_host_compiler()
                if host_cxx:
                    cuda_args.append(f"-DCMAKE_CUDA_HOST_COMPILER={host_cxx}")
                osenv.assert_cuda_host_compiler_ok(nvcc, host_cxx)
            else:  # pragma: no cover — nvcc uses cl.exe (from ensure_msvc_env) as its host compiler
                import os
                os.environ["CUDA_PATH"] = root
                cuda_args.append("-DCMAKE_CUDA_FLAGS=-allow-unsupported-compiler")  # VS 18 newer than CUDA 12.8
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

    release_bin = build_dir / "bin"   # Ninja is single-config on both OSes
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


def _prune_orphan_models() -> None:
    """After relock, offer to delete models/*.gguf that versions.lock no longer references (e.g. a coder a
    release dropped) to reclaim disk. Opt-in and TTY-only: it lists the orphans and asks before deleting;
    in a non-interactive/agent context it lists them and skips (never blocks on stdin, never deletes
    without a yes). Keeps referenced GGUFs and their mmproj sidecars, and only touches top-level *.gguf, so
    the whisper.cpp fallback binary and the faster-whisper CT2 model dir are left alone. Guarded: skips the
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
    """The newest v* release tag by version order, or '' if there are none. Powers `--channel stable`."""
    try:
        r = subprocess.run(["git", "-C", str(REPO), "tag", "--list", "v*", "--sort=-v:refname"],
                           capture_output=True, text=True, timeout=10)
        tags = [t for t in r.stdout.splitlines() if t.strip()]
        return tags[0].strip() if tags else ""
    except (OSError, subprocess.SubprocessError):
        return ""


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
    try:
        r = subprocess.run(["git", "-C", str(REPO), "describe", "--exact-match", "--tags", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        return r.returncode == 0 and r.stdout.strip().startswith("v")
    except (OSError, subprocess.SubprocessError):
        return False


def resolve_update_channel(explicit: str = None) -> str:
    """The effective release channel: 'stable' (track v* release tags, which carry the tested prebuilt engines)
    or 'latest' (track the branch, i.e. main, source-built bleeding edge). An explicit choice wins; otherwise
    it is inferred from the checkout (a release tag -> stable, a branch -> latest), so it never disagrees with
    the git state and needs no separate persisted flag."""
    if explicit in ("stable", "latest"):
        return explicit
    return "stable" if _head_is_release_tag() else "latest"


def _stable_target_tag() -> str:
    """The tag `--channel stable` should move to, or '' to stay put. The newest v* tag, UNLESS HEAD already
    contains it (an ancestor) — never downgrade a checkout that is already at or ahead of the latest release."""
    tag = _latest_release_tag()
    if not tag:
        return ""
    try:
        rc = subprocess.run(["git", "-C", str(REPO), "merge-base", "--is-ancestor", tag, "HEAD"],
                            capture_output=True, timeout=10).returncode
        return "" if rc == 0 else tag   # rc==0: tag is an ancestor of HEAD (already at/ahead) -> stay
    except (OSError, subprocess.SubprocessError):
        return tag


def update_stack(tag: str = None, from_source: bool = False, channel: str = None) -> int:
    """Release-aware update with rollback: fetch/checkout, submodule sync, venv reinstall, then rebuild EVERY
    compiled submodule that actually moved (the llama-server rebuild goes through the prebuilt-first lifecycle
    seam, so a release update is a fast driver-only binary swap) under one bin/ snapshot with per-binary verify
    + rollback on failure, relock, fetch newly-added models, offer to prune dropped ones, provision voice, then
    doctor. Channel (explicit, else inferred from the checkout) selects what to move to when no explicit tag:
    'stable' = the latest v* release tag (which carries the tested prebuilt engines), 'latest' = fast-forward
    the current branch (source-built bleeding edge). from_source forces a source engine build. Returns 0 on
    success, 1 on a handled failure. CLI-only + long."""
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
    # Per-component build tier, late-bound (the lambdas read these at call time; they are refined below once we
    # know which submodules actually moved). llama-server is prebuilt-first: a driver-only GPU prebuilt needs
    # no toolkit, so llama-server keeps its own flag that a missing toolkit must NOT flip to CPU while a GPU
    # prebuilt is available. whisper.cpp has no prebuilt, so it follows the source-build tier decision.
    cpu_llama = cpu
    cpu_whisper = cpu
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
        ("whisper.cpp", SRC_WHISPER, "whisper-server", False, lambda: build_whisper(force=True, cpu_only=cpu_whisper)),
        ("llama-swap",  SRC_SWAP,    "llama-swap",    True,  lambda: build_llama_swap(force=True)),
        ("fabric",      SRC_FABRIC,  "fabric",        True,  lambda: setup_fabric(force=True)),
    ]
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
        # The rebuild lambdas read cpu_llama / cpu_whisper at call time, so refining them here re-tiers them.
        if any(n in ("llama.cpp", "whisper.cpp") for n, *_ in moved):
            decision = lifecycle.apply_block_policy(lifecycle.resolve_build_tier(), on_block="warn")
            # whisper.cpp is source-only, so it follows the (possibly CPU-downgraded) decision directly.
            cpu_whisper = decision["tier"] == "cpu"
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
                print("Rolled bin/ back to the previous build; the endpoint keeps running on it. The source "
                      "tree and venv were already advanced to the new revisions; the owed rebuild is recorded, "
                      "so re-running `bob update` once the build issue is resolved WILL finish the move.",
                      file=sys.stderr)
            return 1  # pending-rebuild marker intentionally kept so the re-run rebuilds
        osenv.remove_build_output_backup(BIN, bak)
        _clear_pending_rebuild()
        print("Rebuild verified.", file=sys.stderr)

    # Relock to the new revisions. Best-effort: the rebuild already verified, so a lock-write hiccup must not
    # fail (or crash) an otherwise-successful update — warn and let `bob lock` redo it.
    try:
        from bob import versions
        versions.write_lock()
    except Exception as e:  # noqa: BLE001 — advisory; never fail a verified update over relocking
        print(f"relock skipped ({e}); run `bob lock` to refresh versions.lock.", file=sys.stderr)

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
    # coder after the 1.2 refresh). Opt-in, guarded, never fatal.
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
