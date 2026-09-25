"""Bob config generators — regenerate the runtime configs from the neutral registry
(config/models.json via bob_models). One core fn per config reached the standard three ways;
`gen` runs them all.

  gen_llama_swap  -> config/llama-swap.yaml   (macros + per-model cmd assembly + swap group)
  gen_litellm     -> config/litellm.yaml      (local models via llama-swap + pro models via peers)
  gen_continue    -> config/continue/config.yaml
  gen_dsh         -> config/dsh/{settings.yaml,cordis.patch.yml} (DeepSeek Harness route + MCP entry)
  gen_webui       -> tools/webui-data/webui.db (model system prompts; skips if the db is absent)

`gen` also installs the dsh drop-ins into $DSH_HOME, skipping when dsh is not installed.

Deterministic + idempotent."""
import sys
from pathlib import Path

_cfg: dict = {}

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO / "scripts"

MUTATING_TOOLS = {"gen"}

# Canonical role order: ponder,coder,chat,fim,embed first, then the rest sorted.
_ROLE_ORDER = ["ponder", "coder", "chat", "writer", "fim", "embed"]


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
    # Always explicit: llama-server defaults to FOUR slots, and every slot gets its own KV (and, on a
    # hybrid attention/SSM model, its own recurrent-state cache) — measured at ~450 MiB of pure waste
    # for a single-user box. Bob serves one request at a time unless a model asks for more.
    par = f"-np {int(d['parallel'])}" if d.get("parallel") else "-np 1"
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
    aliases: dict = {}

    for m in models:
        _assert_no_quote(m.get("gguf", ""), f"model '{m['role']}' gguf")
        if "gemma" in m.get("gguf", "") and m.get("kv") is True:
            print(f"[{m['role']}] Gemma model with kv=true — KV quant causes quality regression.",
                  file=sys.stderr)
        if m.get("_aliasOf"):
            # Not a model of its own: it rides the target's server under llama-swap `aliases:`.
            aliases.setdefault(m["_aliasOf"], []).append(m["role"])
            continue
        if str(m.get("ngl", "")).lower() == "auto" and not is_cpu:
            # ngl="auto": omit -ngl entirely so llama.cpp sizes the offload to whatever VRAM is actually
            # free (common_fit_params). ANY explicit -ngl aborts that fit ("n_gpu_layers already set by
            # user ... abort"), so the srv macro's -ngl cannot ride along and the model gets its own
            # expansion. This is how a DENSE model larger than the card runs: --n-cpu-moe only helps a
            # MoE, and a hand-tuned layer count is wrong on every card but the one it was measured on.
            srv_ref = " ".join(p for p in [srv_bin, "--port ${PORT}", fa, reason, batch, ub, numa, par, thr] if p)
        else:
            srv_ref = "${srv}"
        parts = [srv_ref, f"-m ${{env.LLAMA_LOCAL_ROOT}}/models/{m['gguf']}"]
        if m.get("ctx") is not None:
            parts.append(f"-c {_fmt(m['ctx'])}")
        if m.get("kv"):
            # Per-model KV quant beats the profile-wide macro: a model held entirely in VRAM at a long
            # context can only afford q4_0, while a small one keeps q8_0's accuracy for free.
            mk = m.get("kvQuantK") or m.get("kvQuant")
            mv = m.get("kvQuantV") or m.get("kvQuant")
            if is_cpu:
                pass
            elif mk or mv:
                parts.append(f"--cache-type-k {mk or kv_k} --cache-type-v {mv or kv_v}")
            else:
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
        if by_role[mem].get("_aliasOf"):
            continue   # an alias is not a loadable model — its target carries the membership
        if by_role[mem].get("pinned"):
            raise RuntimeError(f"model '{mem}' is pinned but also listed in group.members — pinned models "
                               "must stay out of the swap group")
        active_members.append(mem)
    # A profile can push one more role into the swap group with "swap": true. That is how a tight tier
    # says "this model cannot stay resident next to the big one" without changing the shared member list.
    for m in models:
        if m.get("swap") is True and not m.get("_aliasOf") and m["role"] not in active_members:
            if m.get("pinned"):
                raise RuntimeError(f"model '{m['role']}' sets both pinned and swap")
            active_members.append(m["role"])

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
        if m.get("_aliasOf"):
            continue
        role_aliases = aliases.get(m["role"], [])
        out.append(f"  {m['role']}:")
        out.append(f'    cmd: "{m["_cmd"]}"')
        if role_aliases:
            out.append(f"    aliases: [{', '.join(role_aliases)}]")
        # Per-alias sampling: one loaded server, but a request that came in under `writer` still gets
        # the writer's temperature. setParamsByID is applied after setParams, so the target's own
        # defaults stay the baseline.
        by_id = {r: by_role[r]["setParams"] for r in role_aliases
                 if by_role[r].get("setParams") and by_role[r]["setParams"] != m.get("setParams")}
        if m.get("setParams") or by_id:
            out.append("    filters:")
            if m.get("setParams"):
                pairs = ", ".join(f"{k}: {_fmt(m['setParams'][k])}" for k in sorted(m["setParams"]))
                out.append(f"      setParams: {{ {pairs} }}")
            if by_id:
                out.append("      setParamsByID:")
                for r in sorted(by_id):
                    pairs = ", ".join(f"{k}: {_fmt(by_id[r][k])}" for k in sorted(by_id[r]))
                    out.append(f"        {r}: {{ {pairs} }}")
        if m.get("ttl") is not None:
            out.append(f"    ttl: {_fmt(m['ttl'])}")
        out.append("")
    out.append("groups:")
    out.append(f"  {mcfg['group']['name']}:")
    out.append(f"    swap: {_fmt(mcfg['group']['swap'])}")
    out.append(f"    members: [{', '.join(active_members)}]")
    # Everything Bob does not list as a swap member (fim/embed/rerank) would otherwise land in
    # llama-swap's implicit default group, which defaults to exclusive:true — so a single embedding
    # call for a memory lookup would evict the big chat model. Name the group instead and mark it
    # non-exclusive + persistent: these are small, always-wanted models that coexist with the swapper.
    resident = [m["role"] for m in models
                if not m.get("_aliasOf") and m["role"] not in active_members]
    if resident:
        out.append("  resident:")
        out.append("    swap: false")
        out.append("    exclusive: false")
        out.append("    persistent: true")
        out.append(f"    members: [{', '.join(resident)}]")

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
            # maxOutputTokens (role, else peer) is the default output cap for every client that sends
            # none; unset leaves the provider's own default, which is often far shorter.
            rv = rv if isinstance(rv, dict) else {"model": rv}
            model_id = rv.get("model")
            max_toks = rv.get("maxOutputTokens") or peer.get("maxOutputTokens")
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
                "writer": ["chat"], "vision": ["chat"], "fim": ["autocomplete"], "embed": ["embed"]}
