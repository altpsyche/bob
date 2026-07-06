"""Bob model-registry capabilities (ONE-C Slice 4) — the read-only + profile verbs, built on the neutral
registry (config/models.json via bob_models.py, C0c). Functional grouping (D6): one module, several
related tool fns, each reached three ways (agent tool / `bob <verb>` / `bob --run`) with no duplicated
logic. Ports the bob.ps1 models/show/profiles/profile/verify-urls/bench cases + Get-Models/Get-GpuVramGB/
Get-SuggestedProfile.

A profile switch regenerates the runtime configs via the single-sourced best-effort
bob_models.regenerate_configs (the Python generators — shared with the stack bring-up)."""
import sys
from pathlib import Path

_cfg: dict = {}

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO / "scripts"

# Stable role order so output is deterministic regardless of dict enumeration (mirrors Get-Models).
_ROLE_ORDER = ["planner", "coder", "chat", "fim", "embed"]


def configure(config: dict) -> None:
    global _cfg
    _cfg = config
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))


MUTATING_TOOLS = {"profile_switch"}


# --- helpers --------------------------------------------------------------------------------------

def _ordered_roles(roles: dict) -> list:
    """Role names in the canonical order (planner,coder,chat,fim,embed, then the rest sorted)."""
    known = [r for r in _ROLE_ORDER if r in roles]
    rest = sorted(r for r in roles if r not in _ROLE_ORDER)
    return known + rest


def _model_path(gguf: str) -> Path:
    return REPO / "models" / gguf


def gpu_vram_gb():
    """Total VRAM of GPU 0 in whole GB, or None (no GPU / nvidia-smi absent). Delegates to the single
    source osenv.gpu_vram_gb (ONE-D consolidated the former per-module copies)."""
    import osenv
    return osenv.gpu_vram_gb()


def suggested_profile(vram_gb=None, config=None):
    """Largest '<N>gb' profile whose N <= detected VRAM (else the smallest sized one). None with no GPU
    info. Port of Get-SuggestedProfile."""
    import bob_models

    if vram_gb is None:
        vram_gb = gpu_vram_gb()
    if not vram_gb:
        return None
    config = config if config is not None else bob_models.load_models_config()
    import re

    sized = []
    for name in config.get("profiles", {}):
        m = re.match(r"^(\d+)gb$", name)
        if m:
            sized.append((name, int(m.group(1))))
    if not sized:
        return None
    fits = sorted([(n, gb) for n, gb in sized if gb <= vram_gb], key=lambda x: -x[1])
    if fits:
        return fits[0][0]
    return sorted(sized, key=lambda x: x[1])[0][0]


# --- capabilities (each takes/uses config, returns a string) --------------------------------------

def models_list(config: dict) -> str:
    """Active-profile roles + load state (queries the endpoint's /v1/models). Port of the `models` case."""
    import bob_models
    import requests
    from bob_core import _port

    mcfg = bob_models.load_models_config()
    profile = bob_models.resolve_profile_name(config=mcfg)
    roles = bob_models.profile_roles(profile, mcfg)

    loaded, endpoint_up = set(), False
    try:
        data = requests.get(f"http://localhost:{_port(config, 'port')}/v1/models", timeout=3).json()
        loaded = {m.get("id") for m in data.get("data", [])}
        endpoint_up = True
    except (requests.RequestException, ValueError):
        pass

    lines = ["", f"Profile: {profile}", "",
             f"{'Role':<10} {'Model':<42} {'VRAM':<9} State", "-" * 70]
    for role in _ordered_roles(roles):
        spec = roles[role]
        label = spec.get("gguf", "").replace(".gguf", "").replace("-", " ").replace("_", " ")
        label = f"{label} ({spec.get('sizeGB', '?')} GB)"
        if not endpoint_up:
            state = "(endpoint down)"
        elif role in loaded:
            state = "loaded, pinned" if spec.get("pinned") else "loaded"
        else:
            state = "unloaded"
        lines.append(f"{role:<10} {label:<42} {str(spec.get('sizeGB', '?')) + ' GB':<9} {state}")
    lines.append("")
    if not endpoint_up:
        lines.append("Endpoint not running — state unknown. bob serve")
    return "\n".join(lines)


