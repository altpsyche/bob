"""The single install/update lifecycle seam — one place that decides the build tier and makes a working
inference engine present in bin/.

Before this module, four entry points each decided GPU-vs-CPU their own way (kernel bootstrap, `bob build`,
`bob update`, and build_llama's internal guard). That drift let a GPU box silently run CPU inference and let
`bob update` hard-fail where `bob setup` would have fallen back. Now every entry point delegates here, so the
tier is decided in exactly one place and it is structurally impossible for the paths to diverge. This mirrors
the SERVICES + ensure_inference single-source pattern in scripts/tools/stack.py.

Tier-0: stdlib + osenv only (plus lazy imports of the sibling Tier-0/tool modules), so the cold-start kernel
can call it under the system python BEFORE any venv exists.

  resolve_build_tier(cpu, self_heal) -> decision dict   # the ONE tier decision (self-heals the toolkit)
  apply_block_policy(decision, on_block)                 # the ONE stop-vs-warn policy for a blocked GPU box
  ensure_engine(cpu, from_source, force, on_block, cfg)  # decision -> build llama-server -> tier marker
"""
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import osenv  # noqa: E402


def _tools_on_path() -> None:
    tools = str(_SCRIPTS / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)


def resolve_build_tier(cpu: bool = False, self_heal: bool = True) -> dict:
    """THE single build-tier decision, shared by setup/bootstrap, `bob build`, and `bob update`. Never raises;
    callers decide what to do with a `blocked` result via apply_block_policy(). Returns:

        {tier: 'gpu'|'cpu', cuda_root: str|None, arch: int, blocked: bool, reason: str, remedy: str|None}

    Rules, in order:
      1. cpu=True (explicit consent)          -> tier=cpu                (never blocked)
      2. no NVIDIA GPU (gpu_info None)         -> tier=cpu                (nothing to accelerate)
      3. GPU + a reachable CUDA toolkit        -> tier=gpu                (self_heal installs it on a mutable distro)
      4. GPU + no toolkit + no consent         -> tier=gpu, blocked=True  (remedy = osenv.cuda_missing_message())

    Rule 4 is the whole fix: a GPU box with no toolkit and no --cpu is *blocked*, never a silent CPU build.
    (Phase 2 will additionally treat "a prebuilt engine exists" as satisfying rule 3, so a driver-only box is
    no longer blocked — the download layer plugs in here.)"""
    arch = (osenv.gpu_arch() or {}).get("CudaArch", 0)
    if cpu:
        return {"tier": "cpu", "cuda_root": None, "arch": arch, "blocked": False,
                "reason": "--cpu requested", "remedy": None}
    gpu = osenv.gpu_info()
    if gpu is None:
        return {"tier": "cpu", "cuda_root": None, "arch": 0, "blocked": False,
                "reason": "no NVIDIA GPU detected", "remedy": None}
    gen = gpu.get("Gen") or f"sm_{arch}"
    if self_heal:
        # ensure_cuda_toolkit probes, then installs the toolkit on a mutable distro and re-probes; returns a
        # root or None (an atomic host with read-only /usr, or an install that failed). Never raises.
        from bob import install_prereqs
        root = install_prereqs.ensure_cuda_toolkit(cpu=False)
    else:
        root = osenv.best_cuda_root(arch)
    if root:
        return {"tier": "gpu", "cuda_root": root, "arch": arch, "blocked": False,
                "reason": f"GPU {gen} (sm_{arch}) + CUDA toolkit {root}", "remedy": None}
    return {"tier": "gpu", "cuda_root": None, "arch": arch, "blocked": True,
            "reason": f"GPU {gen} (sm_{arch}) present but no CUDA toolkit found and no --cpu consent",
            "remedy": osenv.cuda_missing_message()}


