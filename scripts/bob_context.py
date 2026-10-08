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

    Precedence: explicit value, then agent.contextMode, then quick.  Raises
    ContextModeError when the value is unknown.
    """
    raw = value
    if raw is None:
        raw = ((config or {}).get("agent", {}) or {}).get("contextMode")
    if raw is None or not str(raw).strip():
        raw = DEFAULT_CONTEXT_MODE
    key = str(raw).strip().lower()
    mode = MODE_ALIASES.get(key, key)
    modes = ((config or {}).get("agent", {}) or {}).get("contextModes")
    if isinstance(modes, dict) and mode not in modes:
        raise ContextModeError(
            f"unknown context mode '{value}'. Valid: {', '.join(sorted(modes))}"
        )
    return mode


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
    cfg = config or {}
    agent = cfg.get("agent", {}) or {}
    spec = (agent.get("contextModes") or {}).get(mode) or {}
    block = spec.get(backend) or {}
    cap = block.get("maxContextTokens", agent.get("maxContextTokens", 0))
    try:
        cap = int(cap or 0)
    except (TypeError, ValueError):
        cap = 0
    base = int(role_window or 0)
    if cap > 0:
        return min(cap, base) if base else cap
    return base


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
    if not window:
        return body
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return body

    from bob_core import est_tokens, fit_messages

    tools = body.get("tools")
    tools_tokens = est_tokens(json.dumps(tools, ensure_ascii=False)) + 4 if tools else 0
    requested = body.get("max_tokens")
    if requested is None:
        requested = body.get("max_completion_tokens")
    try:
        requested = int(requested or 0)
    except (TypeError, ValueError):
        requested = 0
    policy_out = int(policy.output_tokens(config, base) or 0)
    output = min(requested, policy_out) if requested > 0 else policy_out
    output = max(1, output)
    send_budget = max(0, int(window) - output - int(tools_tokens) - 64)
    out = dict(body)
    out["messages"] = fit_messages(messages, send_budget)
    out["max_tokens"] = output
    out.pop("max_completion_tokens", None)
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
        from bob_core import role_window

        base = int(role_window(config, role) or 0)
        cap = int(self.max_context_tokens or 0)
        if cap > 0:
            return min(cap, base) if base else cap
        return base

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


def resolve(config: dict, role: str, mode: Optional[str] = None) -> ContextPolicy:
    """Resolve the active context policy for ``role``.

    ``role`` must already be the effective served role (after fallback and
    image routing).  Local and API blocks are selected with the same
    ``is_local_role`` seam the loop uses for reasoning kwargs.
    """
    from bob_core import _mem, is_local_role

    cfg = config or {}
    name = normalize_mode(mode, cfg)
    modes = (cfg.get("agent", {}) or {}).get("contextModes") or {}
    spec = modes.get(name) or {}
    backend = "local" if is_local_role(role, cfg) else "api"
    block = spec.get(backend) or {}
    base_agent = cfg.get("agent", {}) or {}
    mem_cfg = cfg.get("memory", {}) or {}

    def pick(key, default):
        if key in block:
            return block[key]
        return base_agent.get(key, default)

    try:
        memory_default = _mem(mem_cfg, "maxInjectedTokens") or 1200
    except Exception:
        memory_default = 1200

    # Memory and tool-result budgets are mode-specific when present.  A mode
    # can still fall back to the existing agent/memory values.
    memory_tokens = block.get("memoryMaxInjectedTokens")
    if memory_tokens is None:
        memory_tokens = base_agent.get("memoryMaxInjectedTokens", memory_default)

    return ContextPolicy(
        mode=name,
        backend=backend,
        label=str(spec.get("label") or name.title()),
        max_context_tokens=_int(pick("maxContextTokens", 0), 0),
        max_history_msgs=_int(pick("maxHistoryMsgs", 40), 40),
        output_reserve_tokens=_int(pick("outputReserveTokens", 0), 0),
        memory_max_injected_tokens=_int(memory_tokens, 1200),
        max_tool_result_tokens=_int(pick("maxToolResultTokens", 1000), 1000),
        compaction=str(pick("compaction", "truncate") or "truncate"),
        compact_keep_last_turns=_int(pick("compactKeepLastTurns", 6), 6),
        compact_summary_max_tokens=_int(pick("compactSummaryMaxTokens", 512), 512),
        stable_prefix=bool(pick("stablePrefix", False)),
        clear_tool_results=bool(pick("clearToolResults", False)),
        clear_tool_results_after_tokens=_int(pick("clearToolResultsAfterTokens", 4000), 4000),
        compact_schemas_after=_int(pick("compactSchemasAfter", 12), 12),
    )
