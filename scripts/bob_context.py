"""Context-mode policy for Bob.

A mode is a named context budget bundle, not a model swap.  The same mode is
resolved differently for local roles and API (cloud/pro) roles, so a Quick
local budget can never clamp a 1M-token API peer and a Wide API budget can
never make a local llama-server exceed its loaded ``-c`` window.

The resolver is deliberately dependency-light: ``bob_core`` is imported lazily
inside methods to avoid an import cycle, and every field is safe to read with
plain ``getattr`` by callers that already hold a ContextPolicy.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

DEFAULT_CONTEXT_MODE = "quick"

# User-facing aliases. Canonical names are quick/deep; the others are accepted
# for muscle memory and for older planning notes.
MODE_ALIASES = {
    "quick": "quick",
    "deep": "deep",
    "fast": "quick",
    "slow": "deep",
    "lean": "quick",
    "wide": "deep",
}


class ContextModeError(ValueError):
    """Unknown or malformed context mode."""


def normalize_mode(value=None, config: Optional[dict] = None) -> str:
    """Return the canonical mode name for ``value``.

    Precedence: explicit value, then agent.contextMode, then quick.  A mode defined
    in agent.contextModes is matched by its own name before the aliases, so a user
    mode called ``fast`` is that mode, not Quick.  Raises ContextModeError when the
    value is unknown, naming agent.contextMode when that is where it came from.
    """
    agent = (config or {}).get("agent", {}) or {}
    from_config = value is None or not str(value).strip()
    raw = agent.get("contextMode") if from_config else value
    if raw is None or not str(raw).strip():
        raw = DEFAULT_CONTEXT_MODE
    key = str(raw).strip().lower()
    modes = agent.get("contextModes")
    if not isinstance(modes, dict) or not modes:
        return MODE_ALIASES.get(key, key)
    if key in modes:
        return key
    mode = MODE_ALIASES.get(key, key)
    if mode in modes:
        return mode
    valid = ", ".join(known_modes(config))
    if from_config:
        raise ContextModeError(
            f"agent.contextMode is '{raw}', which is not a context mode. "
            f"Set it to one of: {valid} (config/user.json)"
        )
    raise ContextModeError(f"unknown context mode '{raw}'. Valid: {valid}")


def known_modes(config: Optional[dict] = None) -> list:
    """Canonical mode names present in config, quick/deep first when present."""
    modes = ((config or {}).get("agent", {}) or {}).get("contextModes") or {}
    names = list(modes)
    preferred = [m for m in ("quick", "deep") if m in names]
    return preferred + sorted(n for n in names if n not in preferred)


def mode_label(mode: str, config: Optional[dict] = None) -> str:
    """Human label for a mode, falling back to title case."""
    spec = (((config or {}).get("agent", {}) or {}).get("contextModes") or {}).get(mode) or {}
    return str(spec.get("label") or mode.title())


# Mode aliases exposed as model-name suffixes.  Every harness that can only choose an OpenAI model
# name can still select a mode without a bespoke protocol.
MODE_SUFFIXES = {"-quick": "quick", "-deep": "deep"}


def parse_model_alias(name: str) -> tuple:
    """(base_role, mode|None) for a mode-suffixed model name."""
    value = str(name or "")
    for suffix, mode in MODE_SUFFIXES.items():
        if value.endswith(suffix):
            return value[: -len(suffix)], mode
    return value, None


def wire_model_names(role: str) -> list:
    """The base name plus its Quick/Deep model aliases, in stable order."""
    return [role, f"{role}-quick", f"{role}-deep"]


def mode_window(role_window: int, backend: str, mode: str, config: Optional[dict] = None) -> int:
    """The effective window for a role when served under ``mode``.

    This is the generator-side counterpart of ContextPolicy.window, used to advertise the same window
    to external clients that Bob itself would enforce.
    """
    from bob_core import cap_window

    return cap_window(role_window, _resolve_block(config or {}, mode, backend).max_context_tokens)


def wire_model_variants(role: str, role_window: int, backend: str,
                        config: Optional[dict] = None) -> list:
    """[(model_name, effective_window)] for the base role and its Quick/Deep aliases."""
    out = []
    for name in wire_model_names(role):
        _base, mode = parse_model_alias(name)
        window = int(role_window or 0) if mode is None else mode_window(role_window, backend, mode, config)
        out.append((name, window))
    return out


def apply_openai_request(config: dict, model_name: str, body: dict) -> dict:
    """Apply the active context mode to one OpenAI chat-completions request body.

    This is the single enforcement seam for every external harness.  A request whose model name has no
    mode suffix is returned unchanged, so base ``chat`` and ``chat-pro`` keep their existing behavior.

    The window is the mode's, the same one the generated client configs advertise (mode_window), and
    the prompt is measured with the plain tokenizer count (no safety padding): the harness was told
    that window and manages its own prompt against it, so this seam only steps in when a request would
    really exceed it.  Messages are trimmed by whole turns (bob_core.fit_messages), reserving the mode's
    output or the client's smaller ask.  A max_tokens / max_completion_tokens the client sent is kept in
    the field it used and lowered only when it asks for more than the window leaves after the prompt
    or than the model can produce; a request that sent neither stays without one.
    """
    if not isinstance(body, dict):
        return body
    base, mode = parse_model_alias(model_name)
    if mode is None:
        return body
    try:
        policy = resolve(config, base, mode)
        window = policy.window(config, base)
    except Exception:
        return body
    messages = body.get("messages")
    if not window or not isinstance(messages, list) or not messages:
        return body

    from bob_core import _COMPLETE_MARGIN_TOKENS, est_tokens, fit_messages, message_tokens, role_max_output

    tools = body.get("tools") or body.get("functions")
    tools_tokens = est_tokens(json.dumps(tools, ensure_ascii=False), pad=False) + 4 if tools else 0
    fields = [f for f in ("max_tokens", "max_completion_tokens") if body.get(f) is not None]
    requested = max((_int(body[f], 0) for f in fields), default=0)
    policy_out = max(1, int(policy.output_tokens(config, base) or 0))
    reserve = min(requested, policy_out) if requested > 0 else policy_out
    fixed = int(tools_tokens) + _COMPLETE_MARGIN_TOKENS
    out = dict(body)
    out["messages"] = fit_messages(messages, max(1, int(window) - reserve - fixed), pad=False)
    if fields:
        prompt = sum(message_tokens(m, pad=False) for m in out["messages"]) + fixed
        limit = max(1, int(window) - prompt)
        model_max = role_max_output(config, base)
        if model_max > 0:
            limit = min(limit, model_max)
        for f in fields:
            if _int(out[f], 0) > limit:
                out[f] = limit
    return out


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


@dataclass
class ContextPolicy:
    """Resolved per-request context policy for one backend class."""

    mode: str
    backend: str
    label: str
    max_context_tokens: int
    max_history_msgs: int
    output_reserve_tokens: int
    memory_max_injected_tokens: int
    max_tool_result_tokens: int
    compaction: str
    compact_keep_last_turns: int
    compact_summary_max_tokens: int
    stable_prefix: bool
    clear_tool_results: bool
    clear_tool_results_after_tokens: int
    compact_schemas_after: int

    @property
    def is_local(self) -> bool:
        return self.backend == "local"

    def window(self, config: dict, role: str) -> int:
        """Effective per-request window for ``role`` under this policy.

        The role window remains the source of truth: a local role's loaded
        ``-c`` slot or an API peer's advertised ``contextWindow``.  A positive
        mode cap only lowers it; zero means use the full role window.
        """
        from bob_core import cap_window, role_window

        return cap_window(role_window(config, role), self.max_context_tokens)

    def output_tokens(self, config: dict, role: str) -> int:
        """Requested output reservation/max_tokens for ``role`` under this mode."""
        from bob_core import role_output_tokens

        base = int(role_output_tokens(config, role) or 0)
        mode_value = int(self.output_reserve_tokens or 0)
        if self.backend == "api":
            # For API peers, 0 means use the peer's maxOutputTokens.  A positive
            # mode value caps the peer, never raises above it.
            return min(base, mode_value) if mode_value > 0 else base
        return mode_value if mode_value > 0 else base

    def summary(self, config: dict, role: str) -> dict:
        """A small UI/debug view used by the shell and logs."""
        base = 0
        try:
            from bob_core import role_window
            base = int(role_window(config, role) or 0)
        except Exception:
            base = 0
        return {
            "mode": self.mode,
            "label": self.label,
            "backend": self.backend,
            "role": role,
            "window": self.window(config, role),
            "role_window": base,
            "max_history_msgs": self.max_history_msgs,
            "output_tokens": self.output_tokens(config, role),
            "memory_max_injected_tokens": self.memory_max_injected_tokens,
            "max_tool_result_tokens": self.max_tool_result_tokens,
            "compaction": self.compaction,
            "stable_prefix": self.stable_prefix,
            "clear_tool_results": self.clear_tool_results,
            "compact_schemas_after": self.compact_schemas_after,
        }


def clearing_history_window(config: dict) -> int:
    """The largest history window of any policy that clears tool results, 0 when none does.

    The tool registry is built once per process, before a run picks its mode and role, so the
    re-fetch tool and the result store are sized for every policy a run could resolve to.
    """
    cfg = config or {}
    modes = ((cfg.get("agent", {}) or {}).get("contextModes") or {})
    policies = [_resolve_block(cfg, None, "local")]
    policies += [_resolve_block(cfg, name, backend) for name, spec in modes.items()
                 if isinstance(spec, dict) for backend in ("local", "api")]
    return max((p.max_history_msgs for p in policies if p.clear_tool_results), default=0)


# ContextPolicy field -> (config key, fallback when no layer sets it).
_POLICY_KEYS = {
    "max_context_tokens": ("maxContextTokens", 0),
    "max_history_msgs": ("maxHistoryMsgs", 40),
    "output_reserve_tokens": ("outputReserveTokens", 0),
    "max_tool_result_tokens": ("maxToolResultTokens", 1000),
    "compaction": ("compaction", "truncate"),
    "compact_keep_last_turns": ("compactKeepLastTurns", 6),
    "compact_summary_max_tokens": ("compactSummaryMaxTokens", 512),
    "stable_prefix": ("stablePrefix", False),
    "clear_tool_results": ("clearToolResults", False),
    "clear_tool_results_after_tokens": ("clearToolResultsAfterTokens", 4000),
    "compact_schemas_after": ("compactSchemasAfter", 12),
}
_MISSING = object()


def _shipped(*path):
    """The value at ``path`` under config/defaults.json runtime, or {} when absent."""
    from bob_core import load_defaults

    node = load_defaults().get("runtime", {}) or {}
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else {}


def _layered(key, block: dict, shipped_block: dict, user: dict, shipped_user: dict, default):
    """One policy value by layer: the user's mode block, the user's global setting, the shipped mode
    block, the shipped global setting.

    The runtime config is one deep merge of config/defaults.json and config/user.json, so a layer
    counts as the user's when its value differs from the shipped one (or the shipped file has no such
    key, as for a mode the user defined).  Setting a key to its shipped value is therefore the same
    as leaving it unset.
    """
    own = block.get(key, _MISSING)
    if own is not _MISSING and shipped_block.get(key, _MISSING) != own:
        return own
    glob = user.get(key, _MISSING)
    if glob is not _MISSING and shipped_user.get(key, _MISSING) != glob:
        return glob
    if own is not _MISSING:
        return own
    return default if glob is _MISSING else glob


def _resolve_block(cfg: dict, name: Optional[str], backend: str) -> ContextPolicy:
    """The policy of mode ``name`` (None = no mode block) on ``backend``, without validating the name."""
    agent = cfg.get("agent", {}) or {}
    spec = ((agent.get("contextModes") or {}).get(name) or {}) if name else {}
    block = (spec.get(backend) if isinstance(spec, dict) else None) or {}
    if not isinstance(block, dict):
        block = {}
    shipped_block = _shipped("agent", "contextModes", name, backend) if name else {}
    shipped_agent = _shipped("agent")
    values = {field: _layered(key, block, shipped_block, agent, shipped_agent, default)
              for field, (key, default) in _POLICY_KEYS.items()}
    # The memory budget's global setting is memory.maxInjectedTokens, under the mode blocks' key name.
    mem_cfg = cfg.get("memory", {}) or {}
    shipped_mem = _shipped("memory").get("maxInjectedTokens", _MISSING)
    memory_tokens = _layered(
        "memoryMaxInjectedTokens", block, shipped_block,
        {"memoryMaxInjectedTokens": mem_cfg["maxInjectedTokens"]} if "maxInjectedTokens" in mem_cfg else {},
        {"memoryMaxInjectedTokens": shipped_mem}, 1200 if shipped_mem is _MISSING else shipped_mem)
    return ContextPolicy(
        mode=name or DEFAULT_CONTEXT_MODE,
        backend=backend,
        label=str((spec.get("label") if isinstance(spec, dict) else None) or (name or "").title()),
        max_context_tokens=_int(values["max_context_tokens"], 0),
        max_history_msgs=_int(values["max_history_msgs"], 40),
        output_reserve_tokens=_int(values["output_reserve_tokens"], 0),
        memory_max_injected_tokens=_int(memory_tokens, 1200),
        max_tool_result_tokens=_int(values["max_tool_result_tokens"], 1000),
        compaction=str(values["compaction"] or "truncate"),
        compact_keep_last_turns=_int(values["compact_keep_last_turns"], 6),
        compact_summary_max_tokens=_int(values["compact_summary_max_tokens"], 512),
        stable_prefix=bool(values["stable_prefix"]),
        clear_tool_results=bool(values["clear_tool_results"]),
        clear_tool_results_after_tokens=_int(values["clear_tool_results_after_tokens"], 4000),
        compact_schemas_after=_int(values["compact_schemas_after"], 12),
    )


def resolve(config: dict, role: str, mode: Optional[str] = None) -> ContextPolicy:
    """Resolve the active context policy for ``role``.

    ``role`` must already be the effective served role (after fallback and
    image routing).  Local and API blocks are selected with the same
    ``is_local_role`` seam the loop uses for reasoning kwargs.  Each value comes
    from the first layer that sets it (_layered): the user's mode block, the
    user's agent.* (memory.maxInjectedTokens for the memory budget), the shipped
    mode block, the shipped agent.* default.
    """
    from bob_core import is_local_role

    cfg = config or {}
    name = normalize_mode(mode, cfg)
    backend = "local" if is_local_role(role, cfg) else "api"
    return _resolve_block(cfg, name, backend)
