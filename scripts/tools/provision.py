"""Bob provisioning capabilities (ONE-D) — the download/provision verbs.

Functional grouping (D6): one module, several related capability fns, each reached three ways with no
duplicated logic — the agent tool (DISPATCH), the `bob <verb>` cli handler (scripts/bob/cli.py), and
`bob --run <cap>`. The cold-start KERNEL also calls these same fns directly (they must stay import-clean
under a bare system python — no `requests`, no venv-only deps; downloads shell `curl` per DD2).

Slice D1 ports `fetch` (scripts/fetch-models.ps1): download the active-profile GGUFs with resume
(`curl -C -`), verify each against versions.lock (pinned SHA256 -> loud-fail; unpinned -> TOFU + warn),
and record the SHA256 into models/manifest.json. mmproj (multimodal projector) rides the model's revision.
Model set + repos/paths/sizes come from the neutral registry (bob_models, config/models.json)."""
import shutil
import subprocess
import sys
from pathlib import Path

_cfg: dict = {}

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO / "scripts"
MODELS_DIR = REPO / "models"

MUTATING_TOOLS = {"fetch_models"}  # lock_check is read-only; `bob lock` (write) is CLI-only, not a tool


def configure(config: dict) -> None:
    global _cfg
    _cfg = config
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))


# --- model-set resolution (from the neutral registry) ---------------------------------------------

def resolve_fetch_set(profile=None):
    """(profile_name, [ {role, gguf, repo, path, sizeGB, mmproj} ]) for a profile, deduped by gguf
    (first role wins — a gguf shared across roles is one download). Mirrors Get-Models' role set."""
    import bob_models

    config = bob_models.load_models_config()
    name = bob_models.resolve_profile_name(profile, config)
    roles = bob_models.profile_roles(name, config)
    seen = set()
    models = []
    for role, spec in roles.items():
        gguf = spec.get("gguf")
        if not gguf or gguf in seen:
            continue
        seen.add(gguf)
        models.append({"role": role, "gguf": gguf, "repo": spec.get("repo"),
                       "path": spec.get("path"), "sizeGB": spec.get("sizeGB"),
                       "mmproj": spec.get("mmproj")})
    return name, models


# --- versions.lock coupling (best-effort — a missing lock falls back to 'main' + TOFU) -------------

def _load_lock():
    try:
        from bob.versions import load_lock
        return load_lock()
    except Exception:
        return None


def _model_revision(gguf: str, lock) -> str:
    """The pinned HF revision for a gguf from versions.lock; 'main' when unpinned/absent. Port of
    Get-ModelRevision."""
    if lock:
        meta = (lock.get("models") or {}).get(gguf) or {}
        if meta.get("revision"):
            return meta["revision"]
    return "main"


def _verify_download(file: Path, gguf: str, lock) -> str:
    """Hash the freshly-downloaded file and compare to the versions.lock pin. Pinned + mismatch -> delete
    the bad file and raise (loud-fail); pinned + match -> ok; unpinned -> TOFU + warn. Returns the
    computed lowercase hash so the caller records it without re-hashing a multi-GB file. Port of
    Confirm-Download."""
    from bob.versions import sha256_file

    sha = sha256_file(file)
    expected = ""
    if lock:
        meta = (lock.get("models") or {}).get(gguf) or {}
        expected = str(meta.get("sha256") or "").lower()
    if expected:
        if sha != expected:
            file.unlink(missing_ok=True)
            raise RuntimeError(
                f"Checksum mismatch for {gguf} — versions.lock pins {expected} but the download is {sha}. "
                "Deleted the bad file. (ND1 verify-on-install)")
    else:
        print(f"  WARNING: {gguf} is not pinned in versions.lock (sha256 null) — recording the downloaded "
              "hash (TOFU). Run 'bob lock' to pin it.", file=sys.stderr)
    return sha


