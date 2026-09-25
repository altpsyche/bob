"""Bob config generators — regenerate the runtime configs from the neutral registry
(config/models.json via bob_models). One core fn per config reached the standard three ways;
`gen` runs them all.

  gen_llama_swap  -> config/llama-swap.yaml   (macros + per-model cmd assembly + swap group)
  gen_litellm     -> config/litellm.yaml      (local models via llama-swap + pro models via peers)
  gen_continue    -> config/continue/config.yaml
  gen_dsh         -> config/dsh/{settings.yaml,cordis.patch.yml} (DeepSeek Harness route + MCP entry)
  gen_aider       -> config/aider/{.aider.conf.yml,model-metadata.json} (`bob aider` passes --config)
  gen_webui       -> tools/webui-data/webui.db (model system prompts; skips if the db is absent)

`gen` also installs the dsh drop-ins into $DSH_HOME, skipping when dsh is not installed.

Deterministic + idempotent."""
import os
import shutil
import sys
from pathlib import Path

_cfg: dict = {}

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO / "scripts"

MUTATING_TOOLS = {"gen"}

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))



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
    models = []
    for role in bob_models.ordered_roles(roles):
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


# Generated client configs that embed Bob's LiteLLM key or wire a client to it. The list lives in
# bob_fsguard (which refuses them to the file tools); on POSIX they are written 0600 so other local users
# cannot read them, and every other generated file keeps the umask default.
from bob_fsguard import KEY_BEARING as _KEY_BEARING  # noqa: E402


def _is_key_bearing(path: Path) -> bool:
    try:
        return path.resolve().relative_to(REPO.resolve()).as_posix() in _KEY_BEARING
    except ValueError:
        return False


