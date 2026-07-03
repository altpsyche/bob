"""O6 — permission policy, resolved from config and enforced at the dispatch choke point.

Turns the NE0 binary approval *mechanism* (a tool self-declaring REQUIRES_APPROVAL, or the whole run
being in ``agency='confirm'``) into a real per-tool ``allow | ask | deny`` *policy*, overridable
per-owner (N1 identity) and per-agent-depth (an O1 sub-agent may be more restricted than the root).

Default reproduces today's behavior. An **absent or empty** ``agent.permissions`` config resolves every
tool to ``allow``, so the only thing that still prompts is the NE0 floor (``agency='confirm'`` or a
tool's ``REQUIRES_APPROVAL``) and nothing is ever denied — byte-identical to pre-O6. A configured policy
*adds* ``deny`` and can *promote* ``allow -> ask``; it never weakens the NE0 floor (``_dispatch_with_
approval`` keeps that as a lower bound).

Config shape (``config/defaults.json`` -> ``runtime.agent.permissions``; all keys optional)::

    "permissions": {
      "read":     "allow",                       # class default for non-mutating tools
      "mutating": "ask",                          # class default for tools in registry.mutating_tools
      "tools":    { "shell_run": "ask", "x": "deny" },   # per-tool, wins over the class default
      "perOwner": { "guest": { "mutating": "deny" } },   # same shape, per N1 owner id
      "perDepth": { "1": { "mutating": "deny" } }        # same shape, per agent_depth (str key)
    }

Resolution precedence (most specific first): ``perDepth[depth]`` -> ``perOwner[owner]`` -> top level;
within each, a per-tool entry wins over the ``mutating``/``read`` class default. First hit wins; no hit
anywhere -> ``allow``. ``PermissionPolicy`` is pure (no registry/LLM dependency) so it unit-tests in
isolation — the caller passes the ``mutating`` bool it read from ``registry.mutating_tools``.
"""

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
                mutating: bool = False) -> str:
        """Return 'allow' | 'ask' | 'deny' for this call. Unknown/malformed modes fall through to
        'allow' (a config typo can't silently deny reads; the NE0 floor still guards mutations)."""
        if not self.configured:
            return ALLOW
        for scope in (self._per_depth.get(str(agent_depth)),
                      self._per_owner.get(owner),
                      self._perms):
            mode = _lookup(scope, tool, mutating)
            if mode in _MODES:
                return mode
        return ALLOW
