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


# --- setup-voice (D7): provision whisper + piper (post-venv) --------------------------------------

def _dl_file(url: str, dest: Path, label: str, force: bool, out: list) -> None:
    import urllib.request
    if dest.exists() and not force:
        out.append(f"  {label} already present — skipping.")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(url, dest)  # noqa: S310 — fixed HF/GitHub https URLs
    out.append(f"  saved {label}")


def _install_piper(url: str, win: bool, bindir: Path, out: list) -> None:
    """Download + extract the piper release: binary -> bin/piper(.exe), shared libs + espeak-ng-data ->
    bin/. Port of setup-voice.ps1 step 3's extract."""
    import tarfile
    import tempfile
    import urllib.request
    import zipfile

    tmp = Path(tempfile.mkdtemp())
    arc = tmp / ("piper.zip" if win else "piper.tar.gz")
    urllib.request.urlretrieve(url, arc)  # noqa: S310
    if win:  # pragma: no cover — Windows piper .zip
        with zipfile.ZipFile(arc) as z:
            z.extractall(tmp)
        binname = "piper.exe"
    else:
        with tarfile.open(arc) as t:
            t.extractall(tmp, filter="data")  # 3.12+ safe-extraction filter
        binname = "piper"
    found = next((p for p in tmp.rglob(binname) if p.is_file()), None)
    if not found:
        raise RuntimeError(f"{binname} not found in the extracted piper archive")
    bindir.mkdir(parents=True, exist_ok=True)
    dst = bindir / binname
    shutil.copy2(found, dst)
    if not win:
        dst.chmod(0o755)
    for lib in found.parent.glob("*.so*" if not win else "*.dll"):
        shutil.copy2(lib, bindir / lib.name)
    espeak = found.parent / "espeak-ng-data"
    if espeak.exists():
        dest = bindir / "espeak-ng-data"
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(espeak, dest)
        out.append("  espeak-ng-data/ copied to bin/")
    else:
        out.append("  WARNING: espeak-ng-data not found beside piper — TTS phonemization may fail")
    shutil.rmtree(tmp, ignore_errors=True)
    out.append("  piper extracted to bin/")


def _silent_wav(path: Path, seconds: int = 2, rate: int = 44100) -> None:
    import wave
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * (rate * seconds))