def _write(path: Path, text: str) -> Path:
    """Write a generated file. A key-bearing one (_KEY_BEARING) is created, or re-moded before any byte is
    written, as 0600 on POSIX."""
    import osenv

    path.parent.mkdir(parents=True, exist_ok=True)
    if osenv.os_name() != "windows" and _is_key_bearing(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            os.fchmod(fh.fileno(), 0o600)   # an existing file keeps its old mode through O_CREAT
            fh.write(text)
        return path
    path.write_text(text, encoding="utf-8")
    return path


def _bob_cfg() -> dict:
    return _cfg or {}


# --- gen-llama-swap -------------------------------------------------------------------------------

# llama-server sampling flags. In `flags` they are only a server default that any client overrides, and on
# an aliased server they apply to every alias alike, so per-role sampling belongs in setParams instead.
_SAMPLING_FLAGS = ("--temp", "--top-p", "--top-k", "--min-p")


def sampling_flag_warnings(models: list) -> list:
    """One warning per sampling flag found in a (non-alias) model's `flags`."""
    out = []
    for m in models:
        if m.get("_aliasOf"):
            continue   # an alias carries its target's flags; the target is reported once
        for f in (m.get("flags") or []):
            if str(f) in _SAMPLING_FLAGS:
                out.append(f"[{m['role']}] sampling flag {f} in flags is a client-overridable server "
                           "default; put per-role sampling in setParams instead")
    return out


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
    for w in sampling_flag_warnings(models):
        print(w, file=sys.stderr)

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

# llama-swap takes no key, so the local upstreams carry a fixed placeholder rather than a copy of the
# proxy's master key.
_UPSTREAM_KEY = "none"


def _runtime(bobcfg: dict, key: str):
    """A top-level runtime key (bindHost, langfuseEnabled, n8nTimezone, ...): the resolved config's value,
    else its single default in config/defaults.json runtime."""
    if key in bobcfg:
        return bobcfg[key]
    from bob_core import load_defaults
    return load_defaults().get("runtime", {}).get(key)


def gen_litellm(profile: str = None) -> str:
    """Generate config/litellm.yaml."""
    import bob_models
    from bob_core import LITELLM_KEY_ENV, _port

    mcfg = bob_models.load_models_config()
    _, models = _ordered_models(mcfg, profile)
    peers = enabled_peers(mcfg)
    bobcfg = _bob_cfg()
    port = _port(bobcfg, "port")

    out = ["# GENERATED - DO NOT EDIT.  Source: config/models.json",
           "# Regenerate: bob gen  (also runs on `bob serve`)",
           "#",
           f"# SECURITY: the proxy's master key comes from {LITELLM_KEY_ENV} in its environment. `bob up`",
           "# always sets it. Run by hand (`litellm --config config/litellm.yaml`) without it and LiteLLM",
           "# only logs a warning and serves every request UNAUTHENTICATED. Start it with `bob up`, or export",
           f"# {LITELLM_KEY_ENV} first.", "", "model_list:"]
    for m in models:
        # Rerankers aren't OpenAI chat/embedding models — LiteLLM's /rerank expects a cohere/jina/infinity
        # provider, not openai/. The rerank call goes straight to llama-swap's native /v1/rerank instead.
        if m.get("reranking"):
            continue
        out += [f"  - model_name: {m['role']}", "    litellm_params:",
                f"      model: openai/{m['role']}", f"      api_base: http://127.0.0.1:{port}/v1",
                f"      api_key: {_UPSTREAM_KEY}"]
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

    langfuse = bool(_runtime(bobcfg, "langfuseEnabled"))
    if langfuse:
        out += ['  success_callback: ["langfuse"]', '  failure_callback: ["langfuse"]']
    else:
        out += ['  # Langfuse tracing is off. Set "langfuseEnabled": true at the top level of config/user.json,',
                "  # then `bob gen` and `bob restart`; the keys are generated and exported for you."]
    out += ["", "general_settings:",
            "  drop_params: true      # silently drop unsupported params from clients (avoids 400s)",
            "  # The master key is read from the proxy's environment: every Bob start passes LITELLM_MASTER_KEY",
            "  # from the litellmKey secret seam (generated on first use), so it is never written here. The",
            "  # proxy listens on bindHost, loopback (127.0.0.1) unless config/user.json opens it to the LAN.",
            f"  master_key: os.environ/{LITELLM_KEY_ENV}"]
    if langfuse:
        lf_port = _port(bobcfg, "langfusePort")
        out += ["", "# Langfuse credentials, read from the proxy's environment. `bob up` exports all three: the",
                "# generated project key pair, and LANGFUSE_HOST (http://127.0.0.1:" + str(lf_port) + " unless",
                "# LANGFUSE_HOST is already set, which is how a hosted Langfuse is used).",
                "environment_variables:",
                "  LANGFUSE_PUBLIC_KEY: os.environ/LANGFUSE_PUBLIC_KEY",
                "  LANGFUSE_SECRET_KEY: os.environ/LANGFUSE_SECRET_KEY",
                "  LANGFUSE_HOST: os.environ/LANGFUSE_HOST"]

    dest = _write(REPO / "config" / "litellm.yaml", "\n".join(out) + "\n")
    return f"Generated {dest}"


def routing_warnings(mcfg: dict, profile: str = None, bobcfg: dict = None) -> list:
    """One warning per roleTable route (routing.* / vision.*) whose role the active profile does not
    serve: a local role missing from the profile, or a `-pro` role no enabled peer offers. The runtime
    falls back to chat for coder/ponder/writer/agent and refuses a vision request it cannot serve."""
    from bob_core import load_defaults

    bobcfg = bobcfg if bobcfg is not None else _bob_cfg()
    name, models = _ordered_models(mcfg, profile)
    local = {m["role"] for m in models}
    pro = {f"{r}-pro" for p in enabled_peers(mcfg) for r in (p.get("pro") or {})}
    out, seen = [], set()
    for task, entry in load_defaults()["roleTable"].items():
        section_name = entry.get("section", "routing")
        section = bobcfg.get(section_name) or {}
        for key, fallback in ((entry["base"], entry["fallback"]), (entry["pro"], entry["proFallback"])):
            if (section_name, key) in seen:
                continue
            seen.add((section_name, key))
            role = section.get(key) or fallback
            if role in local or role in pro:
                continue
            if role.endswith("-pro"):
                why = "no enabled peer serves it"
            elif task == "vision" or role == "vision":
                why = f"profile '{name}' has no such role, so vision requests are refused"
            elif role == "chat":
                why = f"profile '{name}' has no chat role to fall back to"
            else:
                why = f"profile '{name}' has no such role, so Bob falls back to chat"
            out.append(f"routing: {section_name}.{key} = '{role}': {why}")
    return out


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


def _role_prompt(prompts: dict, role: str, rv=None) -> str:
    """The system prompt for `role`: one per role in the registry's `prompts`, which a pro role
    inherits unless its peer entry sets its own systemPrompt."""
    if isinstance(rv, dict) and rv.get("systemPrompt"):
        return str(rv["systemPrompt"])
    return str((prompts or {}).get(role, ""))


def _bob_shim() -> str:
    """The command a client spawns to run Bob: the Windows cmd shim, else the repo's `bob` script."""
    import osenv

    return "bob.cmd" if osenv.os_name() == "windows" else str(REPO / "bob")


def _have_npx() -> bool:
    """Whether Node's `npx` is on PATH (npx.cmd on Windows, which shutil.which resolves via PATHEXT)."""
    return shutil.which("npx") is not None


def gen_continue(profile: str = None) -> str:
    """Generate config/continue/config.yaml."""
    import bob_models
    from bob_core import _litellm_key, _port

    mcfg = bob_models.load_models_config()
    _, models = _ordered_models(mcfg, profile)
    defaults = mcfg.get("defaults") or {}
    peers = enabled_peers(mcfg)
    bobcfg = _bob_cfg()
    agent = bobcfg.get("agent", {}) or {}
    litellm_port = _port(bobcfg, "litellmPort")
    searxng_port = _port(bobcfg, "searxngPort")
    litellm_key = _litellm_key(bobcfg)
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
        # fim and embed fill Continue's own autocomplete and embed slots; every other non-chat or internal
        # role is left out.
        if m["role"] not in _NAME_FOR and not bob_models.is_chat_role(m["role"], m):
            continue
        name = _NAME_FOR.get(m["role"], m["role"])
        ctx = 0 if m.get("embedding") else _slot_ctx(m, defaults)
        roles = _ROLE_ASSIGN.get(m["role"], ["chat"])
        add_model(name, m["role"], ctx, _role_prompt(prompts, m["role"]), roles)
        out.append("")

    for peer in peers:
        pro = peer.get("pro")
        if not pro:
            continue
        for role in sorted(pro):
            if not bob_models.is_chat_role(role):
                continue
            roles = _PRO_ASSIGN.get(role, ["chat"])
            add_model(f"{role}-pro", f"{role}-pro", 0, _role_prompt(prompts, role, pro[role]), roles)
            out.append("")

    # The npx-launched servers need Node.js; without npx on PATH Continue would fail to spawn them, so they
    # are left out (and named in the returned notice) until Node is installed and `bob gen` runs again.
    has_npx = _have_npx()
    skipped = []
    out.append("mcpServers:")
    if has_npx:
        out += ["  - name: filesystem", "    command: npx", "    args:",
                '      - "-y"', '      - "@modelcontextprotocol/server-filesystem"',
                f"      - {_yaml_str(home_dev)}", f"      - {_yaml_str(str(REPO))}"]
    else:
        skipped.append("filesystem")
    out += ["  - name: fetch", "    command: uvx", "    args:", '      - "mcp-server-fetch"']
    if has_npx:
        out += ["  - name: github", "    command: npx", "    args:",
                '      - "-y"', '      - "@modelcontextprotocol/server-github"', "    env:",
                '      GITHUB_PERSONAL_ACCESS_TOKEN: "${GITHUB_TOKEN}"']
    else:
        skipped.append("github")
    if (agent.get("searchProvider") or "").lower() == "searxng":
        if has_npx:
            out += ["  - name: searxng-search", "    command: npx", "    args:",
                    '      - "-y"', '      - "mcp-searxng"', "    env:",
                    f'      SEARXNG_URL: "http://localhost:{searxng_port}"']
        else:
            skipped.append("searxng-search")
    if agent.get("mcpEnabled"):
        # Bob's own tool registry over stdio. No cwd: Continue starts the server in the open workspace,
        # so Bob's file and git tools act on that project rather than on Bob's repo.
        out += ["  - name: bob", f"    command: {_yaml_str(_bob_shim())}", "    args:",
                '      - "agent"', '      - "mcp"']

    dest = _write(REPO / "config" / "continue" / "config.yaml", "\n".join(out) + "\n")
    if skipped:
        return (f"Generated {dest}\n  notice: npx not found (Node.js), so Continue's {', '.join(skipped)} MCP "
                "server(s) were left out. Install Node.js, then `bob gen`.")
    return f"Generated {dest}"


# --- gen-dsh --------------------------------------------------------------------------------------

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
    window is the failure (dsh then overruns the slot before it compacts) and understating it is not.
    One implementation, shared with the agent's budget: bob_core.slot_ctx."""
    from bob_core import slot_ctx
    return slot_ctx(m, defaults)


def _dsh_models(mcfg: dict, profile: str = None):
    """([(model_id, contextWindow|0, maxTokens|0, vision)], [skipped note]) for the dsh route: the
    local chat-capable roles, then each enabled peer's pro roles, first peer wins on a duplicate id.

    A local role whose per-request window is under _DSH_MIN_CTX is left out: pi-ai caps output at the
    window minus the prompt minus a fixed 4096-token margin, so on a small window every reply is
    clamped to a single token. A pro role takes contextWindow / maxOutputTokens from the role, else
    the peer; left unset, pi-ai's defaults stand. A pro role is image
    capable only when it says supportsVision, and a 'vision' pro role that is not is left out."""
    import bob_models

    _, models = _ordered_models(mcfg, profile)
    defaults = mcfg.get("defaults") or {}
    out, skipped, seen = [], [], set()
    for m in models:
        if not bob_models.is_chat_role(m["role"], m):
            continue
        ctx = _slot_ctx(m, defaults)
        if ctx < _DSH_MIN_CTX:
            skipped.append(f"{m['role']} ({ctx} ctx < {_DSH_MIN_CTX})")
            continue
        out.append((m["role"], ctx, 0, bool(m.get("supportsVision"))))
        seen.add(m["role"])
    for peer in enabled_peers(mcfg):
        for role in sorted(peer.get("pro") or {}):
            if not bob_models.is_chat_role(role):
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
    from bob_core import _port

    agent = bobcfg.get("agent", {}) or {}
    if (agent.get("mcpTransport") or "stdio").lower() == "http":
        host = agent.get("mcpHost", "127.0.0.1")
        # 0.0.0.0 is a bind address, not a reachable one: a remote harness needs a name it can dial,
        # so fall back to loopback and let agent.mcpUrl name the public address.
        if host in ("0.0.0.0", "::"):  # noqa: S104 — comparison, not a bind
            host = "127.0.0.1"
        url = agent.get("mcpUrl") or f"http://{host}:{_port(agent, 'mcpPort')}/mcp"
        # The bearer is read from dsh's environment, never written here: this file is a plain config
        # file, not dsh's owner-only credential store, and a `!!js` expression cannot read that store.
        # With BOB_LITELLM_KEY unset the header is empty and Bob answers 401, so a missing key fails
        # closed instead of falling back to a guessable one.
        return [
            "# Bob's tool registry as a dsh MCP server (Streamable HTTP). Written into",
            "# $DSH_HOME/cordis.patch.yml by `bob gen` when agent.mcpEnabled is on. Bob must be serving",
            "# it: `bob agent mcp --http`. Set agent.mcpUrl when dsh runs on another machine, and export",
            "# BOB_LITELLM_KEY (Bob's LiteLLM key) in the environment dsh starts from.",
            "- insert:", f"    - id: {_DSH_MCP_ID}", "      name: '@deepseek-ai/dsh-mcp-client'",
            "      config:", "        serverName: bob", "        transport: http",
            f"        url: {_yaml_str(url)}", "        headers:",
            "          Authorization: !!js `Bearer ${process.env.BOB_LITELLM_KEY ?? ''}`"]
    shim = _bob_shim()
    return [
        "# Bob's tool registry as a dsh MCP server (stdio). Written into $DSH_HOME/cordis.patch.yml by",
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


def _top_level_items(lines: list) -> list:
    """(start, end) line spans of each top-level `- ` item of a YAML sequence document. An item ends
    where the next one begins, less any comment or blank lines that lead into that next item."""
    starts = [i for i, ln in enumerate(lines) if ln.startswith("- ")]
    spans = []
    for n, i in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        while end > i + 1 and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")):
            end -= 1
        spans.append((i, end))
    return spans


def _install_dsh_mcp(home: Path) -> str:
    """Put Bob's MCP entry into $DSH_HOME/cordis.patch.yml: written when the file is new, replaced in
    place when a `bob-tools` entry is already there (so a changed agent.mcpTransport reaches dsh), else
    appended. Textual, so a hand-written patch file keeps its comments, its other entries and any `!!js`
    expressions."""
    import re

    src = REPO / "config" / "dsh" / "cordis.patch.yml"
    dest = home / "cordis.patch.yml"
    block = src.read_text(encoding="utf-8")
    if not dest.exists():
        dest.write_text(block, encoding="utf-8")
        return f"  mcp: wrote {dest}"
    entry = block[block.index("- insert:"):].rstrip("\n").split("\n")
    lines = dest.read_text(encoding="utf-8").split("\n")
    id_line = re.compile(rf"\s*-\s+id:\s*['\"]?{re.escape(_DSH_MCP_ID)}['\"]?\s*(#.*)?$")
    for start, end in _top_level_items(lines):
        item = lines[start:end]
        if not any(id_line.match(ln) for ln in item):
            continue
        if sum(1 for ln in item if re.match(r"\s*-\s+id:", ln)) > 1:
            return (f"  mcp: {dest} holds the '{_DSH_MCP_ID}' entry inside an insert with other entries; "
                    f"update it by hand from {src}")
        if item == entry:
            return f"  mcp: {dest} already carries the current '{_DSH_MCP_ID}' entry"
        lines[start:end] = entry
        dest.write_text("\n".join(lines), encoding="utf-8")
        return f"  mcp: replaced the '{_DSH_MCP_ID}' entry in {dest}"
    current = "\n".join(lines)
    sep = "" if current.endswith("\n") else "\n"
    dest.write_text(current + sep + "\n" + "\n".join(entry) + "\n", encoding="utf-8")
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
        if not bob_models.is_chat_role(m["role"], m):
            continue
        entries.append({"id": m["role"], "prompt": _role_prompt(prompts, m["role"])})
    for peer in peers:
        pro = peer.get("pro")
        if not pro:
            continue
        for role in sorted(pro):
            if bob_models.is_chat_role(role):
                entries.append({"id": f"{role}-pro", "prompt": _role_prompt(prompts, role, pro[role])})

    lines = [_webui_write(str(db_path), entries)]
    key_line = webui_sync_key(db_path)
    if key_line:
        lines.append(key_line)
    return "\n".join(lines)


# The Open WebUI config rows that hold Bob's LiteLLM connection: the chat connections (parallel lists of
# base URLs and keys) and the embedding connection.
_WEBUI_KEY_ROWS = ("openai.api_base_urls", "openai.api_keys", "rag.openai.api_base_url", "rag.openai.api_key")


def webui_sync_key(db_path, config: dict = None) -> str:
    """Point the connections Open WebUI stored for Bob's LiteLLM at the current key. With persistent config
    (its default) WebUI keeps these in its own db, where they win over the OPENAI_API_KEY / RAG_OPENAI_API_KEY
    Bob starts it with, so a changed key would otherwise never reach it. Only a connection whose base URL is
    Bob's proxy (localhost / 127.0.0.1 on litellmPort) is touched; any other connection the user added keeps
    its key. Returns a status line, or "" when the db is absent or already current. A locked db (WebUI
    running) is skipped with a warning after a short busy wait."""
    import json
    import sqlite3
    import time

    from bob_core import _litellm_key, _port

    path = Path(db_path)
    if not path.exists():
        return ""
    cfg = config if config is not None else _bob_cfg()
    key = _litellm_key(cfg)
    port = _port(cfg, "litellmPort")
    bob_urls = {f"http://{host}:{port}/v1" for host in ("localhost", "127.0.0.1")}

    def ours(url) -> bool:
        return isinstance(url, str) and url.rstrip("/") in bob_urls

    def load(raw):
        try:
            return json.loads(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    try:
        db = sqlite3.connect(str(path), timeout=2)
        try:
            marks = ",".join("?" * len(_WEBUI_KEY_ROWS))
            rows = {k: load(v) for k, v in
                    db.execute(f"SELECT key, value FROM config WHERE key IN ({marks})", _WEBUI_KEY_ROWS)}
            updates = {}
            urls, keys = rows.get("openai.api_base_urls"), rows.get("openai.api_keys")
            if isinstance(urls, list) and isinstance(keys, list):
                new = list(keys)
                for i, url in enumerate(urls):
                    if ours(url):
                        new += [""] * (i + 1 - len(new))
                        new[i] = key
                if new != keys:
                    updates["openai.api_keys"] = new
            if ours(rows.get("rag.openai.api_base_url")) and rows.get("rag.openai.api_key") != key:
                updates["rag.openai.api_key"] = key
            if updates:
                now = int(time.time())
                with db:
                    for k, v in updates.items():
                        db.execute("UPDATE config SET value=?, updated_at=? WHERE key=?", (json.dumps(v), now, k))
        finally:
            db.close()
    except sqlite3.OperationalError as ex:
        msg = str(ex).lower()
        if "no such table" in msg:
            return ""
        if "locked" in msg or "busy" in msg:
            return ("warning: Open WebUI's db is locked, so its stored LiteLLM key was not checked; it is "
                    "updated on the next start (or `bob gen` with WebUI stopped).")
        return f"warning: could not update Open WebUI's stored LiteLLM key ({ex})"
    if not updates:
        return ""
    line = f"Open WebUI: updated the stored LiteLLM key ({', '.join(sorted(updates))})"
    import osenv
    if osenv.is_port_in_use(_port(cfg, "webuiPort")):
        # A running WebUI read these rows at start and keeps using the old key until it restarts.
        line += ("; Open WebUI is running and keeps the old key until it restarts "
                 "(run `bob stop`, then `bob up`)")
    return line


def _webui_write(db_path: str, entries: list) -> str:
    """Write the prompt entries to webui.db. Bob owns only params.system: an existing model row keeps
    its name, meta, other params and active flag, and gets just that key set or removed; a missing row
    is created. Short busy timeout so a running WebUI (holding the lock) makes us skip with a clear
    message rather than block."""
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
            cur.execute("SELECT params FROM model WHERE id=?", (eid,))
            existing = cur.fetchone()
            if existing is None:
                params = json.dumps({"system": prompt}) if prompt else "{}"
                cur.execute(
                    """INSERT INTO model
                       (id, user_id, base_model_id, name, params, meta, updated_at, created_at, is_active)
                       VALUES (?,?,?,?,?,?,?,?,1)""",
                    (eid, admin_id, eid, eid, params, "{}", now_ms, now_ms))
            else:
                try:
                    params = json.loads(existing[0] or "{}")
                except (TypeError, ValueError):
                    params = {}
                params = params if isinstance(params, dict) else {}
                if prompt:
                    params["system"] = prompt
                else:
                    params.pop("system", None)
                cur.execute("UPDATE model SET params=?, updated_at=? WHERE id=?",
                            (json.dumps(params), now_ms, eid))
            lines.append(f"  {eid}: system prompt {'set' if prompt else 'cleared'}")
        db.commit()
        db.close()
    except sqlite3.OperationalError as ex:
        if "locked" in str(ex).lower():
            return ("gen-webui: webui.db is locked (Open WebUI running?) — skipping; re-run `bob gen` "
                    "after stopping WebUI.")
        raise
    return "Generated Open WebUI model system prompts\n" + "\n".join(lines)


# --- gen-aider ------------------------------------------------------------------------------------

def _aider_map_tokens(window: int) -> int:
    """aider's repo-map budget for a `window`-token model: a sixteenth of it in 256-token steps,
    between 512 and 4096, so the map never crowds out the conversation on a small window."""
    return max(512, min(4096, (window // 16) // 256 * 256))


def gen_aider(profile: str = None) -> str:
    """Generate config/aider/.aider.conf.yml (architect mode: ponder plans, coder edits, both falling
    back to chat when the profile lacks them) and config/aider/model-metadata.json (the per-request
    context windows aider cannot know for Bob's role names). `bob aider` passes --config."""
    import json

    import bob_models
    from bob_core import _litellm_key, _port

    mcfg = bob_models.load_models_config()
    name, models = _ordered_models(mcfg, profile)
    defaults = mcfg.get("defaults") or {}
    by_role = {m["role"]: m for m in models}
    bobcfg = _bob_cfg()
    if "chat" not in by_role:
        raise RuntimeError(f"profile '{name}' has no chat role, which aider needs as its fallback model")
    architect = "ponder" if "ponder" in by_role else "chat"
    editor = "coder" if "coder" in by_role else "chat"
    windows = {r: _slot_ctx(by_role[r], defaults) for r in {architect, editor}}
    aider_dir = REPO / "config" / "aider"
    metadata_file = aider_dir / "model-metadata.json"

    out = ["# GENERATED - DO NOT EDIT.  Source: config/models.json  (+ config/user.json)",
           "# Regenerate: bob gen.  Used by `bob aider`, which passes --config with this file.",
           f"# Active profile: {name}",
           "# Local OpenAI-compatible models are prefixed openai/ (case-sensitive).", ""]
    if architect != editor:
        out += ["architect: true", f"model: openai/{architect}               # plans the change",
                f"editor-model: openai/{editor}          # writes the edits", "editor-edit-format: diff",
                "auto-accept-architect: false        # review the plan before edits are applied"]
    else:
        out += ["architect: false", f"model: openai/{architect}", "edit-format: diff"]
    out += [f"openai-api-base: http://127.0.0.1:{_port(bobcfg, 'litellmPort')}/v1",
            f"openai-api-key: {_yaml_str(_litellm_key(bobcfg))}",
            f"model-metadata-file: {_yaml_str(str(metadata_file))}",
            f"map-tokens: {_aider_map_tokens(min(windows.values()))}"
            "                    # sized to the smaller per-request window"]
    conf = _write(aider_dir / ".aider.conf.yml", "\n".join(out) + "\n")

    meta = {f"openai/{r}": {"max_input_tokens": w, "max_tokens": w, "input_cost_per_token": 0,
                            "output_cost_per_token": 0, "litellm_provider": "openai", "mode": "chat"}
            for r, w in sorted(windows.items())}
    meta_dest = _write(metadata_file, json.dumps(meta, indent=2) + "\n")
    return f"Generated {conf}\nGenerated {meta_dest}"


# --- LiteLLM key drift ----------------------------------------------------------------------------

# The key-bearing configs that carry the LiteLLM key on a line of their own, and the generator that writes
# each. master_key (litellm.yaml) must hold the environment reference the proxy reads the key from; apiKey
# (Continue) and openai-api-key (aider) must hold the resolved key itself.
_KEY_FILE_GENERATORS = {"config/litellm.yaml": "gen_litellm", "config/continue/config.yaml": "gen_continue",
                        "config/aider/.aider.conf.yml": "gen_aider"}


def stale_key_files(config: dict = None) -> list:
    """Repo-relative key-bearing configs (of those that exist) whose embedded LiteLLM key is not the one
    bob_core._litellm_key resolves now, or whose litellm.yaml writes a literal master key instead of the
    environment reference. A text check only: no network, nothing written."""
    import re

    from bob_core import LITELLM_KEY_ENV, _litellm_key

    key = _litellm_key(config if config is not None else _bob_cfg())
    line = re.compile(r"^\s*(master_key|apiKey|openai-api-key):\s*(.*?)\s*$", re.M)
    stale = []
    for rel in _KEY_FILE_GENERATORS:
        try:
            text = (REPO / rel).read_text(encoding="utf-8")
        except OSError:
            continue
        for field, value in line.findall(text):
            want = f"os.environ/{LITELLM_KEY_ENV}" if field == "master_key" else key
            if value.strip("'\"") != want:
                stale.append(rel)
                break
    return stale


def refresh_stale_key_files(config: dict = None) -> list:
    """Regenerate every stale_key_files entry with its own generator, so clients stop sending a key the
    proxy no longer accepts (an upgrade from the fixed key, or a rotated secret). One line per file
    regenerated; [] when all are current. A generator that raises is reported, never propagated."""
    if config is not None:
        configure(config)
    lines = []
    for rel in stale_key_files():
        try:
            globals()[_KEY_FILE_GENERATORS[rel]]()
            lines.append(f"Regenerated {rel} (it carried an outdated LiteLLM key)")
        except Exception as e:  # noqa: BLE001 (best-effort: the stack still starts)
            lines.append(f"warning: could not regenerate {rel} ({e}); run: bob gen")
    return lines


def refresh_dsh_credential(config: dict = None) -> str:
    """Bring the LiteLLM key in dsh's credential store ($DSH_HOME/.credentials.yaml) to the current one,
    the way `bob gen` stores it (_install_dsh_credential, line-edited). Runs on every stack start, so a
    rotated key reaches dsh without a `bob gen`. Nothing is created: returns "" when dsh has no home or no
    credential store yet, or the stored value is already current; else the status line."""
    if config is not None:
        configure(config)
    home = _dsh_home()
    if not home.is_dir() or not (home / ".credentials.yaml").exists():
        return ""
    line = _install_dsh_credential(home).strip()
    return "" if "already carries" in line else f"dsh: {line.removeprefix('key: ')}"


def refresh_fabric_env() -> str:
    """Re-point fabric at the current LiteLLM key when its .env already routes the LiteLLM vendor to Bob
    but holds another key, and migrate a .env that still holds the OPENAI_* pair an earlier setup wrote
    (sk-local at a localhost proxy), which fabric_run's `--vendor LiteLLM` cannot use. fabric-setup writes
    that .env; this keeps it current on `bob gen`. Returns a status line, or "" when there is no fabric
    .env, it is not Bob's route, or it is already current."""
    import osenv
    from bob_core import _litellm_key, _port

    import build

    path = osenv.home_config_dir("fabric") / ".env"
    if not path.exists():
        return ""
    current = {}
    for ln in build._read_env_file(path):
        k, sep, v = ln.partition("=")
        if sep and not ln.lstrip().startswith("#"):
            current[k.strip()] = v.strip()
    port = _port(_bob_cfg(), "litellmPort")
    key = _litellm_key(_bob_cfg())
    if build.fabric_env_is_legacy(current):
        build.merge_fabric_env(path, port, key)
        return f"Migrated fabric's .env to the LiteLLM vendor in {path}"
    if current.get("LITELLM_API_BASE_URL") != f"http://localhost:{port}/v1":
        return ""
    if current.get("LITELLM_API_KEY") == key:
        return ""
    build.merge_fabric_env(path, port, key)
    return f"Updated fabric's LiteLLM key in {path}"


# --- gen (all) ------------------------------------------------------------------------------------

# The generators that write files only (no install step, no Open WebUI db), in the order `gen` runs them.
_FILE_GENERATORS = ("gen_llama_swap", "gen_litellm", "gen_continue", "gen_dsh", "gen_aider")


def gen_all(profile: str = None) -> str:
    """Regenerate every runtime config from the registry, install the dsh drop-ins, and report any
    route the profile cannot serve. Port of the `gen` verb."""
    import bob_models

    lines = [gen_llama_swap(profile), gen_litellm(profile), gen_webui(profile),
             gen_continue(profile), gen_dsh(profile), gen_aider(profile), install_dsh()]
    fabric = refresh_fabric_env()
    if fabric:
        lines.append(fabric)
    lines += routing_warnings(bob_models.load_models_config(), profile)
    return "\n".join(lines)


def render_all(profile: str = None) -> dict:
    """{repo-relative path: text} for every file the generators would write, rendered in memory: nothing
    under config/ is touched, Open WebUI's db is not opened and $DSH_HOME is not read or written."""
    global _write
    captured = {}

    def _capture(path: Path, text: str) -> Path:
        captured[str(path.relative_to(REPO)).replace("\\", "/")] = text
        return path

    real = _write
    _write = _capture
    try:
        for fn in _FILE_GENERATORS:
            globals()[fn](profile)
    finally:
        _write = real
    return captured


# --- agent tool adapter ---------------------------------------------------------------------------

def _gen() -> str:
    return gen_all()


def test() -> str:
    """Render every generated file in memory and check the YAML ones parse; writes nothing."""
    import bob_models

    files = render_all()
    try:
        import yaml
    except ModuleNotFoundError:
        yaml = None
    for rel, text in files.items():
        if yaml is not None and rel.endswith((".yaml", ".yml")) and "!!js" not in text:
            yaml.safe_load(text)
    notes = routing_warnings(bob_models.load_models_config())
    return "\n".join([f"rendered {len(files)} files in memory: {', '.join(sorted(files))}"] + notes)


TOOL_DEFS = [
    {"type": "function", "function": {
        "name": "gen",
        "description": ("Regenerate all runtime configs (llama-swap.yaml, litellm.yaml, Continue config, "
                        "DeepSeek Harness route, aider config, Open WebUI prompts) from config/models.json. "
                        "Run after changing the model registry or profile. Mutating (writes config files)."),
        "parameters": {"type": "object", "properties": {}}}},
]

DISPATCH = {"gen": _gen}