def model_show(role: str, config: dict) -> str:
    """file/VRAM/repo/path/on-disk/SHA for one role. Port of the `show` case."""
    import json

    import bob_models

    mcfg = bob_models.load_models_config()
    roles = bob_models.profile_roles(config=mcfg)
    if role not in roles:
        return f"Unknown role '{role}'. Valid: {', '.join(_ordered_roles(roles))}"
    spec = roles[role]
    gguf = spec.get("gguf", "")
    dest = _model_path(gguf)
    lines = ["", f"Role:     {role}", f"File:     {gguf}", f"VRAM:     {spec.get('sizeGB', '?')} GB",
             f"Repo:     {spec.get('repo', '?')}", f"Path:     {spec.get('path', '?')}"]
    if dest.exists():
        lines.append(f"On disk:  {round(dest.stat().st_size / (1024 ** 3), 2)} GB")
    else:
        lines.append("On disk:  MISSING")
    manifest_file = REPO / "models" / "manifest.json"
    if manifest_file.exists():
        try:
            entry = json.loads(manifest_file.read_text(encoding="utf-8")).get(gguf)
            if entry:
                lines.append(f"SHA256:   {entry.get('sha256', '')[:24]}...")
                lines.append(f"Verified: {entry.get('verifiedAt', '')}")
        except (json.JSONDecodeError, OSError):
            pass
    lines.append("")
    return "\n".join(lines)


def profiles_list(config: dict) -> str:
    """All VRAM profiles with size, on-disk count, target VRAM, and a suggestion. Port of `profiles`."""
    import bob_models

    mcfg = bob_models.load_models_config()
    active = mcfg.get("activeProfile")
    lines = []
    for name in sorted(mcfg.get("profiles", {})):
        roles = bob_models.profile_roles(name, mcfg)
        total = sum(float(s.get("sizeGB", 0) or 0) for s in roles.values())
        have = sum(1 for s in roles.values() if _model_path(s.get("gguf", "")).exists())
        mark = "* " if name == active else "  "
        target = mcfg["profiles"][name].get("_targetVRAM", "")
        lines.append(f"{mark}{name:<6} ~{total:5.1f} GB  {have}/{len(roles)} on disk   {target}")
    vram = gpu_vram_gb()
    sug = suggested_profile(vram, mcfg)
    if sug:
        lines.append(f"\nDetected ~{vram} GB VRAM -> suggested '{sug}'.")
    lines.append("(* = active)  switch: bob profile <name>")
    return "\n".join(lines)


def profile_switch(name: str, config: dict) -> str:
    """Switch the active profile (name or 'auto' = detect VRAM), persist via bob_models.set_active_profile
    (data/active-profile.json, D4), regenerate configs best-effort, and report on-disk status. Port of
    the `profile` case."""
    import bob_models

    mcfg = bob_models.load_models_config()
    if not name or name == "auto":
        vram = gpu_vram_gb()
        if not vram:
            target = "cpu" if "cpu" in mcfg.get("profiles", {}) else None
            if not target:
                return "No GPU detected and no 'cpu' profile available."
            header = "No GPU detected -> switching to the 'cpu' profile (correctness/wiring only)."
        else:
            target = suggested_profile(vram, mcfg)
            if not target:
                return (f"No profile fits {vram} GB VRAM. Available: "
                        f"{', '.join(sorted(mcfg['profiles']))}")
            header = f"Detected {vram} GB VRAM -> switching to profile: {target}"
    else:
        target = name
        header = f"Switching to profile: {target}"

    try:
        bob_models.set_active_profile(target, mcfg)
    except ValueError as e:
        return str(e)
    bob_models.regenerate_configs()  # best-effort; runs the Python generators (generate.py)

    # Report which of the new profile's models are on disk.
    roles = bob_models.profile_roles(target)
    missing = [r for r in _ordered_roles(roles) if not _model_path(roles[r].get("gguf", "")).exists()]
    lines = [header, f"activeProfile -> '{target}'"]
    if missing:
        lines.append(f"MISSING on disk: {', '.join(missing)} — download with: bob fetch")
    else:
        lines.append("All models for this profile are on disk.")
    lines.append("Apply now: bob restart  (or bob up)")
    return "\n".join(lines)


_VERIFY_ROLES = ["planner", "coder", "chat", "fim", "embed", "vision", "agent"]


