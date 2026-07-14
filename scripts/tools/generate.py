"""Bob config generators — regenerate the runtime configs from the neutral registry
(config/models.json via bob_models). One core fn per config reached the standard three ways;
`gen` runs all four.

  gen_llama_swap  -> config/llama-swap.yaml   (macros + per-model cmd assembly + swap group)
  gen_litellm     -> config/litellm.yaml      (local models via llama-swap + pro models via peers)
  gen_continue    -> config/continue/config.yaml
  gen_webui       -> tools/webui-data/webui.db (model system prompts; skips if the db is absent)

Deterministic + idempotent."""
import sys
from pathlib import Path

_cfg: dict = {}

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO / "scripts"

MUTATING_TOOLS = {"gen"}

# Canonical role order: ponder,coder,chat,fim,embed first, then the rest sorted.
_ROLE_ORDER = ["ponder", "coder", "chat", "fim", "embed"]


def configure(config: dict) -> None:
    global _cfg
    _cfg = config
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))


# --- shared helpers -------------------------------------------------------------------------------

def _fmt(v) -> str:
    """InvariantCulture-style scalar formatting: bools lowercase, integral floats
    without a decimal point, everything else str()."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    return str(v)


def _assert_no_quote(s: str, what: str) -> None:
    if '"' in str(s):
        raise ValueError(f"value for {what} contains a double-quote, which would break the generated YAML: {s}")


def _ordered_models(mcfg: dict, profile: str = None):
    """(profile_name, [spec-with-'role', ...]) in canonical role order. Skips '_'-prefixed metadata keys."""
    import bob_models

    name = bob_models.resolve_profile_name(profile, mcfg)
    roles = bob_models.profile_roles(name, mcfg)
    ordered = [r for r in _ROLE_ORDER if r in roles] + sorted(r for r in roles if r not in _ROLE_ORDER)
    models = []
    for role in ordered:
        spec = dict(roles[role])
        spec["role"] = role
        models.append(spec)
    return name, models


def enabled_peers(mcfg: dict):
    """Enabled peers as dicts with 'name', in registry (insertion) order."""
    peers = mcfg.get("peers", {})
    out = []
    for name, spec in peers.items():
        if spec.get("enabled") is False:
            continue
        p = dict(spec)
        p["name"] = name
        out.append(p)
    return out


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _bob_cfg() -> dict:
    return _cfg or {}


# --- gen-llama-swap -------------------------------------------------------------------------------

def gen_llama_swap(profile: str = None) -> str:
    """Generate config/llama-swap.yaml from the registry."""
    import bob_models
    import osenv

    mcfg = bob_models.load_models_config()
    name, models = _ordered_models(mcfg, profile)
    d = mcfg.get("defaults")
    if not d:
        raise RuntimeError("models.json is missing the defaults block.")
    macros = dict(mcfg.get("macros", {}))

    srv_bin = "${env.LLAMA_LOCAL_ROOT}/bin/" + osenv.exe_name("llama-server")
    is_cpu = name == "cpu"

    ngl = 0 if is_cpu else (d["ngl"] if d.get("ngl") is not None else 99)
    fa = "--flash-attn on" if (d.get("flashAttn") is not False and not is_cpu) else ""
    # Pin reasoning extraction rather than trust the engine default (which a bump can flip, exactly the
    # class of regression the -ngl 99 MoE change was): 'deepseek' routes a reasoning model's <think>
    # content into the separate `reasoning_content` field, including streaming deltas, so the agent
    # loop's content-only stream reader keeps it out of the transcript and memory.
    reason = "--reasoning-format deepseek"
    batch = f"-b {d['batch']}" if d.get("batch") and d["batch"] != 512 else ""
    ub = f"-ub {d['ubatch']}" if d.get("ubatch") and d["ubatch"] != 512 else ""
    par = f"-np {d['parallel']}" if d.get("parallel") and d["parallel"] > 1 else ""
    thr = f"-t {d['threads']}" if d.get("threads") and d["threads"] > 0 else ""
    numa = f"--numa {d['numa']}" if d.get("numa") else ""
    srv_parts = [p for p in [srv_bin, "--port ${PORT}", f"-ngl {ngl}", fa, reason, batch, ub, numa, par, thr] if p]
    macros["srv"] = " ".join(srv_parts)

    legacy_kv = d["kvQuant"] if d.get("kvQuant") not in (None, "") else None
    kv_k = legacy_kv or (d["kvQuantK"] if d.get("kvQuantK") is not None else "q8_0")
    kv_v = legacy_kv or (d["kvQuantV"] if d.get("kvQuantV") is not None else "q8_0")
    macros["kv"] = "" if is_cpu else (f"--cache-type-k {kv_k} --cache-type-v {kv_v}" if (kv_k or kv_v) else "")

    members = list(mcfg.get("group", {}).get("members", []))
    role_names = [m["role"] for m in models]
    by_role = {m["role"]: m for m in models}
    global_mlock_big = d.get("mlockBig") is True
    global_no_mmap = d.get("noMmap") is True

    for m in models:
        _assert_no_quote(m.get("gguf", ""), f"model '{m['role']}' gguf")
        if "gemma" in m.get("gguf", "") and m.get("kv") is True:
            print(f"[{m['role']}] Gemma model with kv=true — KV quant causes quality regression.",
                  file=sys.stderr)
        # Flash-attn is incompatible with mmproj; expand srv without it for that model.
        if m.get("mmproj") and fa != "":
            srv_ref = " ".join(p for p in [srv_bin, "--port ${PORT}", f"-ngl {ngl}", batch, ub, numa, par, thr] if p)
        else:
            srv_ref = "${srv}"
        parts = [srv_ref, f"-m ${{env.LLAMA_LOCAL_ROOT}}/models/{m['gguf']}"]
        if m.get("ctx") is not None:
            parts.append(f"-c {_fmt(m['ctx'])}")
        if m.get("kv"):
            parts.append("${kv}")
        if m.get("embedding"):
            parts.append("--embedding")
        if m.get("reranking"):
            parts.append("--reranking")   # enable llama.cpp's /v1/rerank endpoint (rank-pooling model)
        for f in (m.get("flags") or []):
            _assert_no_quote(f, f"model '{m['role']}' flag")
            parts.append(str(f))
        apply_mlock = (m.get("mlock") is True) or (global_mlock_big and m["role"] in members)
        if apply_mlock:
            parts.append("--mlock")
        model_no_mmap = (m["noMmap"] is True) if m.get("noMmap") is not None else global_no_mmap
        if model_no_mmap:
            parts.append("--no-mmap")
        # MoE expert offload: keep the experts of the first N layers in system RAM so a MoE model whose
        # weights overflow VRAM still fits alongside -ngl 99. Only the active experts (~3B for an A3B)
        # stream from RAM per token, so it stays fast. Per-profile in config/models.json; skipped on the
        # CPU tier (already all-CPU). Newer llama.cpp no longer auto-spills at -ngl 99, so this is how a
        # big MoE runs on a small card.
        n_cpu_moe = m.get("nCpuMoe")
        if n_cpu_moe and not is_cpu:
            parts.append(f"--n-cpu-moe {int(n_cpu_moe)}")
        if m.get("draftRole"):
            draft = by_role.get(m["draftRole"])
            if not draft:
                print(f"[{m['role']}] draftRole '{m['draftRole']}' not found in profile — "
                      "speculative decoding disabled.", file=sys.stderr)
            elif draft.get("pinned") is not True:
                print(f"[{m['role']}] draftRole '{m['draftRole']}' is not pinned — draft must be in VRAM. "
                      "Skipping.", file=sys.stderr)
            else:
                parts.append(f"-md ${{env.LLAMA_LOCAL_ROOT}}/models/{draft['gguf']}")
                parts.append("-ngld 99")
        if m.get("mmproj"):
            _assert_no_quote(m["mmproj"], f"model '{m['role']}' mmproj")
            parts.append(f"--mmproj ${{env.LLAMA_LOCAL_ROOT}}/models/{m['mmproj']}")
        m["_cmd"] = " ".join(parts)

    # group assertions
    active_members = []
    for mem in members:
        if mem not in role_names:
            print(f"group member '{mem}' not in profile '{name}' — skipping in swap group", file=sys.stderr)
            continue
        if by_role[mem].get("pinned"):
            raise RuntimeError(f"model '{mem}' is pinned but also listed in group.members — pinned models "
                               "must stay out of the swap group")
        active_members.append(mem)

    nl = "\n"
    out = []
    out.append("# =============================================================")
    out.append("#  GENERATED - DO NOT EDIT.  Source: config/models.json")
    out.append("#  Regenerate: bob gen  (also runs on `bob serve`)")
    out.append(f"#  Active profile: {name}")
    out.append("# =============================================================")
    out.append("")
    out.append("macros:")
    macro_order = ["srv", "kv"] + sorted(k for k in macros if k not in ("srv", "kv"))
    for k in macro_order:
        if k not in macros:
            continue
        val = str(macros[k])
        _assert_no_quote(val, f"macro '{k}'")
        out.append(f'  {k}: "{val}"')
    out.append("")
    out.append("models:")
    for m in models:
        out.append(f"  {m['role']}:")
        out.append(f'    cmd: "{m["_cmd"]}"')
        if m.get("setParams"):
            pairs = ", ".join(f"{k}: {_fmt(m['setParams'][k])}" for k in sorted(m["setParams"]))
            out.append("    filters:")
            out.append(f"      setParams: {{ {pairs} }}")
        if m.get("ttl") is not None:
            out.append(f"    ttl: {_fmt(m['ttl'])}")
        out.append("")
    out.append("groups:")
    out.append(f"  {mcfg['group']['name']}:")
    out.append(f"    swap: {_fmt(mcfg['group']['swap'])}")
    out.append(f"    members: [{', '.join(active_members)}]")

    dest = _write(REPO / "config" / "llama-swap.yaml", nl.join(out))
    return f"generated {dest}  (profile: {name})"


# --- gen-litellm ----------------------------------------------------------------------------------

def gen_litellm(profile: str = None) -> str:
    """Generate config/litellm.yaml."""
    import bob_models
    import osenv
    from bob_core import _port

    mcfg = bob_models.load_models_config()
    _, models = _ordered_models(mcfg, profile)
    peers = enabled_peers(mcfg)
    bobcfg = _bob_cfg()
    port = mcfg.get("defaults", {}).get("port") or _port(bobcfg, "port")
    litellm_key = osenv.secret("litellmKey", default=bobcfg.get("litellmKey", "sk-local"), config=bobcfg)

    out = ["# GENERATED - DO NOT EDIT.  Source: config/models.json",
           "# Regenerate: bob gen  (also runs on `bob serve`)", "", "model_list:"]
    for m in models:
        # Rerankers aren't OpenAI chat/embedding models — LiteLLM's /rerank expects a cohere/jina/infinity
        # provider, not openai/. The rerank call goes straight to llama-swap's native /v1/rerank instead.
        if m.get("reranking"):
            continue
        out += [f"  - model_name: {m['role']}", "    litellm_params:",
                f"      model: openai/{m['role']}", f"      api_base: http://localhost:{port}/v1",
                f"      api_key: {litellm_key}"]
        if m.get("supportsVision"):
            out.append("      supports_vision: true")

    import os as _os
    for peer in peers:
        pro = peer.get("pro") or {}
        if not pro:
            continue
        key_env = peer.get("apiKeyEnv")
        if key_env and not _os.environ.get(key_env):
            print(f"gen-litellm: env var '{key_env}' not set for peer '{peer['name']}' — pro models will "
                  "fail at request time", file=sys.stderr)
        prefix = peer.get("litellmPrefix") or "openai"
        proxy = peer.get("proxy")
        for role in sorted(pro):
            rv = pro[role]
            if isinstance(rv, str):
                model_id, max_toks = rv, None
            else:
                model_id, max_toks = rv.get("model"), rv.get("maxTokens")
            out += [f"  - model_name: {role}-pro", "    litellm_params:",
                    f"      model: {prefix}/{model_id}"]
            if proxy:
                out.append(f"      api_base: {proxy}")
            out.append(f"      api_key: os.environ/{key_env}")
            if max_toks:
                out.append(f"      max_tokens: {max_toks}")

    out += ["", "litellm_settings:", "  num_retries: 3"]
    req_timeout = bobcfg.get("agent", {}).get("requestTimeout", 600)
    out.append(f"  request_timeout: {req_timeout}")

    budget_peer = next((p for p in peers if p.get("budget") and p["budget"] > 0), None)
    if budget_peer:
        period = budget_peer.get("budgetPeriod") or "1d"
        out.append(f"  max_budget: {budget_peer['budget']}")
        out.append(f'  budget_duration: "{period}"')

    if mcfg.get("defaults", {}).get("langfuseEnabled"):
        lf_port = mcfg["defaults"].get("langfusePort") or _port(bobcfg, "langfusePort")
        out += ['  success_callback: ["langfuse"]', '  failure_callback: ["langfuse"]',
                f"  langfuse_host: http://localhost:{lf_port}",
                "  # langfuse_public_key and langfuse_secret_key: set as LANGFUSE_PUBLIC_KEY / "
                "LANGFUSE_SECRET_KEY env vars"]
    else:
        out += ["  # Enable Langfuse tracing: set langfuseEnabled = $true in config/user.json, then bob "
                "gen + bob litellm",
                "  # Set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY as environment variables (Settings → "
                "API Keys in Langfuse UI)"]
    out += ["", "general_settings:",
            "  drop_params: true      # silently drop unsupported params from clients (avoids 400s)",
            f"  master_key: {litellm_key}   # from the litellmKey seam; default sk-local — local-only proxy"]

    dest = _write(REPO / "config" / "litellm.yaml", "\n".join(out) + "\n")
    return f"Generated {dest}"


# --- gen-continue ---------------------------------------------------------------------------------

_ROLE_ASSIGN = {"coder": ["chat", "edit", "apply"], "chat": ["chat"], "ponder": ["chat", "edit"],
                "vision": ["chat"], "fim": ["autocomplete"], "embed": ["embed"]}
_PRO_ASSIGN = {"chat": ["chat", "edit"], "coder": ["chat", "edit", "apply"], "ponder": ["chat"],
               "vision": ["chat"]}
_NAME_FOR = {"fim": "autocomplete", "embed": "embeddings"}


def _yaml_str(s: str) -> str:
    """Double-quoted YAML scalar: backslash + quote escaped, newlines flattened to spaces."""
    import re
    e = str(s).replace("\\", "\\\\").replace('"', '\\"')
    e = re.sub(r"\r?\n", " ", e)
    return f'"{e}"'


def gen_continue(profile: str = None) -> str:
    """Generate config/continue/config.yaml."""
    import bob_models
    import osenv
    from bob_core import _port

    mcfg = bob_models.load_models_config()
    _, models = _ordered_models(mcfg, profile)
    peers = enabled_peers(mcfg)
    bobcfg = _bob_cfg()
    litellm_port = _port(bobcfg, "litellmPort")
    searxng_port = _port(bobcfg, "searxngPort")
    litellm_key = osenv.secret("litellmKey", default=bobcfg.get("litellmKey", "sk-local"), config=bobcfg)
    api_base = f"http://localhost:{litellm_port}/v1"
    home_dev = str(Path.home() / "dev")
    prompts = mcfg.get("prompts", {})

    out = ["# GENERATED - DO NOT EDIT.  Source: config/models.json  (+ config/user.json)",
           "# Regenerate: bob gen",
           "# Continue.dev config (2026 YAML format). Symlinked to ~/.continue/config.yaml during client setup.",
           "name: bob", "version: 0.0.1", "schema: v1", "", "models:"]

    def add_model(name, model, ctx, prompt, roles):
        out.append(f"  - name: {name}")
        out.append("    provider: openai")
        out.append(f"    model: {model}")
        out.append(f"    apiBase: {api_base}")
        out.append(f"    apiKey: {litellm_key}")
        if ctx > 0:
            out.append(f"    contextLength: {ctx}")
        if roles:
            out.append(f"    roles: [{', '.join(roles)}]")
        if prompt:
            out.append(f"    systemMessage: {_yaml_str(prompt)}")

    for m in models:
        if m["role"] == "agent" or m.get("reranking"):
            continue
        name = _NAME_FOR.get(m["role"], m["role"])
        ctx = 0 if m.get("embedding") else int(m.get("ctx") or 0)
        prompt = str(prompts.get(m["role"], "")) if prompts else ""
        roles = _ROLE_ASSIGN.get(m["role"], ["chat"])
        add_model(name, m["role"], ctx, prompt, roles)
        out.append("")

    for peer in peers:
        pro = peer.get("pro")
        if not pro:
            continue
        for role in sorted(pro):
            rv = pro[role]
            prompt = str(rv["systemPrompt"]) if isinstance(rv, dict) and rv.get("systemPrompt") else ""
            roles = _PRO_ASSIGN.get(role, ["chat"])
            add_model(f"{role}-pro", f"{role}-pro", 0, prompt, roles)
            out.append("")

    out += ["mcpServers:", "  - name: filesystem", "    command: npx", "    args:",
            '      - "-y"', '      - "@modelcontextprotocol/server-filesystem"',
            f"      - {_yaml_str(home_dev)}", f"      - {_yaml_str(str(REPO))}",
            "  - name: fetch", "    command: uvx", "    args:", '      - "mcp-server-fetch"',
            "  - name: github", "    command: npx", "    args:",
            '      - "-y"', '      - "@modelcontextprotocol/server-github"', "    env:",
            '      GITHUB_PERSONAL_ACCESS_TOKEN: "${GITHUB_TOKEN}"',
            "  - name: searxng-search", "    command: npx", "    args:",
            '      - "-y"', '      - "mcp-searxng"', "    env:",
            f'      SEARXNG_URL: "http://localhost:{searxng_port}"']

    dest = _write(REPO / "config" / "continue" / "config.yaml", "\n".join(out) + "\n")
    return f"Generated {dest}"


# --- gen-webui ------------------------------------------------------------------------------------

def gen_webui(profile: str = None) -> str:
    """Sync model system prompts into the Open WebUI sqlite db. Skips gracefully if the db is absent,
    or if WebUI holds the write lock."""
    import bob_models

    db_path = REPO / "tools" / "webui-data" / "webui.db"
    if not db_path.exists():
        return "gen-webui: webui.db not found — skipping (run 'bob webui' once to create it)"

    mcfg = bob_models.load_models_config()
    _, models = _ordered_models(mcfg, profile)
    peers = enabled_peers(mcfg)
    prompts = mcfg.get("prompts", {})

    entries = []
    for m in models:
        if m.get("embedding") or m.get("reranking") or m["role"] in ("fim", "embed"):
            continue
        entries.append({"id": m["role"], "prompt": str(prompts.get(m["role"], "")) if prompts else ""})
    for peer in peers:
        pro = peer.get("pro")
        if not pro:
            continue
        for role in sorted(pro):
            rv = pro[role]
            prompt = str(rv["systemPrompt"]) if isinstance(rv, dict) and rv.get("systemPrompt") else ""
            entries.append({"id": f"{role}-pro", "prompt": prompt})

    return _webui_write(str(db_path), entries)


def _webui_write(db_path: str, entries: list) -> str:
    """Write the prompt entries to webui.db. Short busy timeout so a running WebUI (holding the lock)
    makes us skip with a clear message rather than block. Preserves created_at on update."""
    import json
    import sqlite3
    import time

    lines = []
    try:
        db = sqlite3.connect(db_path, timeout=3)
        cur = db.cursor()
        cur.execute("SELECT id FROM user WHERE role='admin' LIMIT 1")
        row = cur.fetchone()
        if not row:
            db.close()
            return "gen-webui: no admin user found — skipping"
        admin_id = row[0]
        now_ms = int(time.time() * 1000)
        for e in entries:
            eid = e["id"]
            prompt = (e.get("prompt") or "").strip()
            params = json.dumps({"system": prompt}) if prompt else "{}"
            cur.execute(
                """INSERT OR REPLACE INTO model
                   (id, user_id, base_model_id, name, params, meta, updated_at, created_at, is_active)
                   VALUES (?,?,?,?,?,?,?,COALESCE((SELECT created_at FROM model WHERE id=?),?),1)""",
                (eid, admin_id, eid, eid, params, "{}", now_ms, eid, now_ms))
            lines.append(f"  {eid}: system prompt {'set' if prompt else 'cleared'}")
        db.commit()
        db.close()
    except sqlite3.OperationalError as ex:
        if "locked" in str(ex).lower():
            return ("gen-webui: webui.db is locked (Open WebUI running?) — skipping; re-run `bob gen` "
                    "after stopping WebUI.")
        raise
    return "Generated Open WebUI model system prompts\n" + "\n".join(lines)


# --- gen (all four) -------------------------------------------------------------------------------

def gen_all(profile: str = None) -> str:
    """Regenerate every runtime config from the registry. Port of the `gen` verb."""
    return "\n".join([gen_llama_swap(profile), gen_litellm(profile), gen_webui(profile),
                      gen_continue(profile)])


# --- agent tool adapter ---------------------------------------------------------------------------

def _gen() -> str:
    return gen_all()


def test() -> str:
    return gen_all()


TOOL_DEFS = [
    {"type": "function", "function": {
        "name": "gen",
        "description": ("Regenerate all runtime configs (llama-swap.yaml, litellm.yaml, Continue config, "
                        "Open WebUI prompts) from config/models.json. Run after changing the model "
                        "registry or profile. Mutating (writes config files)."),
        "parameters": {"type": "object", "properties": {}}}},
]

DISPATCH = {"gen": _gen}
