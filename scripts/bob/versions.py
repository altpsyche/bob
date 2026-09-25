"""The versions.lock reader/validator for the Python side.

versions.lock is a GENERATED neutral JSON lock (`bob lock`, scripts/bob/versions.py) pinning submodule
commits, the pinned upstream binaries Bob can download instead of compiling (llama-swap), and the model
manifest (repo -> revision -> sha256, incl. the CPU-tier GGUF). It is generated from existing single sources
(git gitlinks + LOCK_BINARIES + config/models.json + the committed shas); the lock is READ to verify model
checksums on fetch, to resolve the llama-swap release download, and to report reproducibility.

Mirrors bob_core.load_defaults(): fail loud with a clear message if the lock is missing rather than
resolving to None.
"""
import datetime
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent.parent  # scripts/bob/versions.py -> repo
LOCK_FILE = REPO / "versions.lock"
VERSION_FILE = REPO / "VERSION"
MANIFEST_FILE = REPO / "models" / "manifest.json"
CHANGELOG_FILE = REPO / "CHANGELOG.md"
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")

# The submodules Bob pins (all three in .gitmodules).
LOCK_SUBMODULES = ["external/llama.cpp", "external/llama-swap", "external/fabric"]

# Upstream release binaries Bob downloads (sha256-verified) so a default install needs no Go toolchain. Each
# entry names the submodule it mirrors and the commit the release was cut from: the download is used only while
# the checkout pins that same commit, so a submodule bump falls back to a source build rather than silently
# running a binary from a different revision. Asset keys are '<os>-<cpuArch>' (osenv.os_name /
# osenv.normalized_cpu_arch).
LOCK_BINARIES = {
    "llama-swap": {
        "version": "v255",
        "submodule": "external/llama-swap",
        "builtFromCommit": "7761aa13360ea379cb89366d07c2d08aa9f1ed10",
        "assets": {
            "linux-x86_64": {
                "url": "https://github.com/mostlygeek/llama-swap/releases/download/v255/llama-swap_255_linux_amd64.tar.gz",
                "sha256": "84aa0df0cf3e302a8591e39de347f64c0c7dce1c3a948df68723a82e1fb4f1d4"},
            "linux-arm64": {
                "url": "https://github.com/mostlygeek/llama-swap/releases/download/v255/llama-swap_255_linux_arm64.tar.gz",
                "sha256": "98686bc626e2d3df3b340b963fd4e4f4d3dd02dcd1bf31f0c777fb09e3053288"},
            "macos-x86_64": {
                "url": "https://github.com/mostlygeek/llama-swap/releases/download/v255/llama-swap_255_darwin_amd64.tar.gz",
                "sha256": "98383f95298919cd6a73fcb11dadc78d53754bfe5d3de519fdead112f336f707"},
            "macos-arm64": {
                "url": "https://github.com/mostlygeek/llama-swap/releases/download/v255/llama-swap_255_darwin_arm64.tar.gz",
                "sha256": "d11b4c733da1c64ffd1b955f64b6af92a8c66a443aeeafeef2e84d3bf1fbf531"},
            "windows-x86_64": {
                "url": "https://github.com/mostlygeek/llama-swap/releases/download/v255/llama-swap_255_windows_amd64.zip",
                "sha256": "14b40b2e11479af9a83dec3ac2bf7f82864e62099f012b171591b871b9f88aae"},
        },
    },
}