def apply_block_policy(decision: dict, on_block: str = "stop") -> dict:
    """THE single stop-vs-warn policy for a `blocked` GPU box (GPU present, no toolkit, no consent). Returns
    the decision to actually build with.

      on_block='stop' (fresh setup / `bob build`): raise RuntimeError(remedy) — fail loud with the one-command
        route (a Fedora distrobox on atomic, ./install_prereqs.sh on a mutable distro), rather than silently
        shipping a CPU-tier build on a GPU box. `--cpu` is the consent escape hatch.
      on_block='warn' (`bob update`): keep a running box alive — print the remedy + the "your GPU is idle"
        warning and downgrade to a CPU build so the operation completes. The tier marker records cpu and
        `bob diagnose` keeps flagging the idle GPU, so the degradation is loud and persistent, never silent."""
    if not decision.get("blocked"):
        return decision
    if on_block == "stop":
        raise RuntimeError(decision["remedy"])
    print(f"WARNING: {decision['reason']}.", file=sys.stderr)
    print(decision["remedy"], file=sys.stderr)
    print("Building the CPU tier so the operation completes; your GPU stays idle until a CUDA toolkit (or a "
          "prebuilt engine) is available. `bob diagnose` will keep flagging this.", file=sys.stderr)
    return {**decision, "tier": "cpu", "cuda_root": None, "blocked": False,
            "reason": decision["reason"] + " -> CPU fallback"}


_COMPONENT_SUBMODULE = {"llama-server": "external/llama.cpp", "whisper-server": "external/whisper.cpp"}