def verify_urls(profile: str, config: dict) -> str:
    """HEAD every HuggingFace resolve URL for a profile (or all profiles). Reports OK/REDIRECT/GATED/
    MISSING/ERROR per model. Port of verify-urls.ps1 (extended to cover vision/agent too). Set HF_TOKEN
    for gated repos."""
    import os

    import bob_models
    import requests

    mcfg = bob_models.load_models_config()
    all_profiles = sorted(mcfg.get("profiles", {}))
    profiles = [profile] if profile else all_profiles
    headers = {"Authorization": f"Bearer {os.environ['HF_TOKEN']}"} if os.environ.get("HF_TOKEN") else {}

    lines, any_bad = [], False
    for pname in profiles:
        if pname not in mcfg["profiles"]:
            lines.append(f"unknown profile '{pname}'")
            any_bad = True
            continue
        lines.append(f"\nProfile '{pname}'")
        prof = mcfg["profiles"][pname]
        for role in _VERIFY_ROLES:
            spec = prof.get(role)
            if not spec:
                continue
            url = f"https://huggingface.co/{spec['repo']}/resolve/main/{spec['path']}"
            try:
                resp = requests.head(url, headers=headers, allow_redirects=False, timeout=15)
                code = resp.status_code
                if code == 200:
                    status = "OK"
                elif 300 <= code < 400:
                    status = "REDIRECT"   # CDN-accessible
                elif code in (401, 403):
                    status = "GATED"
                elif code == 404:
                    status = "MISSING"
                else:
                    status = f"HTTP_{code}"
            except requests.RequestException:
                status = "ERROR"
            if status == "MISSING" or status == "ERROR" or status.startswith(("HTTP_4", "HTTP_5")):
                any_bad = True
            lines.append(f"  {role:<8} {status:<12} {url}")
    if any_bad:
        lines.append("\n(some URLs MISSING/ERROR — fix config/models.json and re-check)")
    return "\n".join(lines)


def bench(role: str, config: dict) -> str:
    """Run llama-bench on a role's gguf (defaults to coder). Port of the `bench` case. Returns the
    benchmark output; requires the staged llama-bench binary + the model on disk."""
    import subprocess

    import bob_models
    import osenv

    exe = osenv.bin_exe("llama-bench")
    if not exe.exists():
        return f"llama-bench not found ({exe}). Build llama.cpp first: bob build"
    target = role or "coder"
    roles = bob_models.profile_roles()
    if target in roles:
        model = _model_path(roles[target].get("gguf", ""))
        if not model.exists():
            return f"Model for role '{target}' is not on disk ({model.name}). Download: bob fetch"
        model_arg = str(model)
    else:
        model_arg = target  # treat as a literal path/name
    try:
        r = subprocess.run([str(exe), "-m", model_arg, "-ngl", "99", "-fa", "1", "-p", "512", "-n", "128"],
                           capture_output=True, text=True, timeout=600)
    except subprocess.SubprocessError as e:
        return f"llama-bench failed: {e}"
    return (r.stdout or r.stderr or "(no output)").strip()