def _update_manifest(gguf: str, url: str, size_gb, sha: str) -> None:
    """Record the (already-computed) SHA256 for a downloaded model. Atomic write — models/manifest.json is
    read concurrently by `bob show`, diagnose and the ND1 lock. Port of Update-Manifest."""
    import json
    from datetime import datetime, timezone

    manifest_path = MODELS_DIR / "manifest.json"
    manifest = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {}
    manifest[gguf] = {"sha256": sha, "sizeGB": size_gb, "url": url,
                      "verifiedAt": datetime.now(timezone.utc).isoformat()}
    tmp = manifest_path.with_suffix(f".{_pid()}.tmp")
    tmp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    tmp.replace(manifest_path)
    print(f"  SHA256: {sha[:16]}... -> models/manifest.json", file=sys.stderr)


def _pid() -> int:
    import os
    return os.getpid()


# --- the download primitive (curl subprocess, DD2 — venv-free for the kernel) ----------------------

def _curl_exe() -> str:
    """curl on PATH (built into Windows 10 1803+; standard elsewhere). Raises if absent."""
    exe = shutil.which("curl")
    if not exe:
        raise RuntimeError("curl not found (install curl; on Windows it ships with Win10 1803+).")
    return exe


def _download(url: str, dest: Path, headers: list) -> None:
    """Resumable download to <dest> via curl (`-C -` resume, `--fail-with-body`). Writes to <dest>.part
    then atomically moves. On curl 22 (HTTP >=400 with --fail-with-body: the error page was written INTO
    the .part, poisoning a future resume) the .part is deleted; other non-zero exits (a network drop)
    leave a valid partial that -C - can legitimately resume. Raises on failure. Port of the fetch loop."""
    part = Path(f"{dest}.part")
    cmd = [_curl_exe(), "-L", "-C", "-", "--fail-with-body", "--progress-bar", *headers,
           "-o", str(part), url]
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        if rc == 22:
            part.unlink(missing_ok=True)
        raise RuntimeError(f"download failed (curl exit {rc}): {url}  (verify repo/filename on huggingface.co)")
    part.replace(dest)


# --- fetch (the capability) -----------------------------------------------------------------------

def fetch_models(profile=None, list_only=False) -> str:
    """Download the GGUFs for a profile into models/. Resume + SHA256-verify (vs versions.lock) + manifest.
    list_only=True is a dry run: report each file's present/MISSING status and size, download nothing.
    Public repos need no token; gated repos read $HF_TOKEN as a bearer header. Port of fetch-models.ps1."""
    import os

    name, models = resolve_fetch_set(profile)
    total_gb = sum(float(m.get("sizeGB") or 0) for m in models)
    lines = [f"Profile '{name}': {len(models)} models, ~{round(total_gb, 1)} GB total"]

    if list_only:
        for m in models:
            present = "present" if (MODELS_DIR / m["gguf"]).exists() else "MISSING"
            lines.append(f"  {m['role']:<10} {m['gguf']:<40} {m.get('sizeGB', '?')} GB  {present}"
                         f"  <- {m['repo']}/{m['path']}")
            if m.get("mmproj"):
                mp = "present" if (MODELS_DIR / m["mmproj"]).exists() else "MISSING"
                lines.append(f"  {m['role'] + '/mmproj':<10} {m['mmproj']:<40} ~0.6 GB  {mp}")
        lines.append("(dry run — nothing downloaded)")
        return "\n".join(lines)

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    lock = _load_lock()
    headers = ["-H", f"Authorization: Bearer {os.environ['HF_TOKEN']}"] if os.environ.get("HF_TOKEN") else []

    # Advisory disk-space pre-check (never fatal).
    missing = [m for m in models if not (MODELS_DIR / m["gguf"]).exists()]
    needed_gb = sum(float(m.get("sizeGB") or 0) for m in missing)
    if needed_gb > 0:
        try:
            free_gb = shutil.disk_usage(MODELS_DIR).free / (1024 ** 3)
            if free_gb < needed_gb * 1.2:
                lines.append(f"  WARNING: low disk space: {free_gb:.1f} GB free, ~{needed_gb:.1f} GB needed "
                             f"(+20% buffer = {needed_gb * 1.2:.1f} GB)")
        except OSError:
            pass

    fail = 0
    for m in models:
        rev = _model_revision(m["gguf"], lock)
        for gguf, rel_path, size_gb in _files_for(m):
            dest = MODELS_DIR / gguf
            if dest.exists():
                lines.append(f"exists  {gguf}")
                continue
            url = f"https://huggingface.co/{m['repo']}/resolve/{rev}/{rel_path}"
            lines.append(f"fetch   {gguf}  <-  {m['repo']}/{rel_path} @ {rev}")
            try:
                _download(url, dest, headers)
                sha = _verify_download(dest, gguf, lock)  # raises + deletes on a pinned mismatch
                _update_manifest(gguf, url, size_gb, sha)
                lines.append(f"done    {gguf}")
            except RuntimeError as e:
                lines.append(f"FAILED  {gguf}: {e}")
                fail += 1

    present = sorted(p.name for p in MODELS_DIR.glob("*.gguf"))
    lines.append(f"\nModels in {MODELS_DIR} ({len(present)}): {', '.join(present) or '(none)'}")
    if fail:
        lines.append(f"WARNING: {fail} file(s) failed — fix config/models.json and re-run.")
    return "\n".join(lines)


