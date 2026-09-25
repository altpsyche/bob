"""Permission policy, resolved from config and enforced at the dispatch choke point.

Turns the binary approval *mechanism* (a tool self-declaring REQUIRES_APPROVAL, or the whole run
being in ``agency='confirm'``) into a real per-tool ``allow | ask | deny`` *policy*, overridable
per-owner (by identity) and per-agent-depth (a sub-agent may be more restricted than the root).

Default reproduces today's behavior. An **absent or empty** ``agent.permissions`` config resolves every
tool to ``allow``, so the only thing that still prompts is the approval floor (``agency='confirm'`` or a
tool's ``REQUIRES_APPROVAL``) and nothing is ever denied. A configured policy
*adds* ``deny`` and can *promote* ``allow -> ask``; it never weakens the approval floor
(``dispatch_with_approval`` keeps that as a lower bound).

Config shape (``config/defaults.json`` -> ``runtime.agent.permissions``; all keys optional)::

    "permissions": {
      "read":     "allow",                       # class default for non-mutating tools
      "mutating": "ask",                          # class default for tools in registry.mutating_tools
      "tools":    { "shell_run": "ask", "x": "deny" },   # per-tool, wins over the class default
      "perOwner": { "guest": { "mutating": "deny" } },   # same shape, per owner id
      "perDepth": { "1": { "mutating": "deny" } }        # same shape, per agent_depth (str key)
    }

Resolution precedence (most specific first): ``perDepth[depth]`` -> ``perOwner[owner]`` -> top level;
within each, a per-tool entry wins over the ``mutating``/``read`` class default. First hit wins; no hit
anywhere -> ``allow``. ``PermissionPolicy`` is pure (no registry/LLM dependency) so it unit-tests in
isolation — the caller passes the ``mutating`` bool it read from ``registry.mutating_tools``.

This module is also the ONE approval gate every front door dispatches through: the agent loop, the MCP
server (stdio and HTTP), ``bob --run`` and skill ``steps`` all call ``dispatch_with_approval`` (a
generator yielding the protocol events) or its blocking wrapper ``run_gated``. Non-interactive surfaces
pass no approver, which fails closed.
"""
import hashlib
import json
import logging

ALLOW = "allow"
ASK = "ask"
DENY = "deny"
_MODES = (ALLOW, ASK, DENY)


def _lookup(scope: dict, tool: str, mutating: bool):
    """Resolve one scope dict: a per-tool entry wins, else the mutating/read class default, else None."""
    if not isinstance(scope, dict):
        return None
    tools = scope.get("tools")
    if isinstance(tools, dict) and tool in tools:
        return tools[tool]
    key = "mutating" if mutating else "read"
    return scope.get(key)


class PermissionPolicy:
    """An allow|ask|deny policy resolved from ``agent.permissions``. Empty config -> everything ``allow``."""

    __slots__ = ("_perms", "_per_owner", "_per_depth", "configured")

    def __init__(self, config: dict = None):
        perms = ((config or {}).get("agent", {}) or {}).get("permissions", {}) or {}
        if not isinstance(perms, dict):
            perms = {}
        self._perms = perms
        self._per_owner = perms.get("perOwner", {}) or {}
        self._per_depth = perms.get("perDepth", {}) or {}
        # True when a non-empty policy is configured — lets callers cheaply skip resolution.
        self.configured = bool(perms)

    def resolve(self, tool: str, owner: str = "local", agent_depth: int = 0,
                mutating: bool = False, default: str = ALLOW) -> str:
        """Return 'allow' | 'ask' | 'deny' for this call. ``default`` is the mode used when nothing in
        the policy matches (and when the policy is empty) — 'allow' preserves the default behavior, while
        a remote MCP tool passes 'ask' so it prompts unless a config rule overrides it. Unknown/
        malformed modes fall through to ``default`` (a config typo can't silently deny reads)."""
        if not self.configured:
            return default
        for scope in (self._per_depth.get(str(agent_depth)),
                      self._per_owner.get(owner),
                      self._perms):
            mode = _lookup(scope, tool, mutating)
            if mode in _MODES:
                return mode
        return default


# --- the shared approval gate ----------------------------------------------------------------------

_ERR_PREFIXES = ("Tool error", "Unknown tool", "Bad arguments")