def load_lock(path: Optional[Path] = None) -> dict:
    """Load and parse versions.lock. Raises RuntimeError if missing (it is generated: run `bob lock`)."""
    path = path or LOCK_FILE
    if not path.exists():
        raise RuntimeError(
            f"versions.lock not found at {path} — it is generated; run: bob lock"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def pinned_binary(name: str, platform_key: str, lock: Optional[dict] = None) -> Optional[dict]:
    """The locked release asset for binary `name` on `platform_key` ('<os>-<cpuArch>'), merged with the entry's
    version/submodule/builtFromCommit, or None when the lock has no such asset (or no lock exists)."""
    try:
        lock = lock if lock is not None else load_lock()
    except (RuntimeError, OSError, ValueError):
        return None
    entry = (lock.get("binaries") or {}).get(name) or {}
    asset = (entry.get("assets") or {}).get(platform_key)
    if not asset:
        return None
    return {**{k: v for k, v in entry.items() if k != "assets"}, **asset}


def sha256_file(path, _chunk: int = 1 << 20) -> str:
    """Streaming SHA256 (lowercase hex) — GGUFs are multi-GB, so never read the whole file at once."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(_chunk), b""):
            h.update(block)
    return h.hexdigest().lower()


def verify_model(path, expected_sha: Optional[str]) -> bool:
    """True iff the file at `path` hashes to `expected_sha` (case-insensitive).

    A falsy `expected_sha` means the model is unpinned (e.g. the CPU GGUF before its first fetch) —
    there is nothing to verify against, so this returns True. A missing file returns False.
    """
    if not expected_sha:
        return True
    p = Path(path)
    if not p.exists():
        return False
    return sha256_file(p) == expected_sha.strip().lower()


def check_reproducibility(repo: Optional[Path] = None, lock: Optional[dict] = None) -> list:
    """Return a list of drift dicts {kind, name, expected, actual}; empty when reproducible.

    Compares the lock to what is actually installed: submodule checked-out HEADs (via git) and, for
    models that are present AND pinned, the on-disk SHA256. Unpinned or not-downloaded models are
    skipped — they are not drift. Used by tests and by `bob doctor`.
    """
    repo = repo or REPO
    lock = lock if lock is not None else load_lock()
    drift = []
    for sub, want in (lock.get("submodules") or {}).items():
        full = Path(repo) / sub
        if not want or not full.exists():
            continue
        try:
            head = subprocess.run(
                ["git", "-C", str(full), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip()
        except Exception:
            head = ""
        if head and head != want:
            drift.append({"kind": "submodule", "name": sub, "expected": want, "actual": head})
    for gguf, meta in (lock.get("models") or {}).items():
        want = (meta or {}).get("sha256")
        if not want:
            continue
        f = Path(repo) / "models" / gguf
        if not f.exists():
            continue
        actual = sha256_file(f)
        if actual != want.strip().lower():
            drift.append({"kind": "model", "name": gguf, "expected": want, "actual": actual})
    return drift


# --- writer + sync-gate --------------------------------------------------------------------------
# GENERATED, NEVER HAND-EDITED: every field derives from an existing single source (git gitlinks +
# config/models.json + models/manifest.json + LOCK_BINARIES), so the lock never
# drifts by hand. Regenerate with `bob lock`; `bob lock --check` (wired into the Python gate + CI) fails if the
# on-disk file drifts from those sources.

def bob_version() -> str:
    """Release identity from the VERSION file ('0.0.0' if absent)."""
    if VERSION_FILE.exists():
        return VERSION_FILE.read_text(encoding="utf-8").strip()
    return "0.0.0"


def submodule_commits(repo: Optional[Path] = None) -> dict:
    """Superproject gitlink commit per submodule via `git rev-parse HEAD:<path>` — the real pin, resolvable
    without the submodule checked out (CI core-suite checks out none)."""
    repo = repo or REPO
    out = {}
    for p in LOCK_SUBMODULES:
        try:
            r = subprocess.run(["git", "-C", str(repo), "rev-parse", f"HEAD:{p}"],
                               capture_output=True, text=True, timeout=10)
            out[p] = r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
        except (OSError, subprocess.SubprocessError):
            out[p] = None
    return out


def _tracked_models_config(repo: Path) -> dict:
    """config/models.json exactly as committed. The lock is a tracked file, so it is built from tracked
    sources only: the per-machine config/user.json overlay (a local profile or model override) must never
    make `bob lock --check` report STALE or leak into the committed lock."""
    return json.loads((Path(repo) / "config" / "models.json").read_text(encoding="utf-8"))


def lock_model_manifest(models_config: Optional[dict] = None, repo: Optional[Path] = None,
                        use_manifest: bool = True) -> dict:
    """Union of every gguf across all profiles, keyed by local filename, in (profile-sorted, role-sorted)
    order with the first occurrence winning. repo/path/revision/sizeGB come from the committed
    config/models.json (no user overlay); sha256
    from models/manifest.json when fetched, else the sha already in versions.lock (TOFU-then-lock — the lock
    is a committed source; the manifest is a gitignored per-fetch capture, absent in CI), else null.

    use_manifest=False ignores the gitignored per-machine manifest and takes shas only from the committed lock,
    making the result deterministic across machines (a clean checkout, a dev box that has fetched models, and
    CI all compute the same thing). The sync gate uses this so a real on-disk sha for a lock-null model can no
    longer report a false STALE; sha integrity is still enforced at fetch time by verify_model."""
    repo = repo or REPO
    cfg = models_config if models_config is not None else _tracked_models_config(repo)
    manifest = {}
    mf = (repo / "models" / "manifest.json")
    if use_manifest and mf.exists():
        try:
            manifest = json.loads(mf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {}
    locked = {}
    lf = repo / "versions.lock"
    if lf.exists():
        try:
            locked = (json.loads(lf.read_text(encoding="utf-8")).get("models")) or {}
        except (OSError, ValueError):
            locked = {}

    models = {}
    for prof_name in sorted(cfg.get("profiles", {})):
        prof = cfg["profiles"][prof_name]
        for role in sorted(k for k in prof if not k.startswith("_")):
            m = prof[role]
            gguf = m.get("gguf")
            if not gguf or gguf in models:
                continue
            man_sha = (manifest.get(gguf) or {}).get("sha256")
            lock_sha = (locked.get(gguf) or {}).get("sha256")
            sha = str(man_sha).lower() if man_sha else (str(lock_sha).lower() if lock_sha else None)
            entry = {
                "repo": m.get("repo"),
                "path": m.get("path"),
                "revision": m.get("revision", "main"),
                "sha256": sha,
                "sizeGB": m.get("sizeGB"),
            }
            if m.get("mmproj"):
                entry["mmproj"] = m["mmproj"]
            models[gguf] = entry
    return models


def build_lock_object(repo: Optional[Path] = None, models_config: Optional[dict] = None,
                      use_manifest: bool = True) -> dict:
    """The full lock object, keys in a deterministic order (matches New-VersionsLockObject). The lock is the
    SOURCE trust root (submodules + models); prebuilt engine binaries are distributed via a per-release
    manifest asset (see scripts/bob/lifecycle.py), not committed here, so the lock never churns as platforms
    or engines are added. use_manifest=False makes the model shas machine-independent (see lock_model_manifest)."""
    return {
        "lockVersion": 1,
        "release": bob_version(),
        "submodules": submodule_commits(repo),
        "binaries": json.loads(json.dumps(LOCK_BINARIES)),
        "models": lock_model_manifest(models_config, repo, use_manifest=use_manifest),
    }


def lock_text(repo: Optional[Path] = None, models_config: Optional[dict] = None,
              use_manifest: bool = True) -> str:
    """Canonical serialization used by BOTH the writer and the sync gate (no trailing newline), so a clean
    regenerate byte-matches the on-disk file. json.dumps(indent=2) — 2-space indent, 'key': value, null,
    float formatting."""
    return json.dumps(build_lock_object(repo, models_config, use_manifest=use_manifest), indent=2)


def write_lock(path: Optional[Path] = None, repo: Optional[Path] = None,
               use_manifest: bool = True) -> Path:
    """(Re)generate versions.lock from the single sources. Atomic write + trailing newline. use_manifest=False
    keeps the committed lock's model shas instead of adopting this machine's fetched shas (used by bob release
    so cutting a version never bakes a dev box's shas into the committed lock)."""
    path = path or LOCK_FILE
    tmp = path.with_suffix(f".{__import__('os').getpid()}.tmp")
    tmp.write_text(lock_text(repo, use_manifest=use_manifest) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def check_sync(path: Optional[Path] = None) -> int:
    """Is the on-disk versions.lock in sync with the sources it is generated from? 0 in sync, 1 stale/missing.
    Compares canonical text (the only writer uses the same canonical text). Regenerates with use_manifest=False
    so the check is deterministic across machines: a real on-disk sha for a lock-null model no longer trips a
    false STALE (that per-machine manifest capture is not a source of truth; verify_model enforces sha integrity
    at fetch time)."""
    import sys as _sys

    path = path or LOCK_FILE
    if not path.exists():
        print(f"versions.lock missing at {path} — run: bob lock", file=_sys.stderr)
        return 1
    want = lock_text(use_manifest=False).strip()
    have = path.read_text(encoding="utf-8").strip()
    if want != have:
        print("versions.lock is STALE (out of sync with submodules/models.json) — run: bob lock",
              file=_sys.stderr)
        return 1
    return 0


# --- release cutting (bob release) ----------------------------------------------------------------
# One command moves VERSION, the versions.lock `release` field, and CHANGELOG.md together so they cannot
# drift. Bumping VERSION alone leaves the lock's release stale -> check_sync STALE -> the gates fail; that
# drift forced a 1.2.1 re-cut. This never runs a manifest-baking `bob lock`: it regenerates the lock with
# use_manifest=False, so cutting a release on a dev box does not bake that machine's model shas.


def parse_semver(version: str) -> str:
    """Validate an X.Y.Z version string (patch line — no pre-release/build suffix) and return it stripped."""
    v = (version or "").strip().lstrip("v")
    if not _SEMVER_RE.match(v):
        raise ValueError(f"not a semantic version 'X.Y.Z': {version!r}")
    return v


def set_release(version: str, dry_run: bool = False) -> None:
    """Write the VERSION file and regenerate versions.lock (manifest-free) so its `release` field moves in
    lockstep. Manifest-free keeps the committed lock's model shas (no machine-sha bake) and produces exactly
    what check_sync recomputes, so `bob lock --check` passes immediately after."""
    v = parse_semver(version)
    if dry_run:
        return
    VERSION_FILE.write_text(v + "\n", encoding="utf-8")
    write_lock(use_manifest=False)


def cut_changelog(version: str, date: Optional[str] = None, path: Optional[Path] = None,
                  dry_run: bool = False) -> dict:
    """Move the `## [Unreleased]` body into a new `## [version] (date)` section, leaving a fresh empty
    Unreleased. Returns {"was_empty": bool, "text": str}. was_empty flags an Unreleased section with no
    content (the caller warns; a real release should have notes). Line-based so it never mangles the body.

    IDEMPOTENT: a changelog that already carries a `## [version]` section is returned untouched. The
    documented flow is cut -> review -> commit -> `bob release <v> --tag`, and that second call re-enters
    here; without this guard it cut a SECOND, empty `## [version]` heading above the real one.
    """
    v = parse_semver(version)
    date = date or datetime.date.today().isoformat()
    path = path or CHANGELOG_FILE
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines()

    if any(ln.startswith(f"## [{v}]") for ln in lines):
        return {"was_empty": False, "text": original, "already_cut": True}

    i = next((n for n, ln in enumerate(lines) if ln.strip() == "## [Unreleased]"), None)
    if i is None:
        raise RuntimeError(f"no '## [Unreleased]' heading in {path} — cannot cut a release section")
    j = next((n for n in range(i + 1, len(lines)) if lines[n].startswith("## [")), len(lines))
    body = lines[i + 1:j]
    was_empty = "".join(body).strip() == ""

    new_lines = lines[:i + 1] + [""] + [f"## [{v}] ({date})"] + body + lines[j:]
    text = "\n".join(new_lines) + ("\n" if original.endswith("\n") else "")
    if not dry_run:
        path.write_text(text, encoding="utf-8")
    return {"was_empty": was_empty, "text": text, "already_cut": False}


def create_release_tag(version: str, repo: Optional[Path] = None) -> str:
    """Create the annotated git tag v<version> at HEAD (does NOT push). Raises if the tag already exists so a
    shipped-good tag is never silently moved."""
    v = parse_semver(version)
    repo = repo or REPO
    tag = f"v{v}"
    existing = subprocess.run(["git", "-C", str(repo), "tag", "--list", tag],
                              capture_output=True, text=True)
    if existing.returncode == 0 and existing.stdout.strip():
        raise RuntimeError(f"tag {tag} already exists — refusing to move it (re-cut only a broken release)")
    r = subprocess.run(["git", "-C", str(repo), "tag", "-a", tag, "-m", f"Bob {v}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git tag {tag} failed: {r.stderr.strip()}")
    return tag


def cut_release(version: str, tag: bool = False, date: Optional[str] = None,
                dry_run: bool = False) -> dict:
    """Cut a release in one step: bump VERSION + versions.lock `release`, move the changelog's Unreleased
    section into a dated one, and (opt-in) create the git tag. No commit, no push — the working tree is left
    for review. Returns a summary dict."""
    v = parse_semver(version)
    date = date or datetime.date.today().isoformat()
    set_release(v, dry_run=dry_run)
    cl = cut_changelog(v, date=date, dry_run=dry_run)
    created_tag = None
    if tag and not dry_run:
        created_tag = create_release_tag(v)
    return {"version": v, "date": date, "changelog_was_empty": cl["was_empty"],
            "changelog_already_cut": cl.get("already_cut", False),
            "tag": created_tag, "dry_run": dry_run}


if __name__ == "__main__":
    # `python -m bob.versions [--check|--write]`:
    #   (no arg) print a short reproducibility summary vs the installed state (read-only)
    #   --check  the gate — exit 1 if the on-disk lock drifts from its sources (used by the Python gate + CI)
    #   --write  regenerate versions.lock from the sources (same as `bob lock`)
    import sys

    if "--check" in sys.argv[1:]:
        sys.exit(check_sync())
    if "--write" in sys.argv[1:]:
        p = write_lock()
        print(f"wrote {p}")
        sys.exit(0)
    try:
        lk = load_lock()
    except RuntimeError as e:
        print(e, file=sys.stderr)
        sys.exit(1)
    d = check_reproducibility(lock=lk)
    print(f"versions.lock release {lk.get('release')} — "
          f"{len(lk.get('submodules') or {})} submodules, {len(lk.get('models') or {})} models")
    if d:
        for item in d:
            print(f"  DRIFT {item['kind']} {item['name']}: locked {item['expected'][:12]} "
                  f"!= actual {item['actual'][:12]}")
        sys.exit(1)
    print("  reproducible (no drift vs installed state)")