_PRO_ASSIGN = {"chat": ["chat", "edit"], "coder": ["chat", "edit", "apply"], "ponder": ["chat"],
               "writer": ["chat"], "vision": ["chat"]}
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


# --- gen-dsh --------------------------------------------------------------------------------------

# 'agent' is Bob's own loop model; fim/embed/rerank are not chat models, so dsh has no use for them.
_DSH_SKIP_ROLES = {"agent", "fim", "embed", "rerank"}
_DSH_MCP_ID = "bob-tools"
_DSH_KEY_REF = "BOB_LITELLM_KEY"   # the credential name the route and the MCP header resolve
# Smallest per-request window worth offering a coding agent: pi-ai keeps 4096 tokens back as margin,
# and dsh's system prompt plus tool schemas take several thousand more before the first turn.
_DSH_MIN_CTX = 16384


def _dsh_home() -> Path:
    """The DeepSeek Harness data root, resolved dsh's way: $DSH_HOME when set and non-blank, else
    ~/.dsh (a blank value is treated as unset, never as the cwd)."""
    import os

    env = (os.environ.get("DSH_HOME") or "").strip()
    return Path(env).expanduser() if env else Path.home() / ".dsh"


def _slot_ctx(m: dict, defaults: dict) -> int:
    """The window ONE request gets from a llama-server: -c split across its slots unless the KV cache
    is unified. Slots come from the model's own --parallel (appended last, so it wins) or the
    defaults' `parallel`; a split is assumed unless --kv-unified is explicit, since overstating the
    window is the failure (dsh then overruns the slot before it compacts) and understating it is not."""
    flags = [str(f) for f in (m.get("flags") or [])]
    slots = int(defaults.get("parallel") or 1)
    for i, f in enumerate(flags[:-1]):
        if f in ("--parallel", "-np"):
            slots = int(flags[i + 1])
    ctx = int(m.get("ctx") or 0)
    if slots > 1 and not {"--kv-unified", "-kvu"} & set(flags):
        return ctx // slots
    return ctx