def _multipart(fields: dict, filename: str, file_bytes: bytes) -> "tuple[bytes, str]":
    """Build a multipart/form-data body (one file field 'file' + text fields). Stdlib-only so the voice
    smoke runs under the bare kernel interpreter (no requests). Returns (body, content_type)."""
    import os
    boundary = f"----bob{os.getpid()}"
    pre = b""
    for k, v in fields.items():
        pre += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
    pre += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n").encode()
    body = pre + file_bytes + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def _voice_smoke(stt_port: int) -> str:
    """Best-effort STT smoke: start whisper, POST a silent WAV, stop. Never fatal (port of step 5). Stdlib
    urllib (no requests) so it works both under `bob setup-voice` (venv) and the cold-start kernel (system
    python3)."""
    import json as _json
    import os
    import tempfile
    import time
    import urllib.request

    sys.path.insert(0, str(SCRIPTS / "tools"))
    import stack
    wav = Path(tempfile.gettempdir()) / f"bob-stt-probe-{os.getpid()}.wav"
    _silent_wav(wav)
    try:
        stack.configure(_cfg)
        stack.service_control(_cfg, "whisper", "start")
        time.sleep(2)
        body, ctype = _multipart({"temperature": "0.0", "response_format": "json"},
                                 wav.name, wav.read_bytes())
        req = urllib.request.Request(f"http://localhost:{stt_port}/inference", data=body,
                                     headers={"Content-Type": ctype}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 — localhost only
            data = _json.loads(r.read().decode("utf-8", "replace"))
        text = data.get("text", "") if isinstance(data, dict) else str(data)
        return f"  smoke test passed. Transcript of silence: '{text.strip()}'"
    except Exception as e:  # noqa: BLE001 — smoke is advisory
        return f"  smoke test skipped/failed ({e}) — verify later: bob transcribe <file>"
    finally:
        wav.unlink(missing_ok=True)
        try:
            stack.service_control(_cfg, "whisper", "stop")
        except Exception:  # noqa: BLE001
            pass


def setup_voice(force: bool = False, smoke: bool = True) -> str:
    """Provision Phase-2 voice: build whisper-server, download the whisper model + piper binary/voice +
    espeak-ng-data, install sounddevice+numpy into venv-litellm, and (best-effort) smoke-test STT. Port of
    setup-voice.ps1. Post-venv (needs venv-litellm pip)."""
    import subprocess

    import osenv
    from bob_core import _port

    voice = (_cfg or {}).get("voice", {})
    stt_model = voice.get("sttModel", "small")
    tts_voice = voice.get("ttsVoice", "en_GB-alan-medium")
    win = osenv.os_name() == "windows"
    out = ["Voice setup:"]

    parts = tts_voice.split("-")  # en_GB-alan-medium
    lang = parts[0].split("_")[0]
    vbase = (f"https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/{lang}/{parts[0]}/"
             f"{parts[1]}/{parts[2]}/{tts_voice}")
    piper_url = ("https://github.com/rhasspy/piper/releases/download/2023.11.14-2/"
                 + ("piper_windows_amd64.zip" if win else "piper_linux_x86_64.tar.gz"))

    # [1/5] whisper-server
    server = osenv.bin_exe("whisper-server")
    if force or not server.exists():
        sys.path.insert(0, str(SCRIPTS / "tools"))
        import build
        build.configure(_cfg)
        out.append(build.build_whisper(force=force))
    else:
        out.append("  whisper-server already built — skipping.")

    # [2/5] whisper model
    _dl_file(f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{stt_model}.bin",
             REPO / "models" / "whisper" / f"ggml-{stt_model}.bin", f"ggml-{stt_model}.bin", force, out)

    # [3/5] piper binary + voice model
    bindir = REPO / "bin"
    voices = bindir / "voices"
    if force or not osenv.bin_exe("piper").exists():
        _install_piper(piper_url, win, bindir, out)
    _dl_file(f"{vbase}.onnx", voices / f"{tts_voice}.onnx", f"{tts_voice}.onnx", force, out)
    _dl_file(f"{vbase}.onnx.json", voices / f"{tts_voice}.onnx.json", f"{tts_voice}.onnx.json", force, out)

    # [4/5] python audio deps
    pip = osenv.venv_exe("venv-litellm", "pip")
    if not pip.exists():
        raise RuntimeError("venv-litellm not found — run bootstrap first")
    subprocess.run([str(pip), "install", "--quiet", "sounddevice", "numpy"])
    out.append("  sounddevice + numpy installed")

    # [5/5] smoke test (best-effort)
    if smoke:
        out.append(_voice_smoke(_port(_cfg, "sttPort")))
    out.append("\nVoice setup complete. Enable voice.enabled / vision.enabled in your config to use it.")
    return "\n".join(out)


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


def _mlock_status() -> str:
    import osenv
    st = osenv.mlock_status()
    return ("mlock: " + ("granted ✓  " if st["granted"] else "NOT granted  ") + st["detail"]
            + ("" if st["granted"] else "\n  grant it with: bob mlock --grant"))


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
    {"type": "function", "function": {
        "name": "mlock_status",
        "description": ("Report whether the mlock privilege is granted (Windows SeLockMemoryPrivilege / Linux "
                        "memlock rlimit) so llama-server's --mlock can pin weights in RAM. Read-only. Granting "
                        "requires elevation and is `bob mlock --grant` (CLI-only, not an agent action)."),
        "parameters": {"type": "object", "properties": {}}}},
]

DISPATCH = {"fetch_models": _fetch_models, "lock_status": _lock_status, "mlock_status": _mlock_status}
