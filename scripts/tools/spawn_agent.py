"""Bob tool: spawn_agent — delegate a subtask to an isolated nested agent run (O1, the centerpiece).

Composes the earlier O work: O2 runs several spawn_agent calls in one step concurrently (fan-out/
fan-in) for free — spawn_agent is deliberately NOT mutating; O3 lets the sub-run compact its own
transcript; O6 makes the sub-run inherit the same allow|ask|deny policy + approver.

Gated on agent.subAgents (default false) so the default toolset is byte-identical to pre-O1. Reaches
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
    # Optional whitelist for the sub-agent's toolset; None = inherit the full (already-restricted) view.
    allow = agent_cfg.get("subAgentTools") or None
    sub_registry = base_registry.filtered(allow=allow)

    from bob_loop import run_agent_events, CancelToken

    parent_cancel = getattr(ctx, "cancel", None)
    child_cancel = parent_cancel.child() if isinstance(parent_cancel, CancelToken) else None
    parent_rid = getattr(ctx, "run_id", None) or "root"
    child_rid = f"{parent_rid}.sub{parent_depth + 1}"

    result, error, tools_used = None, None, []
    try:
        for ev in run_agent_events(
            task, config, role=role, agency=agent_cfg.get("agency", "show"),
            registry=sub_registry, stream=False, history=None,
            cancel=child_cancel, run_id=child_rid, approve=getattr(ctx, "approve", None),
            owner=getattr(ctx, "owner", None), agent_depth=parent_depth + 1,
            scope=getattr(ctx, "scope", None),
            trace_parent=getattr(ctx, "trace_span", None),   # O9 — nest the sub-run under the parent
        ):
            t = ev.get("type")
            if t == "tool_call":
                tools_used.append(ev.get("name"))
            elif t == "final":
                result = ev.get("result")
            elif t == "error":
                error = ev.get("message")
    except Exception as e:   # a sub-run failure must not crash the parent step
        error = str(e)

    summary = {
        "result": result if result is not None else "(sub-agent produced no final answer)",
        "steps": len(tools_used),
        "tools_used": list(dict.fromkeys(t for t in tools_used if t)),
    }
    if error:
        summary["error"] = error
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
                             "description": "Optional model role override for the sub-run (default: the agent role)."},
                },
                "required": ["task"],
            },
        },
    },
]

DISPATCH = {"spawn_agent": _spawn_agent}