def _dsh_models(mcfg: dict, profile: str = None):
    """([(model_id, contextWindow|0, maxTokens|0, vision)], [skipped note]) for the dsh route: the
    local chat-capable roles, then each enabled peer's pro roles, first peer wins on a duplicate id.

    A local role whose per-request window is under _DSH_MIN_CTX is left out: pi-ai caps output at the
    window minus the prompt minus a fixed 4096-token margin, so on a small window every reply is
    clamped to a single token. A pro role takes contextWindow / maxOutputTokens from the role, else
    the peer; left unset, pi-ai's defaults stand. A pro role is image
    capable only when it says supportsVision, and a 'vision' pro role that is not is left out."""
    _, models = _ordered_models(mcfg, profile)
    defaults = mcfg.get("defaults") or {}
    out, skipped, seen = [], [], set()
    for m in models:
        if m["role"] in _DSH_SKIP_ROLES or m.get("embedding") or m.get("reranking"):
            continue
        ctx = _slot_ctx(m, defaults)
        if ctx < _DSH_MIN_CTX:
            skipped.append(f"{m['role']} ({ctx} ctx < {_DSH_MIN_CTX})")
            continue
        out.append((m["role"], ctx, 0, bool(m.get("supportsVision"))))
        seen.add(m["role"])
    for peer in enabled_peers(mcfg):
        for role in sorted(peer.get("pro") or {}):
            if role in _DSH_SKIP_ROLES:
                continue
            mid = f"{role}-pro"
            if mid in seen:
                continue
            rv = peer["pro"][role] if isinstance(peer["pro"][role], dict) else {}
            vision = bool(rv.get("supportsVision", peer.get("supportsVision")))
            if role == "vision" and not vision:
                skipped.append(f"{mid} ({rv.get('model')} takes no images)")
                continue
            ctx = int(rv.get("contextWindow") or peer.get("contextWindow") or 0)
            max_tokens = int(rv.get("maxOutputTokens") or peer.get("maxOutputTokens") or 0)
            out.append((mid, ctx, max_tokens, vision))
            seen.add(mid)
    return out, skipped