def _pinned_submodule_commit(component: str):
    """The commit `component`'s submodule is pinned to in THIS checkout (the superproject gitlink), or None.
    The commit-match guard compares this to a prebuilt row's builtFromCommit so a prebuilt is only ever used
    when it is provably the same source a `--from-source` build would compile here."""
    import subprocess
    path = _COMPONENT_SUBMODULE.get(component)
    if not path:
        return None
    try:
        r = subprocess.run(["git", "-C", str(osenv.REPO), "rev-parse", f"HEAD:{path}"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
    except (OSError, subprocess.SubprocessError):
        return None


def _current_release_tag():
    """The v* release tag HEAD is exactly at (a 'stable' checkout), or None. The prebuilt manifest is
    published per release, so a checkout that isn't on a release tag (main / dev) has no manifest and builds
    from source."""
    import subprocess
    try:
        r = subprocess.run(["git", "-C", str(osenv.REPO), "describe", "--exact-match", "--tags", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        t = r.stdout.strip()
        return t if r.returncode == 0 and t.startswith("v") else None
    except (OSError, subprocess.SubprocessError):
        return None


def _repo_slug():
    """'owner/repo' from the origin remote, or None (used to build the release-asset URL)."""
    import re
    import subprocess
    try:
        url = subprocess.run(["git", "-C", str(osenv.REPO), "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else None


def _manifest_rows(raw: dict) -> dict:
    """Real engine rows from a manifest dict (drop the '_'-prefixed schema/doc keys)."""
    return {k: v for k, v in (raw or {}).items() if not k.startswith("_")}


def _load_engine_manifest() -> dict:
    """The engine manifest to resolve a prebuilt from. Resolution order:
      1. a local `config/engines.json` override (real rows only) — for dev/test/air-gapped pinning;
      2. else the `engines.json` published as a release asset for the tag this checkout is on;
      3. else {} (main / no tag / offline / fetch failure) -> ensure_engine builds from source.
    The manifest travels WITH the release rather than being committed back to the repo, so adding platforms,
    components, or channels never churns the repo. Rows are keyed '<component>-<os>-<arch>-<tier>'."""
    import json
    local = osenv.REPO / "config" / "engines.json"
    if local.exists():
        try:
            rows = _manifest_rows(json.loads(local.read_text(encoding="utf-8")))
            if rows:
                return rows
        except (OSError, ValueError):
            pass
    tag = _current_release_tag()
    slug = _repo_slug()
    if not tag or not slug:
        return {}
    import urllib.request
    url = f"https://github.com/{slug}/releases/download/{tag}/engines.json"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:  # noqa: S310 — release asset over https
            return _manifest_rows(json.loads(r.read().decode("utf-8")))
    except Exception:  # noqa: BLE001 — no manifest / offline / bad JSON -> source build
        return {}


def _select_engine_row(component: str, os_name: str, cpu_arch: str, tier: str):
    """The engine manifest row for this (component, os, arch, tier), or None (-> source build). The manifest
    is fetched from the release the checkout is on (see _load_engine_manifest), not the repo.

    Commit-match guard: a matching row is used ONLY when its builtFromCommit equals the commit this checkout
    pins the submodule to (when both are known). That makes a prebuilt provably the same llama.cpp version a
    source build would produce here, so the two paths can never silently diverge. If either commit can't be
    determined (git unavailable, or an unversioned row) the guard does not block."""
    engines = _load_engine_manifest()
    pinned = _pinned_submodule_commit(component)
    for row in engines.values():
        if not (row.get("component") == component and row.get("os") == os_name
                and row.get("cpuArch") == cpu_arch and row.get("tier") == tier):
            continue
        built = row.get("builtFromCommit")
        if built and pinned and built != pinned:
            print(f"prebuilt {component} skipped: built from {built[:8]} but this checkout pins {pinned[:8]} "
                  "(building from source to stay in sync).", file=sys.stderr)
            continue
        return row
    return None


def _binary_runs(exe) -> bool:
    """True if the staged binary actually executes here (runs `--version` without an OS/loader error). This is
    the safety net that makes prebuilt-for-all-distros honest: a binary built against a newer glibc than the
    host, or for the wrong ABI, fails to launch, and we fall back to a source build instead of leaving the user
    with an engine that won't start. A non-zero exit still counts as 'runs' (it loaded); only a launch failure
    (OSError) or timeout counts as broken."""
    import subprocess
    exe = Path(exe)
    if not exe.exists():
        return False
    try:
        subprocess.run([str(exe), "--version"], capture_output=True, timeout=30)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _install_prebuilt(row: dict, bin_dir) -> str:
    """Download the prebuilt engine archive in `row`, SHA-verify it against the lock (tamper-evident), extract,
    and stage every file (the binary + its bundled CUDA runtime libs) into bin/ so the result is driver-only.
    urllib/tarfile/zipfile (stdlib, pre-venv safe). Raises RuntimeError/OSError on any failure so ensure_engine
    can fall back to a source build."""
    import hashlib
    import shutil
    import tarfile
    import tempfile
    import urllib.request
    import zipfile

    url = row.get("url")
    if not url:
        raise RuntimeError("engine row has no url")
    bin_dir = Path(bin_dir)
    tmp = Path(tempfile.mkdtemp(prefix="bob-engine-"))
    try:
        archive = tmp / Path(url).name
        urllib.request.urlretrieve(url, archive)  # noqa: S310 — pinned release URL, SHA-verified below
        want = (row.get("sha256") or "").strip().lower()
        if want:
            h = hashlib.sha256()
            with open(archive, "rb") as f:
                for block in iter(lambda: f.read(1 << 20), b""):
                    h.update(block)
            got = h.hexdigest().lower()
            if got != want:
                raise RuntimeError(f"SHA256 mismatch for {url} (got {got[:12]}..., want {want[:12]}...)")
        extract = tmp / "x"
        extract.mkdir()
        if tarfile.is_tarfile(archive):
            with tarfile.open(archive) as t:
                t.extractall(extract, filter="data")  # noqa: S202 — our own SHA-verified release artifact
        elif zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as z:
                z.extractall(extract)
        else:
            raise RuntimeError(f"unrecognized archive format: {archive.name}")
        bin_dir.mkdir(parents=True, exist_ok=True)
        staged = 0
        for p in extract.rglob("*"):
            if p.is_file():
                shutil.copy2(p, bin_dir / p.name)
                staged += 1
        if staged == 0:
            raise RuntimeError("archive contained no files to stage")
        return f"Installed prebuilt {row.get('component')} ({row.get('tier')}): {staged} file(s) -> {bin_dir}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def ensure_engine(cpu: bool = False, from_source: bool = False, force: bool = False,
                  on_block: str = "stop", self_heal: bool = True, config: dict = None) -> dict:
    """THE single 'make a working inference engine (llama-server) present in bin/' operation. Every entry point
    (kernel setup/bootstrap, `bob build`, and via resolve_build_tier the `bob update` rebuild) routes its tier
    decision through here, so gpu-vs-cpu is chosen in one place and can never drift.

    Prebuilt-first (the outlier fix): if versions.lock carries a matching (component, os, arch, tier) engine
    row, download + SHA-verify + stage the driver-only binary (CUDA runtime libs bundled) so the user never
    compiles. Falls back to a source build when `from_source` is set, no row matches (or the manifest is
    empty), or the download/verify fails. Source-build stays the reproducibility ground truth.

    self_heal=False lets a caller that has ALREADY resolved+ensured the tier (e.g. `bob update`) reuse that
    decision without a second toolkit probe.

    Returns the decision dict augmented with {'source': 'source'|'prebuilt', 'detail': <status>}."""
    bin_dir = osenv.REPO / "bin"

    # Prebuilt-first, and CRUCIALLY before any toolkit probe: a prebuilt is driver-only, so it must NOT
    # trigger a CUDA-toolkit install (mutable) or a block (atomic) — those belong only to the source path.
    # The tier intent here is a cheap, side-effect-free read (self_heal=False); `blocked` is ignored because
    # a prebuilt needs no toolkit. Only when we actually fall back to source do we resolve+ensure+block.
    if not from_source:
        tier = resolve_build_tier(cpu=cpu, self_heal=False)["tier"]
        row = _select_engine_row("llama-server", osenv.os_name(), osenv.normalized_cpu_arch(), tier)
        if row:
            marker = osenv.build_tier_marker(bin_dir) or {}
            if (not force and osenv.bin_exe("llama-server").exists()
                    and marker.get("source") == "prebuilt" and marker.get("tier") == tier):
                return {"tier": tier, "source": "prebuilt", "blocked": False,
                        "reason": "prebuilt already present", "cuda_root": None, "arch": 0,
                        "detail": "llama-server prebuilt already present (use --force to reinstall)."}
            try:
                detail = _install_prebuilt(row, bin_dir)
                # Portability safety net: a prebuilt built against a newer glibc than this host won't launch.
                # Verify it actually runs; if not, drop it and fall through to a source build so no user is
                # ever left with a non-starting engine (this is what makes "works on all distros" honest).
                if not _binary_runs(osenv.bin_exe("llama-server")):
                    osenv.bin_exe("llama-server").unlink(missing_ok=True)
                    raise RuntimeError("prebuilt engine does not run on this system (e.g. glibc too old)")
                osenv.write_build_tier_marker(tier=tier, arch=0, cuda=row.get("cudaMajor"),
                                              source="prebuilt", bin_dir=bin_dir)
                return {"tier": tier, "source": "prebuilt", "blocked": False,
                        "reason": f"prebuilt {tier} engine", "cuda_root": None, "arch": 0, "detail": detail}
            except (RuntimeError, OSError) as e:
                print(f"prebuilt engine unavailable ({e}); building from source.", file=sys.stderr)

    # Source fallback. When self_heal=True (setup / `bob build`) ensure_engine OWNS the decision: resolve the
    # toolkit and apply the stop/warn block policy. When self_heal=False the CALLER (`bob update`) has already
    # resolved + ensured the tier and passed it in as `cpu`, so we trust that and must NOT re-block — a second
    # best_cuda_root probe here (which can miss a just-installed toolkit, or find none on CI) would wrongly
    # downgrade a GPU rebuild to CPU. That double-decision was the exact drift this seam exists to prevent.
    if self_heal:
        decision = apply_block_policy(resolve_build_tier(cpu=cpu, self_heal=True), on_block)
    else:
        decision = {**resolve_build_tier(cpu=cpu, self_heal=False), "blocked": False}
    is_cpu = decision["tier"] == "cpu"
    _tools_on_path()
    import build
    if config is not None:
        build.configure(config)
    # build_llama writes bin/.build-tier.json itself (the executor is the single marker writer), so every
    # source path — here, `bob update`'s rebuild, and a bare `bob build` — records the tier.
    detail = build.build_llama(cpu=is_cpu, arch=(0 if is_cpu else decision["arch"]),
                               cuda_root=(decision.get("cuda_root") or ""), force=force)
    return {**decision, "source": "source", "detail": detail}
