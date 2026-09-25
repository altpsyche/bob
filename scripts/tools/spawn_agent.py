"""Bob tool: spawn_agent — delegate a subtask to an isolated nested agent run.

Composes the loop's other capabilities: parallel tool dispatch runs several spawn_agent calls in one
step concurrently (fan-out/fan-in) for free — spawn_agent is deliberately NOT mutating; the sub-run can
compact its own transcript; and it inherits the same allow|ask|deny policy + approver.

Gated on agent.subAgents (default false) so with the flag off the default toolset is unchanged. Reaches
the parent RunContext via tool_registry.get_run_context() — no fn-signature change. The sub-run gets:
  - an ISOLATED transcript (fresh system + the delegated subtask; the parent transcript is NOT inherited)
  - a RESTRICTED tool view (optional agent.subAgentTools whitelist)
  - a DEPTH CAP (agent.maxAgentDepth) — refuse a level past the cap (bounds recursion)
  - a CHILD cancel token (parent cancel propagates to the sub-run)
  - the parent's owner/scope (memory identity preserved) and a child run-id
and returns a STRUCTURED summary (result / steps / tools_used) — never the raw sub-transcript.

A sub-run must NOT consolidate or create a session: run_agent_events never does (consolidation is wired
only to session-end seams), and this tool calls run_agent_events directly, so a sub-run is safe.
"""
import json

_cfg: dict = {}


def enabled(config: dict) -> bool:
    """Feature gate (read by ToolRegistry): sub-agent delegation is only offered when
    agent.subAgents is on, so with it off the default toolset — and every prompt — is unchanged."""
    return bool(config.get("agent", {}).get("subAgents", False))


def configure(config: dict) -> None:
    global _cfg
    _cfg = config


def resolve_profile(role: str, agent_cfg: dict) -> dict:
    """Resolve a sub-agent `role` to a typed profile. When `role` names a key in agent.subAgentRoles,
    return that profile: {prompt, tools, modelRole}. Otherwise fall back to today's behavior, where
    `role` is only a model-role override (prompt/tools left to the defaults)."""
    roles = agent_cfg.get("subAgentRoles", {}) or {}
    profile = roles.get(role) if role else None
    if isinstance(profile, dict):
        return {"prompt": profile.get("prompt"),
                "tools": profile.get("tools"),
                "modelRole": profile.get("modelRole")}
    return {"prompt": None, "tools": None, "modelRole": role}


def local_roles(config: dict) -> tuple:
    """(local, pro) model roles as routing resolves them: `local` is every roleTable task's base role
    (plus the task names themselves), `pro` every *-pro variant. A pro role is a paid cloud peer."""
    from bob_core import get_role, load_defaults
    tasks = list(load_defaults().get("roleTable", {}))
    base = set(tasks) | {get_role(config, t) for t in tasks}
    pro = {get_role(config, t, pro=True) for t in tasks} - base
    pro |= {r for r in base if r and r.endswith("-pro")}
    return {r for r in base if r and r not in pro}, pro


def check_role_scope(role: str, allowed_roles):
    """None if the caller's role scopes (RunContext.allowed_roles, from the agent API token's
    `role:<name>` scopes) permit `role`, else a refusal. No scopes (None) or no explicit role means
    unrestricted, matching the agent API's own role gate."""
    if not role or not allowed_roles:
        return None
    if role in allowed_roles:
        return None
    return (f"spawn_agent: role '{role}' is not permitted for this caller "
            f"(allowed: {', '.join(sorted(allowed_roles))}).")


def check_model_role(role: str, config: dict):
    """None if the model may route a sub-run to `role`, else a refusal message. Only local roles are
    reachable unless agent.subAgentAllowPro is set, so a model-chosen role can never send a sub-run to
    a paid cloud peer on its own."""
    if not role:
        return None
    local, pro = local_roles(config)
    allow_pro = bool(config.get("agent", {}).get("subAgentAllowPro", False))
    is_pro = role in pro or role.endswith("-pro")
    if role in local and not is_pro:
        return None
    if is_pro and allow_pro:
        return None
    if is_pro:
        return (f"spawn_agent: role '{role}' is a cloud (pro) role; sub-agents run on local roles "
                f"unless agent.subAgentAllowPro is enabled. Local roles: {', '.join(sorted(local))}.")
    return f"spawn_agent: unknown role '{role}'. Local roles: {', '.join(sorted(local))}."