def eval_model(role: str = "coder", task: str = "mmlu", shots: int = 0, limit: int = 0,
               config: dict = None, now: str = None) -> int:
    """Benchmark a role's quality with lm-evaluation-harness (DD3: the isolated venv-eval). CLI-only + very
    long (minutes→hours), so it inherits stdio and returns the process exit code — NOT an agent tool, not
    on --run. Reads the tokenizer from the active profile (config/models.json) and checks the endpoint
    first. Port of scripts/eval.ps1. `now` is an injectable timestamp for tests."""
    import os
    import subprocess
    from datetime import datetime

    import bob_models
    import osenv
    import requests
    from bob_core import _port

    config = config if config is not None else _cfg
    lm_eval = osenv.venv_exe("venv-eval", "lm_eval")
    if not lm_eval.exists():
        # DD3 — the isolated eval venv is provisioned lazily via the shared osenv.new_bob_venv (Slice D8,
        # replacing the retired bootstrap-eval.ps1); lm-eval + transformers are heavy so it's kept off the
        # default bootstrap. Best-effort: a provisioning failure surfaces the actionable hint.
        print("lm-eval venv not found — provisioning tools/venv-eval (lm-eval + transformers)...",
              file=sys.stderr)
        try:
            osenv.new_bob_venv("venv-eval", "eval-requirements")
        except RuntimeError as e:
            print(f"could not provision venv-eval ({e}). Create it manually and re-run.", file=sys.stderr)
            return 1
        if not lm_eval.exists():
            print(f"lm-eval still not installed ({lm_eval}) after provisioning.", file=sys.stderr)
            return 1

    roles = bob_models.profile_roles()
    spec = roles.get(role)
    if not spec:
        print(f"unknown role '{role}'. Roles: {', '.join(sorted(roles))}", file=sys.stderr)
        return 1
    tokenizer = spec.get("tokenizer")
    if not tokenizer:
        print(f"no tokenizer configured for role '{role}'. Add 'tokenizer' to its entry in "
              "config/models.json.", file=sys.stderr)
        return 1

    port = _port(config, "port")
    try:
        requests.get(f"http://localhost:{port}/v1/models", timeout=3).raise_for_status()
    except requests.RequestException:
        print(f"endpoint not running at http://localhost:{port}/v1 — start it first: bob serve",
              file=sys.stderr)
        return 1

    results_dir = REPO / "results"
    results_dir.mkdir(exist_ok=True)
    stamp = now or datetime.now().strftime("%Y%m%d-%H%M")
    out_path = results_dir / f"eval-{role}-{task}-{stamp}"

    limit_note = f" (limit={limit})" if limit > 0 else ""
    print(f"Benchmarking '{role}' on '{task}' (shots={shots}){limit_note}...", file=sys.stderr)
    print(f"Endpoint:  http://localhost:{port}/v1/chat/completions", file=sys.stderr)
    print(f"Tokenizer: {tokenizer}\nResults:   {out_path}\n", file=sys.stderr)

    args = [str(lm_eval), "--model", "local-chat-completions",
            "--model_args", (f"base_url=http://localhost:{port}/v1/chat/completions,model={role},"
                             f"tokenizer={tokenizer},tokenized_requests=False"),
            "--tasks", task, "--apply_chat_template", "--num_fewshot", str(shots),
            "--output_path", str(out_path), "--log_samples"]
    if limit > 0:
        args += ["--limit", str(limit)]
    env = {**os.environ, "PYTHONUTF8": "1"}  # avoid UnicodeEncodeError on a cp1252 Windows console
    return subprocess.run(args, env=env).returncode


# --- agent tool adapters --------------------------------------------------------------------------

def _models_list() -> str:
    return models_list(_cfg)


def _model_show(role: str) -> str:
    return model_show(role, _cfg)


def _profiles_list() -> str:
    return profiles_list(_cfg)


def _profile_switch(name: str = "auto") -> str:
    return profile_switch(name, _cfg)


def _verify_urls(profile: str = "") -> str:
    return verify_urls(profile, _cfg)


def _bench(role: str = "coder") -> str:
    return bench(role, _cfg)


TOOL_DEFS = [
    {"type": "function", "function": {
        "name": "models_list",
        "description": "List the active profile's model roles with backing file, VRAM, and load state (queries the endpoint). Read-only.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "model_show",
        "description": "Show one role's model details: file, VRAM, HuggingFace repo/path, on-disk size, SHA256. Read-only.",
        "parameters": {"type": "object", "properties": {
            "role": {"type": "string", "description": "Role name (planner/coder/chat/fim/embed/vision/agent)."}},
            "required": ["role"]}}},
    {"type": "function", "function": {
        "name": "profiles_list",
        "description": "List all VRAM profiles with total size, models-on-disk count, and a VRAM-based suggestion. Read-only.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "profile_switch",
        "description": "Switch the active VRAM profile ('auto' detects from GPU VRAM), regenerate configs, and report which models need downloading. Apply with a restart.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Profile name (e.g. 8gb/12gb/16gb/24gb/32gb/cpu) or 'auto'."}}}}},
    {"type": "function", "function": {
        "name": "verify_urls",
        "description": "HEAD-check every model's HuggingFace download URL for a profile (or all). Network, read-only. Reports OK/REDIRECT/GATED/MISSING/ERROR.",
        "parameters": {"type": "object", "properties": {
            "profile": {"type": "string", "description": "Profile to check; empty = all profiles."}}}}},
    {"type": "function", "function": {
        "name": "bench",
        "description": "Run a llama-bench throughput benchmark on a role's model (default coder). Runs the local binary; may take a while.",
        "parameters": {"type": "object", "properties": {
            "role": {"type": "string", "description": "Role to benchmark (default coder), or a model path."}}}}},
]

DISPATCH = {
    "models_list": _models_list, "model_show": _model_show, "profiles_list": _profiles_list,
    "profile_switch": _profile_switch, "verify_urls": _verify_urls, "bench": _bench,
}