def _files_for(m: dict):
    """Yield (gguf, repo_relative_path, sizeGB) for a model: the main GGUF, then its mmproj if present
    (same repo, different file; the mmproj rides the model's pinned revision)."""
    yield m["gguf"], m["path"], m.get("sizeGB")
    if m.get("mmproj"):
        yield m["mmproj"], m["mmproj"], 0.6


# --- lock (D2): read-only status for the agent; the write path is CLI-only (bob lock) -------------

def lock_status() -> str:
    """versions.lock report (read-only): whether it is in sync with its generating sources, plus
    reproducibility vs the installed state (submodule HEADs + present-model SHAs). Does NOT write.
    The regeneration path is `bob lock` (CLI/mutating), deliberately not an agent tool."""
    from bob import versions

    lines = []
    in_sync = versions.check_sync() == 0
    lines.append("versions.lock: in sync with sources ✓" if in_sync
                 else "versions.lock: STALE (out of sync with submodules/models.json) — run: bob lock")
    try:
        lock = versions.load_lock()
    except RuntimeError:
        return "versions.lock not found — run: bob lock"
    drift = versions.check_reproducibility(lock=lock)
    lines.append(f"release {lock.get('release')} — {len(lock.get('submodules') or {})} submodules, "
                 f"{len(lock.get('models') or {})} models")
    if drift:
        for d in drift:
            lines.append(f"  DRIFT {d['kind']} {d['name']}: locked {d['expected'][:12]} != actual {d['actual'][:12]}")
    else:
        lines.append("  reproducible (no drift vs installed state)")
    return "\n".join(lines)


# --- agent tool adapters --------------------------------------------------------------------------

def _fetch_models(profile: str = "", list_only: bool = False) -> str:
    return fetch_models(profile or None, list_only=list_only)


def _lock_status() -> str:
    return lock_status()


def test() -> str:
    name, models = resolve_fetch_set()
    return f"fetch set for '{name}': {len(models)} models"


TOOL_DEFS = [
    {"type": "function", "function": {
        "name": "fetch_models",
        "description": ("Download the model files (GGUFs) for a profile into models/, with resume, SHA256 "
                        "verification against versions.lock, and manifest recording. Mutating + long "
                        "(multi-GB downloads). Use when the user wants to download / fetch models. Pass "
                        "list_only=true for a dry run (report what's present/missing, download nothing)."),
        "parameters": {"type": "object", "properties": {
            "profile": {"type": "string", "description": "Profile name (default: the active profile)."},
            "list_only": {"type": "boolean", "description": "Dry run — list files + status, download nothing."}}}}},
    {"type": "function", "function": {
        "name": "lock_status",
        "description": ("Report whether versions.lock is in sync with its generating sources and whether the "
                        "installed state (submodule commits + model checksums) matches the lock. Read-only. "
                        "Use to answer 'is my install reproducible / pinned?'. Regenerating is `bob lock` (CLI)."),
        "parameters": {"type": "object", "properties": {}}}},
]

DISPATCH = {"fetch_models": _fetch_models, "lock_status": _lock_status}