def approval_required(tool_name: str, agency: str, registry) -> bool:
    """Approval trigger (mechanism, not the config policy): approve when the whole run is in confirm mode,
    or when the tool self-declares it always needs approval (e.g. shell_run's REQUIRES_APPROVAL)."""
    return agency == "confirm" or tool_name in getattr(registry, "approval_required_tools", set())


def render_preview(registry, name: str, args: str):
    """A human-readable preview of a call from the tool's PREVIEW renderer (e.g. file_edit's diff), or
    None. Fail-safe: any error (no renderer, bad JSON, renderer raises) yields None so approvals never
    break on a preview bug."""
    render = getattr(registry, "previews", {}).get(name)
    if render is None:
        return None
    try:
        return render(json.loads(args) if args else {})
    except Exception:
        return None


def fire_pre_hooks(registry, name, args, context, log, rid):
    """Run PreToolUse hooks. Each may return {'decision': 'deny'|'ask', 'updatedInput': dict}. Hooks may
    only TIGHTEN (force deny/ask) -- an 'allow' never loosens the approval floor. Returns
    (decision_override, new_args): decision_override in {None,'deny','ask'}; new_args is the (possibly
    rewritten) argument JSON string. A hook that raises is caught + logged (a bad hook can't strand a run)."""
    hooks = getattr(registry, "hooks", {}).get("PreToolUse", [])
    decision, cur_args = None, args
    for hook in hooks:
        try:
            out = hook(name, cur_args, context)
        except Exception as e:
            log.warning(f"[{rid}] PreToolUse hook error (ignored): {e}")
            continue
        if not out:
            continue
        d = out.get("decision")
        if d == "deny":
            decision = "deny"                         # strongest tightening wins; stop
            break
        if d == "ask" and decision != "deny":
            decision = "ask"
        if out.get("updatedInput") is not None:
            try:
                cur_args = json.dumps(out["updatedInput"])
            except (TypeError, ValueError):
                log.warning(f"[{rid}] PreToolUse updatedInput not serializable (ignored)")
    return decision, cur_args


def fire_post_hooks(registry, name, args, result, context, log, rid):
    """Run PostToolUse hooks; each may return {'result': str} to rewrite the tool result. Fail-safe."""
    for hook in getattr(registry, "hooks", {}).get("PostToolUse", []):
        try:
            out = hook(name, args, result, context)
        except Exception as e:
            log.warning(f"[{rid}] PostToolUse hook error (ignored): {e}")
            continue
        if out and isinstance(out.get("result"), str):
            result = out["result"]
    return result


def resolve_approval(approve, action: dict) -> bool:
    """Ask the injected approve callback for a decision. Fail-closed: no approver wired (server,
    scheduler, MCP, tests, non-TTY) -> deny, so a dangerous tool never runs unattended by default."""
    if approve is None:
        return False
    try:
        return bool(approve(action))
    except (EOFError, KeyboardInterrupt):
        return False


def audit(log, rid, name, args, decision, owner):
    """One append-only audit line per tool call: tool, an args DIGEST (never the raw args, so
    secrets in arguments aren't logged), the decision, the owner, and the run id. Every mutation is
    attributable via a single `grep <rid>`."""
    digest = hashlib.sha1((args or "").encode("utf-8", "replace")).hexdigest()[:12]
    log.info(f"[{rid}] AUDIT tool={name} decision={decision} owner={owner} args_sha1={digest}")


def _self_repair_on(context) -> bool:
    """Whether a failed tool call should be retried once (agent.selfRepair). Read off the run's
    config via the context so no tool/dispatch signature changes. Default False == disabled."""
    cfg = getattr(context, "config", None) or {}
    return bool(cfg.get("agent", {}).get("selfRepair", False))