def _spawn_agent(task: str, role: str = None) -> str:
    from tool_registry import get_run_context

    ctx = get_run_context()
    if ctx is None:
        return "spawn_agent is unavailable outside an agent run."
    if not task or not task.strip():
        return "spawn_agent: 'task' must be a non-empty subtask description."

    config = getattr(ctx, "config", None) or _cfg
    agent_cfg = config.get("agent", {})
    parent_depth = int(getattr(ctx, "agent_depth", 0) or 0)
    max_depth = int(agent_cfg.get("maxAgentDepth", 2))
    if parent_depth + 1 > max_depth:
        # Bounds runaway recursion — the authoritative delegation limit.
        return (f"Delegation depth limit reached (maxAgentDepth={max_depth}); refusing to spawn a "
                f"sub-agent at depth {parent_depth + 1}. Complete this part of the task directly.")

    base_registry = getattr(ctx, "registry", None)
    if base_registry is None or not hasattr(base_registry, "filtered"):
        return "spawn_agent: no tool registry available for the sub-run."
    # A typed role (agent.subAgentRoles) gives a distinct prompt + per-role tool whitelist; otherwise
    # `role` is just a model-role override (back-compat). Per-role tools win when a profile is used, else
    # the flat agent.subAgentTools whitelist applies (None = inherit the full, already-restricted view).
    profile = resolve_profile(role, agent_cfg)
    # A typed profile's modelRole is user-authored config and is trusted; a bare `role` is chosen by the
    # model, so it is limited to local roles (see check_model_role).
    if not isinstance((agent_cfg.get("subAgentRoles") or {}).get(role), dict):
        refusal = check_model_role(role, config)
        if refusal:
            return refusal
    # The caller's token may be limited to some model roles; a sub-run can't reach past that.
    refusal = check_role_scope(profile["modelRole"], getattr(ctx, "allowed_roles", None))
    if refusal:
        return refusal
    allow = profile["tools"] or agent_cfg.get("subAgentTools") or None
    sub_registry = base_registry.filtered(allow=allow)

    from bob_loop import CancelToken, fold_events, run_agent_events

    parent_cancel = getattr(ctx, "cancel", None)
    child_cancel = parent_cancel.child() if isinstance(parent_cancel, CancelToken) else None
    parent_rid = getattr(ctx, "run_id", None) or "root"
    child_rid = f"{parent_rid}.sub{parent_depth + 1}"

    # fold_events never raises: a sub-run failure comes back as an error, not a crash of the parent step.
    out = fold_events(run_agent_events(
        task, config, role=profile["modelRole"], agency=agent_cfg.get("agency", "show"),
        registry=sub_registry, stream=False, history=None,
        cancel=child_cancel, run_id=child_rid, approve=getattr(ctx, "approve", None),
        owner=getattr(ctx, "owner", None), agent_depth=parent_depth + 1,
        scope=getattr(ctx, "scope", None),
        trace_parent=getattr(ctx, "trace_span", None),   # nest the sub-run under the parent
        system_prompt=profile["prompt"],                 # typed role -> distinct persona (else None)
        allowed_roles=getattr(ctx, "allowed_roles", None),
        unattended_allow=getattr(ctx, "unattended_allow", None),   # MCP's allow-set holds at every depth
    ))

    summary = {
        "result": out.result if out.result is not None else "(sub-agent produced no final answer)",
        "steps": out.steps,
        "tools_used": list(dict.fromkeys(t for t in out.tools_used if t)),
    }
    if out.error:
        summary["error"] = out.error
    return json.dumps(summary, ensure_ascii=False)


def test() -> str:
    # Outside a dispatched call there's no RunContext — exercises the graceful no-context path.
    return _spawn_agent("test subtask")


TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "spawn_agent",
            "description": ("Delegate a self-contained subtask to a fresh sub-agent that runs on its "
                            "own (it does NOT see this conversation), then returns a structured summary "
                            "of its result. Use for a chunk of work you can describe in one paragraph — "
                            "research, a multi-step lookup, or a parallelizable branch. Give it all the "
                            "context it needs in 'task', since it starts blank."),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string",
                             "description": "The subtask, self-contained (the sub-agent starts with no history)."},
                    "role": {"type": "string",
                             "description": ("Optional sub-agent profile or local model role for the "
                                             "sub-run (default: the agent role).")},
                },
                "required": ["task"],
            },
        },
    },
]

DISPATCH = {"spawn_agent": _spawn_agent}