def _dsh_mcp_lines(bobcfg: dict) -> list:
    """The dsh MCP plugin-instance block for Bob's tool registry, for whichever transport
    agent.mcpTransport selects.

    stdio (the default): dsh spawns `bob agent mcp` as a child, so the harness must sit on the same
    machine as Bob. http: dsh connects to an already-running `bob agent mcp --http`, which is what
    lets a harness on another machine borrow a home Bob's tools. One generator for both, so the
    transport is chosen in config rather than by hand-editing the harness."""
    import osenv
    from bob_core import _port

    agent = bobcfg.get("agent", {}) or {}
    if (agent.get("mcpTransport") or "stdio").lower() == "http":
        host = agent.get("mcpHost", "127.0.0.1")
        # 0.0.0.0 is a bind address, not a reachable one: a remote harness needs a name it can dial,
        # so fall back to loopback and let agent.mcpUrl name the public address.
        if host in ("0.0.0.0", "::"):  # noqa: S104 — comparison, not a bind
            host = "127.0.0.1"
        url = agent.get("mcpUrl") or f"http://{host}:{_port(agent, 'mcpPort')}/mcp"
        return [
            "# Bob's tool registry as a dsh MCP server (Streamable HTTP). Appended to",
            "# $DSH_HOME/cordis.patch.yml by `bob gen` when agent.mcpEnabled is on. Bob must be serving",
            "# it: `bob agent mcp --http`. Set agent.mcpUrl when dsh runs on another machine.",
            "- insert:", f"    - id: {_DSH_MCP_ID}", "      name: '@deepseek-ai/dsh-mcp-client'",
            "      config:", "        serverName: bob", "        transport: http",
            f"        url: {_yaml_str(url)}", "        headers:",
            "          Authorization: !!js `Bearer ${process.env.BOB_LITELLM_KEY || 'sk-local'}`"]
    shim = "bob.cmd" if osenv.os_name() == "windows" else str(REPO / "bob")
    return [
        "# Bob's tool registry as a dsh MCP server (stdio). Appended to $DSH_HOME/cordis.patch.yml by",
        "# `bob gen` when agent.mcpEnabled is on. cwd is the harness's own, so Bob's file and git tools",
        "# act on the project dsh is open in, not on Bob's repo.",
        "- insert:", f"    - id: {_DSH_MCP_ID}", "      name: '@deepseek-ai/dsh-mcp-client'",
        "      config:", "        serverName: bob", "        transport: stdio",
        f"        command: {_yaml_str(shim)}", "        args: [agent, mcp]",
        "        cwd: !!js process.cwd()"]


def gen_dsh(profile: str = None) -> str:
    """Generate the DeepSeek Harness (dsh) drop-ins: config/dsh/settings.yaml (a pi-ai provider route
    pointing at Bob's LiteLLM proxy) and config/dsh/cordis.patch.yml (Bob's MCP server as a dsh plugin
    instance, so dsh gets Bob's tools). `install_dsh` merges them into $DSH_HOME."""
    import bob_models
    import osenv
    from bob_core import _port

    mcfg = bob_models.load_models_config()
    bobcfg = _bob_cfg()
    litellm_port = _port(bobcfg, "litellmPort")
    header = ["# GENERATED - DO NOT EDIT.  Source: config/models.json  (+ config/user.json)",
              "# Regenerate: bob gen"]

    out = header + [
        "# DeepSeek Harness provider route. Merged into $DSH_HOME/settings.yaml (default ~/.dsh) by",
        "# `bob gen`; dsh re-reads it on the next request, so nothing needs a restart.",
        "llm-pi-ai:", "  providers:", "    bob:",
        "      displayName: Bob (local)",
        "      api: openai-completions",
        f"      baseURL: http://localhost:{litellm_port}/v1",
        f"      apiKeyEnv: {_DSH_KEY_REF}",
        "      compat:",
        "        # llama.cpp chat templates know no 'developer' role, and llama-server caps output with",
        "        # max_tokens. pi-ai addresses an unrecognized endpoint as OpenAI itself, so both are set.",
        "        supportsDeveloperRole: false",
        "        maxTokensField: max_tokens",
        "      models:"]
    entries, skipped = _dsh_models(mcfg, profile)
    if not entries:
        out[-1] += " []"
    for mid, ctx, max_tokens, vision in entries:
        out.append(f"        - id: {mid}")
        if ctx > 0:
            out.append(f"          contextWindow: {ctx}")
        if max_tokens > 0:
            out.append(f"          maxTokens: {max_tokens}")
        if vision:
            out.append("          input: [text, image]")
    settings = _write(REPO / "config" / "dsh" / "settings.yaml", "\n".join(out) + "\n")

    patch = header + _dsh_mcp_lines(bobcfg)
    patch_file = _write(REPO / "config" / "dsh" / "cordis.patch.yml", "\n".join(patch) + "\n")
    note = f"\n  left out of the dsh route: {', '.join(skipped)}" if skipped else ""
    return f"Generated {settings}\nGenerated {patch_file}{note}"