def dispatch_with_approval(tc, call_id, *, registry, context, agency, approve, log, rid):
    """Generator: resolve the permission policy, request approval if required, then dispatch one
    tool call. Yields protocol events (approval_required, tool_result) and RETURNS the result string
    that goes into the transcript. A denied call does not run and returns a denial message the model
    can react to.

    The decision = the config PermissionPolicy (allow|ask|deny per tool/owner/depth) combined with the
    approval floor: 'deny' short-circuits; 'ask' (or the approval floor: agency='confirm' /
    REQUIRES_APPROVAL) prompts the approve callback; else the call runs. An empty policy resolves to
    'allow', so behavior is identical to running with no policy configured."""
    from bob_tracing import Tracer

    name = tc.function.name
    args = tc.function.arguments
    owner = getattr(context, "owner", "local")
    policy = getattr(context, "policy", None)

    # An unattended run (MCP, and every sub-run it spawns: the allow-set rides on the RunContext) refuses
    # gated tools that agent.mcpAllowTools does not list, at every agent depth.
    unattended = getattr(context, "unattended_allow", None)
    if unattended is not None:
        refusal = unattended_refusal(registry, name, unattended)
        if refusal:
            audit(log, rid, name, args, "deny(unattended)", owner)
            yield {"type": "tool_result", "call_id": call_id, "name": name, "result": refusal}
            return refusal
    mutating = name in getattr(registry, "mutating_tools", set())
    # A remote MCP tool (mcp:<server>:<tool>) defaults to 'ask': reaching an external server is a
    # side effect worth a prompt. A local tool keeps the 'allow' default. Either way an explicit
    # policy rule (per tool/owner/depth) wins over this default.
    remote = name in getattr(registry, "remote_tools", set())
    tool_default = ASK if remote else ALLOW
    decision = (policy.resolve(name, owner=owner, agent_depth=getattr(context, "agent_depth", 0),
                               mutating=mutating, default=tool_default)
                if policy is not None else tool_default)

    # PreToolUse hooks may TIGHTEN the decision (force deny/ask) and rewrite the arguments; they never
    # loosen below the policy/approval floor. A hook-forced deny short-circuits like a policy deny.
    pre_decision, args = fire_pre_hooks(registry, name, args, context, log, rid)
    if pre_decision == DENY:
        audit(log, rid, name, args, "deny(hook)", owner)
        denied = f"Tool call to '{name}' was blocked by a PreToolUse hook; it did not run."
        yield {"type": "tool_result", "call_id": call_id, "name": name, "result": denied}
        return denied
    if pre_decision == ASK and decision != DENY:
        decision = ASK

    # deny: never dispatches; the model gets a clean refusal it can read and react to.
    if decision == DENY:
        audit(log, rid, name, args, "deny", owner)
        denied = f"Tool call to '{name}' was denied by policy; it did not run."
        yield {"type": "tool_result", "call_id": call_id, "name": name, "result": denied}
        return denied

    # ask: policy 'ask' OR the approval floor (whole run in confirm mode, or the tool self-declares
    # REQUIRES_APPROVAL). The floor is a lower bound the config can tighten but never loosen.
    if decision == ASK or approval_required(name, agency, registry):
        risk = "high" if name in getattr(registry, "approval_required_tools", set()) else "confirm"
        # A preview renderer (e.g. file_edit's diff) lets the operator approve the actual change, not raw
        # args. Fail-safe: a preview that raises falls back to no preview. Raw args are always kept.
        preview = render_preview(registry, name, args)
        action = {"call_id": call_id, "tool": name, "arguments": args, "risk": risk}
        if preview is not None:
            action["preview"] = preview
        yield {"type": "approval_required", **action}
        if not resolve_approval(approve, action):
            audit(log, rid, name, args, "deny(unapproved)", owner)
            log.info(f"[{rid}] tool {name} denied (call_id={call_id})")
            denied = f"Tool call to '{name}' was denied by the user; it did not run."
            yield {"type": "tool_result", "call_id": call_id, "name": name, "result": denied}
            return denied

    audit(log, rid, name, args, decision if policy is not None else ALLOW, owner)
    # One tool span per dispatch (child of the run span). No yield inside the block, so the span's
    # timing is just the dispatch. Disabled tracer => shared no-op (behaviorally inert).
    tracer = getattr(context, "tracer", None) or Tracer(enabled=False)
    with tracer.span("agent.tool", {"tool": name, "owner": owner, "decision": decision},
                     parent=getattr(context, "trace_span", None)) as _sp:
        result = registry.dispatch_call(name, args, context=context)
        is_err = result.startswith(_ERR_PREFIXES)
        # Self-repair: retry a failed tool call ONCE, catching a flaky/transient tool failure. A
        # deterministic error just fails again and is returned as-is. Default off (agent.selfRepair).
        if is_err and _self_repair_on(context):
            retried = registry.dispatch_call(name, args, context=context)
            if not retried.startswith(_ERR_PREFIXES):
                log.info(f"[{rid}] self-repair: {name} succeeded on retry (call_id={call_id})")
                result, is_err = retried, False
        _sp.set("result_chars", len(result)).set_status("error" if is_err else "ok")
    # PostToolUse hooks may rewrite/redact the result before it enters the transcript.
    result = fire_post_hooks(registry, name, args, result, context, log, rid)
    log.log(
        logging.WARNING if is_err else logging.INFO,
        f"[{rid}] tool {name} -> {len(result)}c (call_id={call_id})"
        + (f" ERROR: {result[:200]}" if is_err else ""),
    )
    yield {"type": "tool_result", "call_id": call_id, "name": name, "result": result}
    return result


# Tools gated on an unattended surface however they are otherwise marked, because each runs a whole
# agent loop (other tools) on the caller's behalf: a sub-agent, and a schedule fired now. Delegating is
# only allowed when listed.
UNATTENDED_GATED = frozenset({"spawn_agent", "schedule_run"})


def unattended_refusal(registry, name: str, allow) -> "str | None":
    """For a surface with no operator to ask (MCP): None if `name` may run, else the refusal text.
    Approval-required, mutating and UNATTENDED_GATED tools are refused unless `allow`
    (agent.mcpAllowTools) lists them."""
    gated = (name in UNATTENDED_GATED
             or name in getattr(registry, "approval_required_tools", set())
             or name in getattr(registry, "mutating_tools", set()))
    if gated and name not in set(allow or ()):
        return (f"Tool call to '{name}' was refused: it needs approval or changes state, and this "
                "surface has no one to approve it. List it in agent.mcpAllowTools to allow it here.")
    return None


def run_gated(registry, name: str, args_json: str, *, config: dict = None, owner: str = None,
              agency: str = "silent", approve=None, allow_unattended=None, context=None,
              surface: str = "cli", on_event=None) -> str:
    """Blocking form of dispatch_with_approval for a single call from a surface without an agent loop
    (MCP, `bob --run`, skill steps). Builds the same run context the loop hands a tool (policy, owner,
    config, cancel) unless the caller passes one. `approve=None` fails closed. `allow_unattended` (a
    set of tool names) switches on the unattended rule: approval-required, mutating and UNATTENDED_GATED
    tools are refused unless listed there, and a listed tool counts as approved. The rule rides on the run
    context, so a sub-agent the call spawns is held to it too. `on_event(ev)` sees each protocol event.
    Returns the result string (a denial message when the call did not run)."""
    from types import SimpleNamespace
    import uuid

    config = config or {}
    owner = owner or config.get("agent", {}).get("defaultOwner", "local")
    rid = f"{surface}:{uuid.uuid4().hex[:8]}"
    try:
        import bob_loop
        log = bob_loop._agent_logger(config) if config else logging.getLogger("bob.agent")
    except Exception:
        log = logging.getLogger("bob.agent")
    allowed = None
    if allow_unattended is not None:
        allowed = frozenset(allow_unattended)
        approve = (lambda action: action.get("tool") in allowed)
    if context is None:
        try:
            import bob_loop
            context = bob_loop.RunContext(cancel=bob_loop.CancelToken(), config=config, registry=registry,
                                          run_id=rid, approve=approve, owner=owner,
                                          policy=PermissionPolicy(config), unattended_allow=allowed)
        except Exception:
            context = SimpleNamespace(config=config, owner=owner, agent_depth=0,
                                      policy=PermissionPolicy(config), approve=approve,
                                      unattended_allow=allowed)
    elif allowed is not None:
        context.unattended_allow = allowed
    tc = SimpleNamespace(function=SimpleNamespace(name=name, arguments=args_json))
    gen = dispatch_with_approval(tc, rid, registry=registry, context=context, agency=agency,
                                 approve=approve, log=log, rid=rid)
    while True:
        try:
            ev = next(gen)
        except StopIteration as stop:
            return stop.value
        if on_event is not None:
            on_event(ev)