def install_dsh() -> str:
    """Merge the generated drop-ins into $DSH_HOME. Skips gracefully when dsh is not installed, the
    same way gen_webui skips a missing webui.db, so `bob gen` is safe on a machine without it.

    settings.yaml is merged key-wise (dsh's Settings UI owns the rest of that document, so only the
    'bob' provider route is touched); cordis.patch.yml is appended to textually, because it may carry
    `!!js` tags a safe YAML load would reject."""
    home = _dsh_home()
    if not home.is_dir():
        return (f"install-dsh: no DeepSeek Harness home at {home} — skipping "
                "(run `npx @deepseek-ai/dsh web` once, then `bob gen`)")

    lines = [_install_dsh_settings(home), _install_dsh_credential(home)]
    if (_bob_cfg().get("agent", {}) or {}).get("mcpEnabled"):
        lines.append(_install_dsh_mcp(home))
    else:
        lines.append("  mcp: skipped — set agent.mcpEnabled true in config/user.json, then `bob gen`, "
                     "to give dsh Bob's tools")
    return "Installed dsh drop-ins\n" + "\n".join(lines)


def _install_dsh_settings(home: Path) -> str:
    """Write the 'bob' provider route into $DSH_HOME/settings.yaml, preserving every other provider
    and top-level section. Without PyYAML there is no safe merge, so an existing file is left alone."""
    src = REPO / "config" / "dsh" / "settings.yaml"
    dest = home / "settings.yaml"
    try:
        import yaml
    except ModuleNotFoundError:
        if "      models: []" in src.read_text(encoding="utf-8"):
            return "  settings: no model fits dsh on this profile, route not written"
        if dest.exists():
            return f"  settings: PyYAML not available to merge — copy the route from {src} by hand"
        dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        return f"  settings: wrote {dest}"

    route = yaml.safe_load(src.read_text(encoding="utf-8"))["llm-pi-ai"]["providers"]["bob"]
    existing = {}
    if dest.exists():
        try:
            existing = yaml.safe_load(dest.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as ex:
            return f"  settings: {dest} is not loadable YAML ({ex.__class__.__name__}) — left as-is"
        if not isinstance(existing, dict):
            return f"  settings: {dest} is not a mapping — left as-is"
    providers = existing.setdefault("llm-pi-ai", {}).setdefault("providers", {})
    if not route.get("models"):
        # pi-ai refuses a route that resolves no models, so a stale one is removed, not emptied.
        if providers.pop("bob", None) is None:
            return "  settings: no model fits dsh on this profile, route not written"
        dest.write_text(yaml.safe_dump(existing, sort_keys=False, allow_unicode=True), encoding="utf-8")
        return f"  settings: no model fits dsh on this profile, removed the 'bob' route from {dest}"
    unchanged = providers.get("bob") == route
    providers["bob"] = route
    if unchanged:
        return f"  settings: {dest} already current"
    dest.write_text(yaml.safe_dump(existing, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return f"  settings: merged the 'bob' route into {dest}"


def _install_dsh_credential(home: Path) -> str:
    """Store Bob's LiteLLM key in dsh's local credential store ($DSH_HOME/.credentials.yaml) under the
    name the route's apiKeyEnv references, so the route authenticates with nothing exported. dsh
    watches that file, so a running harness uses the key on its next request.

    Edited by line rather than re-dumped: dsh keeps comments in the file and refuses to load anything
    it cannot parse strictly, so only the one ref line is ever written."""
    import os
    import re
    from bob_core import _litellm_key

    dest = home / ".credentials.yaml"
    key = _litellm_key(_bob_cfg())
    if not dest.exists():
        dest.write_text(f"version: 1\n\nrefs:\n  {_DSH_KEY_REF}: {_yaml_str(key)}\n", encoding="utf-8")
        os.chmod(dest, 0o600)   # dsh refuses a credential file other users can read
        return f"  key: stored {_DSH_KEY_REF} in {dest}"

    lines = dest.read_text(encoding="utf-8").splitlines()
    refs_at = next((i for i, ln in enumerate(lines) if re.match(r"refs:\s*(#.*)?$", ln)), None)
    if refs_at is None:
        if any(ln.startswith("refs:") for ln in lines):
            return f"  key: {dest} writes refs inline; add {_DSH_KEY_REF} to it by hand"
        lines += ["", "refs:", f"  {_DSH_KEY_REF}: {_yaml_str(key)}"]
        verb = "stored"
    else:
        end = next((i for i in range(refs_at + 1, len(lines)) if re.match(r"\S", lines[i])
                    and not lines[i].startswith("#")), len(lines))
        section = range(refs_at + 1, end)
        hit = next((i for i in section if re.match(rf"\s+{_DSH_KEY_REF}\s*:", lines[i])), None)
        if hit is not None:
            indent, current = re.match(rf"(\s+){_DSH_KEY_REF}\s*:\s*(.*?)\s*$", lines[hit]).groups()
            if current in (key, _yaml_str(key), f"'{key}'"):
                return f"  key: {dest} already carries {_DSH_KEY_REF}"
            lines[hit] = f"{indent}{_DSH_KEY_REF}: {_yaml_str(key)}"
            verb = "updated"
        else:
            child = next((re.match(r"\s+", lines[i]).group() for i in section
                          if re.match(r"\s+[^\s#]", lines[i])), "  ")
            lines.insert(refs_at + 1, f"{child}{_DSH_KEY_REF}: {_yaml_str(key)}")
            verb = "stored"
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f"  key: {verb} {_DSH_KEY_REF} in {dest}"


def _install_dsh_mcp(home: Path) -> str:
    """Append Bob's MCP entry to $DSH_HOME/cordis.patch.yml unless it is already there. Textual, so
    a hand-written patch file keeps its comments and any `!!js` expressions."""
    src = REPO / "config" / "dsh" / "cordis.patch.yml"
    dest = home / "cordis.patch.yml"
    block = src.read_text(encoding="utf-8")
    if not dest.exists():
        dest.write_text(block, encoding="utf-8")
        return f"  mcp: wrote {dest}"
    current = dest.read_text(encoding="utf-8")
    if f"id: {_DSH_MCP_ID}" in current:
        return f"  mcp: {dest} already carries the '{_DSH_MCP_ID}' entry"
    entry = block[block.index("- insert:"):]
    sep = "" if current.endswith("\n") else "\n"
    dest.write_text(current + sep + "\n" + entry, encoding="utf-8")
    return f"  mcp: appended the '{_DSH_MCP_ID}' entry to {dest}"


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
                      gen_continue(profile), gen_dsh(profile), install_dsh()])


# --- agent tool adapter ---------------------------------------------------------------------------

def _gen() -> str:
    return gen_all()


def test() -> str:
    return gen_all()


TOOL_DEFS = [
    {"type": "function", "function": {
        "name": "gen",
        "description": ("Regenerate all runtime configs (llama-swap.yaml, litellm.yaml, Continue config, "
                        "DeepSeek Harness route, Open WebUI prompts) from config/models.json. Run after "
                        "changing the model registry or profile. Mutating (writes config files)."),
        "parameters": {"type": "object", "properties": {}}}},
]

DISPATCH = {"gen": _gen}
