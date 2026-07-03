#!/usr/bin/env python3
"""Bob agent loop — LLM reasons about what tools to call, executes them, loops until done.

Called by bob.ps1 agent case and bob-agent.ps1 scheduler.
All config is read from data/config.json (written by Get-BobConfig in _models.ps1).
"""
import json
import logging
import os
import re
import signal
import sys
import threading
import time
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "tools"))


def _estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token for English + JSON). No tokenizer dependency —
    good enough for budgeting message history and tool results (M7)."""
    if not text:
        return 0
    return (len(text) + 3) // 4


def _message_tokens(m: dict) -> int:
    """Estimated token cost of a single chat message, including tool-call payloads."""
    content = m.get("content") or ""
    if not isinstance(content, str):
        content = json.dumps(content)
    total = _estimate_tokens(content) + 4  # per-message role/format overhead
    for tc in (m.get("tool_calls") or []):
        total += _estimate_tokens(json.dumps(tc))
    return total


def _is_transient(e) -> bool:
    """True for LLM errors worth exactly one retry: connection / timeout / 5xx / 429."""
    if type(e).__name__ in (
        "APIConnectionError", "APITimeoutError", "InternalServerError",
        "RateLimitError", "ConnectionError", "Timeout", "ReadTimeout",
    ):
        return True
    return getattr(e, "status_code", None) in (429, 500, 502, 503, 504)


def _parse_hermes_tool_calls(content: str) -> list | None:
    """Parse <tool_call> blocks from Hermes-format content.
    Handles both JSON-inside and XML-sub-element variants.
    Malformed JSON blocks are returned as __parse_error__ calls so the LLM
    can see the failure and self-correct, rather than being silently dropped.
    """
    blocks = re.findall(r"<tool_call>(.*?)</tool_call>", content, re.DOTALL)
    if not blocks:
        return None
    calls = []
    for i, block in enumerate(blocks):
        block = block.strip()
        if block.startswith("{"):
            try:
                d = json.loads(block)
                name = d.get("name", "")
                args = d.get("arguments", {})
            except json.JSONDecodeError as parse_err:
                print(
                    f"[warn] malformed tool call JSON in block {i}: {parse_err}",
                    file=sys.stderr,
                )
                calls.append(
                    SimpleNamespace(
                        id=f"hermes_err_{i}",
                        function=SimpleNamespace(
                            name="__parse_error__",
                            arguments=json.dumps(
                                {"error": str(parse_err), "raw": block[:200]}
                            ),
                        ),
                    )
                )
                continue
        else:
            name_m = re.search(r"<name>(.*?)</name>", block, re.DOTALL)
            if not name_m:
                continue
            name = name_m.group(1).strip()
            args_m = re.search(r"<arguments>(.*?)</arguments>", block, re.DOTALL)
            args = {}
            if args_m:
                try:
                    args = json.loads(args_m.group(1).strip())
                except json.JSONDecodeError as parse_err:
                    # M9 — mirror the JSON path: surface malformed <arguments> as a parse-error
                    # call so the LLM can self-correct, instead of silently dropping the args.
                    print(
                        f"[warn] malformed <arguments> JSON in block {i}: {parse_err}",
                        file=sys.stderr,
                    )
                    calls.append(
                        SimpleNamespace(
                            id=f"hermes_err_{i}",
                            function=SimpleNamespace(
                                name="__parse_error__",
                                arguments=json.dumps(
                                    {"error": str(parse_err), "raw": block[:200]}
                                ),
                            ),
                        )
                    )
                    continue
        if not name:
            continue
        calls.append(
            SimpleNamespace(
                id=f"hermes_{i}",
                function=SimpleNamespace(
                    name=name,
                    arguments=json.dumps(args),
                ),
            )
        )
    return calls or None


def _strip_tool_calls(content: str) -> str:
    return re.sub(r"<tool_call>.*?</tool_call>", "", content, flags=re.DOTALL).strip()


def _final_answer(text, hermes: bool):
    """Clean a (possibly partial) final answer: in hermes mode strip tool-call markup so a
    cancelled/interrupted run never returns raw <tool_call> text as if it were the answer."""
    return _strip_tool_calls(text) if (hermes and text) else text


def _compact_schema(fn: dict) -> dict:
    """Strip verbose descriptions from a function schema, keeping the callable contract
    (name, param names, types/enums, required). Used when the tool count is high so the
    fixed per-turn prompt overhead doesn't grow linearly with the number of tools (M7)."""
    params = fn.get("parameters", {}) or {}
    props = {}
    for pname, pspec in (params.get("properties", {}) or {}).items():
        props[pname] = {k: v for k, v in pspec.items() if k in ("type", "enum")}
    return {
        "name": fn.get("name", ""),
        "description": (fn.get("description", "") or "")[:80],
        "parameters": {
            "type": params.get("type", "object"),
            "properties": props,
            "required": params.get("required", []),
        },
    }


def _hermes_tool_system_addendum(tool_schemas: list, compact_after: int = 12) -> str:
    """Extra system prompt fragment for Hermes-format tool calling.

    Past `compact_after` tools, emit compact schemas (drop param descriptions, no indent) so
    a large plugin set doesn't silently eat a small local context window (M7)."""
    fns = [s["function"] for s in tool_schemas if s.get("type") == "function"]
    if compact_after and len(fns) > compact_after:
        tools_json = json.dumps([_compact_schema(f) for f in fns])
    else:
        tools_json = json.dumps(fns, indent=2)
    return (
        "\n\nYou have access to the following tools:\n"
        f"<tools>\n{tools_json}\n</tools>\n\n"
        "For each function call, output JSON wrapped in <tool_call></tool_call> tags:\n"
        '<tool_call>{"name": "<function-name>", "arguments": {<args>}}</tool_call>\n'
        "Call tools as needed. When you have the final answer, respond normally without tool_call tags."
    )


def _openai_tools_payload(tool_schemas: list, compact_after: int = 12):
    """The `tools=` payload for OpenAI-format calls. Past `compact_after` tools, compact each schema
    (drop param descriptions) — mirrors the hermes addendum so `compactSchemasAfter` isn't a no-op in
    OpenAI mode (previously the full schema list was re-sent every turn regardless of tool count)."""
    fns = [s for s in tool_schemas if s.get("type") == "function"]
    if compact_after and len(fns) > compact_after:
        return [{"type": "function", "function": _compact_schema(s["function"])} for s in fns]
    return tool_schemas


class RunContext:
    """NE0/O1 seam — run-scoped services carried into dispatch_call and reachable by a tool via
    tool_registry.get_run_context(): the cancel token, the resolved config, the tool registry, the
    run id, and the approval callback. Lets a tool (a future sub-agent tool) reach these without any
    change to its fn(**args) signature."""
    __slots__ = ("cancel", "config", "registry", "run_id", "approve", "owner", "agent_depth", "scope")

    def __init__(self, cancel, config, registry, run_id, approve, owner="local", agent_depth=0,
                 scope=None):
        self.cancel = cancel
        self.config = config
        self.registry = registry
        self.run_id = run_id
        self.approve = approve
        # MEM-6 — the identity this run acts as (memory scoping) and its delegation depth. `agent_depth`
        # is 0 for a root run; O1 sets it >0 on sub-agents and propagates owner/cancel parent->child.
        self.owner = owner
        self.agent_depth = agent_depth
        # MEM-7 — project scope (git-root/cwd key) for this run; None = global. Threaded from the
        # shell/CLI; the memory tools read it via get_run_context() to scope project-type facts.
        self.scope = scope


def _call_id(tc, step: int, idx: int) -> str:
    """Stable id correlating a tool_call event with its tool_result (and approval_required). OpenAI
    tool_calls carry an `id`; hermes-parsed calls don't, so fall back to step.idx. Forward-compat for
    O2 parallel tools, where results arrive out of order and must be matched by id."""
    return getattr(tc, "id", None) or f"{step}.{idx}"


def _approval_required(tool_name: str, agency: str, registry) -> bool:
    """NE0 approval trigger (mechanism, not O6 policy): approve when the whole run is in confirm mode,
    or when the tool self-declares it always needs approval (e.g. shell_run's REQUIRES_APPROVAL)."""
    return agency == "confirm" or tool_name in getattr(registry, "approval_required_tools", set())


def _resolve_approval(approve, action: dict) -> bool:
    """Ask the injected approve callback for a decision. Fail-closed: no approver wired (server,
    scheduler, tests, non-TTY) → deny, so a dangerous tool never runs unattended by default."""
    if approve is None:
        return False
    try:
        return bool(approve(action))
    except (EOFError, KeyboardInterrupt):
        return False


def _dispatch_with_approval(tc, call_id, *, registry, context, agency, approve, log, rid):
    """Generator: request approval (if required) then dispatch one tool call. Yields protocol events
    (approval_required, tool_result) and RETURNS the result string that goes into the transcript. A
    denied call does not run and returns a denial message the model can react to."""
    name = tc.function.name
    args = tc.function.arguments
    if _approval_required(name, agency, registry):
        risk = "high" if name in getattr(registry, "approval_required_tools", set()) else "confirm"
        yield {"type": "approval_required", "call_id": call_id, "tool": name,
               "arguments": args, "risk": risk}
        if not _resolve_approval(approve, {"call_id": call_id, "tool": name,
                                           "arguments": args, "risk": risk}):
            log.info(f"[{rid}] tool {name} denied (call_id={call_id})")
            denied = f"Tool call to '{name}' was denied by the user; it did not run."
            yield {"type": "tool_result", "call_id": call_id, "name": name, "result": denied}
            return denied
    result = registry.dispatch_call(name, args, context=context)
    is_err = result.startswith(("Tool error", "Unknown tool", "Bad arguments"))
    log.log(
        logging.WARNING if is_err else logging.INFO,
        f"[{rid}] tool {name} -> {len(result)}c (call_id={call_id})"
        + (f" ERROR: {result[:200]}" if is_err else ""),
    )
    yield {"type": "tool_result", "call_id": call_id, "name": name, "result": result}
    return result


def _console_approve(action: dict) -> bool:
    """Console approver for the interactive CLI, installed by main() only when stdin is a TTY (never
    by the shared run_agent wrapper, so the server can't prompt on its own console). The NE2 shell
    will supply its own approve callback that drives the TUI prompt instead."""
    args = (action.get("arguments") or "")[:200].replace("\n", " ")
    try:
        ans = input(f"Approve {action.get('tool')}({args})? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans == "y"


def parse_args():
    import argparse

    p = argparse.ArgumentParser(description="Bob agent loop")
    p.add_argument("goal", nargs="+", help="Goal or task for the agent")
    p.add_argument(
        "--role", default=None, help="Model role override (default: routing.agentRole)"
    )
    p.add_argument(
        "--agency",
        default=None,
        choices=["silent", "show", "confirm"],
        help="Override agent.agency from config",
    )
    p.add_argument(
        "--notify", action="store_true", help="Write result to logs for toast notification"
    )
    p.add_argument("--notify-title", default="Bob", help="Toast notification title")
    p.add_argument("--exit-on-tool", default=None,
                   help="Comma-separated tool names: exit with code 42 after any of them fire")
    p.add_argument("--stream", action="store_true",
                   help="Stream the final answer token-by-token to stdout (M15)")
    return p.parse_args()


def truncate_history(messages: list, max_msgs: int, max_tokens: int = 0) -> list:
    """Sliding window that keeps the system message(s) + most recent turns.

    Trims by message count first (max_msgs), then by an optional token budget (max_tokens,
    M7): drop oldest non-system messages until the estimated total fits. The system
    message is always kept. An orphaned leading tool-response (whose assistant call got
    trimmed) is dropped so the remaining sequence stays valid for the OpenAI tool format.

    MEM-11 (compaction boundary — note only): this is a LOSSY drop-oldest window. Replacing the
    dropped span with a rolling summary (summarize-the-dropped-span, reusing
    bob_memory.summarize_turns — the MEM-4 core) is **Module O3**, which owns the rolling
    transcript. MEM-10's injection budget (bob_core.budget_injection) is the memory-side
    complement — it bounds what SAVED memory adds to the system message; O3 bounds the transcript
    itself. No code lands here for O3; this note marks the seam."""
    system = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]

    # 1. Message-count window.
    if len(system) + len(rest) > max_msgs:
        keep = max(0, max_msgs - len(system))
        rest = rest[-keep:]

    # 2. Token-budget window — keep as many recent messages as fit under the budget.
    if max_tokens:
        budget = max_tokens - sum(_message_tokens(m) for m in system)
        kept: list = []
        running = 0
        for m in reversed(rest):
            t = _message_tokens(m)
            if kept and running + t > budget:
                break
            running += t
            kept.append(m)
        rest = list(reversed(kept))

    # 3. Don't leave an orphaned tool response at the front.
    while rest and rest[0].get("role") == "tool":
        rest.pop(0)

    return system + rest


def build_tool_message(tc, result: str) -> dict:
    return {
        "role": "tool",
        "tool_call_id": tc.id,
        "content": result,
    }


def build_assistant_message(msg) -> dict:
    return {
        "role": "assistant",
        "content": msg.content,
        "tool_calls": [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in (msg.tool_calls or [])
        ],
    }


# --- M18/N5: structured logging, graceful interrupt --------------------------
#
# N4 (cold-start): the old module-level _REGISTRY_CACHE was removed — it was dead. Every real
# caller either passes a prebuilt registry (the server, built once at startup) or runs in a fresh
# process (CLI/voice), so an in-process singleton never amortized anything. Measured cold-start:
# interpreter ~31ms + import chain ~32ms + registry build ~140ms (cold) vs ~16ms (warm). The
# 140ms registry build is the dominant amortizable cost, and the path that actually amortizes it
# already exists: `bob agent serve` builds once and reuses it across turns. Voice / high-frequency
# clients should route through the server rather than paying a fresh cold build per invocation.


def _agent_logger(config: dict):
    """A 'bob.agent' logger writing structured lines to logs/bob-agent.log (per-run id lives in
    each message). Rotates at agent.logMaxBytes (keeping agent.logBackupCount old files) so the
    log can't grow unbounded across many runs (N5). Human-facing stderr previews stay separate
    for interactive use (M18)."""
    log = logging.getLogger("bob.agent")
    if not log.handlers:
        log.setLevel(logging.INFO)
        agent = config.get("agent", {})
        rel = agent.get("logFile", "logs/bob-agent.log").replace("\\", "/")
        path = REPO / rel
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            h = RotatingFileHandler(
                path,
                maxBytes=int(agent.get("logMaxBytes", 5_000_000)),
                backupCount=int(agent.get("logBackupCount", 3)),
                encoding="utf-8",
            )
            h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            log.addHandler(h)
            log.propagate = False
        except OSError:
            log.addHandler(logging.NullHandler())
    return log


class CancelToken:
    """Cooperative cancel shared by the loop, its LLM stream, and tool dispatch (N3). Thread-safe
    (wraps threading.Event) so a server request thread or a SIGINT handler can trip it while the
    loop reads it. A long in-flight tool can't be preempted, but the loop stops before the next
    tool and the LLM stream stops within one chunk (~1s).

    NE0/O1 seam — a token may be LINKED to a parent: cancelling the parent also cancels the child
    (checked lazily in cancelled()), so a sub-agent run started via child() is torn down when the
    parent run is cancelled. child() returns a fresh linked token to hand to the sub-run."""

    def __init__(self, parent: "CancelToken" = None):
        self._e = threading.Event()
        self._parent = parent

    def cancel(self) -> None:
        self._e.set()

    def cancelled(self) -> bool:
        return self._e.is_set() or (self._parent is not None and self._parent.cancelled())

    def child(self) -> "CancelToken":
        """A new token linked to this one — cancelling this (parent) cancels the child too (O1)."""
        return CancelToken(parent=self)


def _close_stream(stream_resp) -> None:
    """Best-effort close of a streaming response to abort the underlying HTTP request (N3)."""
    try:
        stream_resp.close()
    except Exception:
        pass


_NO_PREV_HANDLER = object()  # sentinel: we did NOT install (off main thread / signal error)


def _install_interrupt_handler(cancel: "CancelToken"):
    """Install a SIGINT handler that trips the shared cancel token instead of raising, so the loop
    aborts the in-flight step (stream stops within a chunk) and exits cleanly rather than dying
    mid-write (M18/N3). No-op off the main thread (a server request), where signal.signal is
    illegal — there the server trips the same token on client disconnect. Returns the previous
    handler (possibly None for a foreign handler), or _NO_PREV_HANDLER if nothing was installed."""
    if threading.current_thread() is not threading.main_thread():
        return _NO_PREV_HANDLER
    try:
        prev = signal.getsignal(signal.SIGINT)  # may be None if the prior handler was foreign (C)

        def _handler(signum, frame):
            cancel.cancel()
            print("\n[bob] interrupt — stopping...", file=sys.stderr)

        signal.signal(signal.SIGINT, _handler)
        return prev
    except (ValueError, OSError):
        return _NO_PREV_HANDLER


def _restore_interrupt_handler(prev):
    # Only skip when we never installed. If we DID install (prev captured, even as None for a
    # foreign handler), always restore so our handler isn't leaked; None -> SIG_DFL.
    if prev is _NO_PREV_HANDLER or threading.current_thread() is not threading.main_thread():
        return
    try:
        signal.signal(signal.SIGINT, prev if prev is not None else signal.SIG_DFL)
    except (ValueError, OSError):
        pass


# --- M15: unified completion call with optional token streaming --------------

_TOOL_OPEN = "<tool_call>"


def _prefix_overlap(s: str, marker: str) -> int:
    """Largest k (0..len(marker)-1) such that s ends with marker[:k] — i.e. s's tail could be the
    start of `marker`. Lets the hermes streamer hold back only the minimal tail that might begin a
    <tool_call> even when the marker is split across chunks (N6)."""
    k = min(len(s), len(marker) - 1)
    while k > 0:
        if s[-k:] == marker[:k]:
            return k
        k -= 1
    return 0


def _consume_stream(stream_resp, cancel=None, emit_tokens=True, hermes=True):
    """Iterate a streaming chat completion (the loop always streams internally now, N3 — non-stream
    agent mode passes emit_tokens=False and drops the tokens). Polls `cancel` between chunks and
    closes the stream promptly when tripped, so an in-flight call aborts within ~1s. Yields
    ('token', text) content deltas (only when emit_tokens) and returns
    SimpleNamespace(content, tool_calls, cancelled).

    Tool-call boundary handling (N6):
      * OpenAI (hermes=False): tool calls arrive as structured deltas, not text — stream every
        content delta and accumulate tool_acc; no marker logic.
      * Hermes (hermes=True): a prefix-buffer state machine holds back only the minimal tail that
        could begin a '<tool_call>' (split-safe), then suppresses the markup. At end, if no
        well-formed <tool_call> block parses, the withheld tail is flushed — so a final answer that
        merely contains the literal '<tool_call>' still streams in full instead of being swallowed."""
    content_parts: list = []
    tool_acc: dict = {}
    emitted = 0          # chars of the joined content already yielded as tokens
    buf = ""             # hermes: un-emitted tail that might begin a marker
    suppressing = False  # hermes: inside/after a confirmed tool_call marker
    cancelled = False

    def _hermes_feed(piece):
        """Yield the safe-to-emit prefix of a content piece; hold back a partial-marker tail."""
        nonlocal buf, suppressing, emitted
        if suppressing:
            return
        buf += piece
        idx = buf.find(_TOOL_OPEN)
        if idx != -1:                       # marker confirmed — emit text before it, then suppress
            head = buf[:idx]
            if emit_tokens and head:
                emitted += len(head)
                yield ("token", head)
            buf = ""
            suppressing = True
            return
        k = _prefix_overlap(buf, _TOOL_OPEN)  # hold back a possible partial marker at the tail
        safe = buf[:len(buf) - k] if k else buf
        buf = buf[len(buf) - k:] if k else ""
        if emit_tokens and safe:
            emitted += len(safe)
            yield ("token", safe)

    for chunk in stream_resp:
        if cancel is not None and cancel.cancelled():
            _close_stream(stream_resp)
            cancelled = True
            break
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        piece = getattr(delta, "content", None)
        if piece:
            content_parts.append(piece)
            if hermes:
                yield from _hermes_feed(piece)
            elif emit_tokens:
                emitted += len(piece)
                yield ("token", piece)
        for tcd in (getattr(delta, "tool_calls", None) or []):
            slot = tool_acc.setdefault(tcd.index, {"id": None, "name": "", "args": ""})
            if tcd.id:
                slot["id"] = tcd.id
            if tcd.function and tcd.function.name:
                slot["name"] += tcd.function.name
            if tcd.function and tcd.function.arguments:
                slot["args"] += tcd.function.arguments

    content = "".join(content_parts)
    if cancelled:
        return SimpleNamespace(content=content, tool_calls=None, cancelled=True)

    tool_calls = None
    if tool_acc:
        tool_calls = [
            SimpleNamespace(
                id=(s["id"] or f"call_{i}"),
                function=SimpleNamespace(name=s["name"], arguments=s["args"]),
            )
            for i, s in sorted(tool_acc.items())
        ]

    # Hermes: was the held/suppressed content really a tool call? If it doesn't parse, flush the
    # withheld remainder so nothing is silently swallowed (final answer containing the literal).
    if hermes and tool_calls is None:
        parsed = _parse_hermes_tool_calls(content) if _TOOL_OPEN in content else None
        if parsed:
            tool_calls = parsed
        elif emit_tokens and emitted < len(content):
            yield ("token", content[emitted:])

    return SimpleNamespace(content=content, tool_calls=tool_calls, cancelled=False)


def run_agent_events(
    goal: str,
    config: dict,
    role: str = None,
    agency: str = None,
    exit_on_tools: set = None,
    registry=None,
    stream: bool = False,
    history: list = None,
    cancel: "CancelToken" = None,
    run_id: str = None,
    approve=None,
    owner: str = None,
    agent_depth: int = 0,
    scope: str = None,
):
    """Generator core of the agent loop (M15). Yields event dicts:
        {"type": "token",             "text": str}                          # final-answer deltas (stream=True)
        {"type": "tool_call",         "call_id": str, "name": str, "arguments": str}
        {"type": "approval_required", "call_id": str, "tool": str, "arguments": str, "risk": str}  # NE0
        {"type": "tool_result",       "call_id": str, "name": str, "result": str}
        {"type": "final",             "result": str|None, "exit_requested": bool, "reason": str}
        {"type": "error",             "message": str}
    A terminal 'final' or 'error' is always the last event. run_agent() is the blocking wrapper
    used by the CLI; the server's SSE endpoint consumes these events directly. Pass a CancelToken
    (N3) to abort in-flight — SIGINT (CLI) and client-disconnect (server) both trip it; the run
    stops within ~1s with a final event reason='cancelled'.

    NE0 — approval is event-driven, not a blocking stdin prompt: before a tool that needs approval
    (agency=='confirm', or a tool with REQUIRES_APPROVAL like shell_run) the loop emits
    'approval_required' and consults `approve(action) -> bool`. `approve=None` fails closed (deny), so
    the server/scheduler never run a gated tool unattended; the CLI wrapper installs a console approver
    on a TTY, and the NE2 shell will pass one that drives the TUI. `call_id` correlates
    tool_call↔approval_required↔tool_result (forward-compat for O2 parallel tools)."""
    from bob_core import (MEMORY_CONTEXT_FRAME, _port, budget_injection, check_litellm,
                          get_llm_client, memory_profile_block, memory_recall, project_memory_block)

    agent_cfg = config.get("agent", {})
    effective_role = role or config.get("routing", {}).get("agentRole", "chat")
    effective_agency = agency or agent_cfg.get("agency", "show")
    max_steps = int(agent_cfg.get("maxSteps", 10))
    max_hist = int(agent_cfg.get("maxHistoryMsgs", 40))
    # M7 — token-aware context: cap history to a token budget (0 = count-only) and shrink
    # the injected tool schemas once the tool count crosses compactSchemasAfter.
    max_context_tokens = int(agent_cfg.get("maxContextTokens", 6000))
    compact_after = int(agent_cfg.get("compactSchemasAfter", 12))
    # Client-side timeout must be >= the proxy's request_timeout (600s): thinking models
    # (planner/R1) can run >2 min before first output. A low value would cut them off.
    request_timeout = int(agent_cfg.get("requestTimeout", 600))

    rid = run_id or uuid.uuid4().hex[:8]   # N5 — server passes its request id so one id spans client→server→loop
    log = _agent_logger(config)
    t_start = time.monotonic()
    reg_build_ms = 0.0

    # Build the registry if the caller didn't supply one (server passes its prebuilt, warm
    # registry). Timed for the N5 metrics line / N4 cold-start visibility.
    if registry is None:
        from tool_registry import ToolRegistry
        disabled_raw = agent_cfg.get("disabledTools", [])
        if isinstance(disabled_raw, str):
            disabled = {t.strip() for t in disabled_raw.split(",") if t.strip()}
        else:
            disabled = set(disabled_raw)
        _t0 = time.monotonic()
        registry = ToolRegistry.build(config, disabled)
        reg_build_ms = (time.monotonic() - _t0) * 1000

    tool_schemas = registry.tool_schemas
    exit_on_tools = exit_on_tools if exit_on_tools is not None else registry.exit_voice_tools
    exit_on_tools = exit_on_tools or set()

    # Pre-flight check
    if not check_litellm(config):
        port = _port(config, "litellmPort")
        msg = f"LiteLLM proxy not reachable at localhost:{port}. Run: bob up"
        log.error(f"[{rid}] preflight failed: {msg}")
        yield {"type": "error", "message": msg}
        return

    system_prompt = config.get("persona", {}).get(
        "systemPrompt", "You are Bob, a helpful AI assistant."
    )

    # M14 — OPTIONAL per-turn memory injection, gated on memory.autoRecall (NOT memory.enabled).
    # `enabled` only makes the memory_recall/store TOOLS available so the model recalls *sporadically*
    # when a request needs it; autoRecall (default off) is the heavier "read memory every turn" mode.
    # Keeping them separate stops the model reciting the same notes on every prompt. Best-effort: a
    # memory/embed failure is logged and skipped, never fatal to the run.
    mem_cfg = config.get("memory", {})
    # MEM-6/7 — resolve the identity (owner) + project scope this run acts as BEFORE any memory read.
    # Owner defaults to agent.defaultOwner (server passes the authed owner, the shell its self.owner,
    # O1 the parent's); scope is the project key threaded from the shell/CLI (None = global).
    owner = owner or config.get("agent", {}).get("defaultOwner", "local")

    # MEM-10 — gather every injected-memory block, then fit them into memory.maxInjectedTokens before
    # concatenating into the one system message truncate_history always keeps (so injected memory can't
    # overflow the context window). Priority (kept longest): BOB.md > profile > autoRecall.
    inject_blocks: list = []   # (label, text, priority)
    inject_budget = int(mem_cfg.get("maxInjectedTokens", 1200))

    if mem_cfg.get("autoRecall"):
        try:
            # A1 fix — recall as the RUN's owner/scope, not the 'local' default.
            recalled = memory_recall(goal, k=int(mem_cfg.get("recallK", 5)), config=config,
                                     owner=owner, scope=scope)
            if recalled and recalled.strip() and recalled != "(no results)":
                recalled = recalled[: inject_budget * 4]   # B4 — hard-cap autoRecall length
                inject_blocks.append(("autoRecall", MEMORY_CONTEXT_FRAME + "\n" + recalled, 1))
        except Exception as e:
            log.warning(f"[{rid}] memory recall skipped: {e}")
            print(f"[warn] memory recall skipped: {e}", file=sys.stderr)

    # MEM-3 — once-per-session profile injection. History-empty == a genuine session start (WI-6
    # repopulates history on resume, so this fires once at the real start, not every process launch).
    # agent_depth==0 restricts it to ROOT runs — an O1 sub-agent starts with empty isolated history by
    # design and must NOT inherit the profile block. Gated on memory.injectProfileAtStart (distinct
    # from autoRecall).
    if not history and agent_depth == 0:
        try:
            profile = memory_profile_block(owner=owner, config=config)
            if profile:
                inject_blocks.append(("profile", profile, 2))
        except Exception as e:
            log.warning(f"[{rid}] profile injection skipped: {e}")

        # MEM-7b — per-project instruction file(s) (BOB.md), once at session start on a root run.
        # `scope` is the project dir (git root/cwd); None → not in a project → skip.
        if scope:
            try:
                pm = project_memory_block(scope, config=config)
                if pm:
                    inject_blocks.append(("bobmd", pm, 3))
            except Exception as e:
                log.warning(f"[{rid}] project memory skipped: {e}")

    if inject_blocks:
        joined, kept, dropped = budget_injection(inject_blocks, inject_budget)
        if joined:
            system_prompt += "\n\n" + joined
            log.info(f"[{rid}] injected memory {kept} ({len(joined)}c)")
        if dropped:
            log.info(f"[{rid}] memory injection over budget; dropped {dropped}")

    tool_fmt = agent_cfg.get("toolFormat", "hermes").lower()
    hermes_mode = tool_fmt == "hermes"
    base_system = (
        system_prompt + _hermes_tool_system_addendum(tool_schemas, compact_after)
        if hermes_mode and tool_schemas
        else system_prompt
    )
    # Prior session turns (M12) are seeded between the system prompt and the new goal;
    # truncate_history keeps the whole thing within the token budget.
    messages = [{"role": "system", "content": base_system}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": goal})

    client = get_llm_client(config)
    exit_requested = False
    last_content = None
    steps_done = 0     # N5 metrics
    tools_run = 0
    tokens_est = 0

    log.info(
        f"[{rid}] start role={effective_role} agency={effective_agency} "
        f"tools={len(tool_schemas)} stream={stream} goal={goal[:200]!r}"
    )

    # OpenAI-mode tool payload, computed once (constant across steps): compacted past compact_after
    # so the per-turn schema tokens stay bounded (hermes mode injects schemas via base_system instead).
    openai_tools = (
        _openai_tools_payload(tool_schemas, compact_after)
        if (tool_schemas and not hermes_mode) else None
    )

    cancel = cancel or CancelToken()
    # NE0 — run-scoped context handed to each tool call (reachable via tool_registry.get_run_context()),
    # and the approve callback the loop consults before a tool that requires approval.
    run_ctx = RunContext(cancel=cancel, config=config, registry=registry, run_id=rid,
                         approve=approve, owner=owner, agent_depth=agent_depth, scope=scope)
    prev_sigint = _install_interrupt_handler(cancel)
    try:
        for step in range(max_steps):
            if cancel.cancelled():
                log.info(f"[{rid}] cancelled before step {step + 1}")
                yield {"type": "final", "result": _final_answer(last_content, hermes_mode),
                       "exit_requested": exit_requested, "reason": "cancelled"}
                return

            messages = truncate_history(messages, max_hist, max_context_tokens)
            tools = openai_tools

            # Unified LLM call (N3): always consume as a stream so `cancel` is polled between
            # chunks and an in-flight call aborts within ~1s. emit_tokens=stream gates whether
            # content deltas surface as 'token' events. One transient retry only when NOT emitting
            # (nothing surfaced yet); never mid-stream (that would re-emit tokens).
            msg = None
            attempts = 1 if stream else 2
            for attempt in range(attempts):
                try:
                    stream_resp = client.chat.completions.create(
                        model=effective_role, messages=messages, tools=tools,
                        stream=True, timeout=request_timeout,
                    )
                    gen = _consume_stream(stream_resp, cancel=cancel, emit_tokens=stream, hermes=hermes_mode)
                    while True:
                        try:
                            _kind, text = next(gen)
                            yield {"type": "token", "text": text}
                        except StopIteration as stop:
                            msg = stop.value
                            break
                    break
                except Exception as e:
                    if attempt + 1 < attempts and _is_transient(e):
                        log.warning(f"[{rid}] transient LLM error step {step + 1}: {e}")
                        print(f"[retry] transient LLM error at step {step + 1}: {e}", file=sys.stderr)
                        continue
                    log.error(f"[{rid}] LLM error step {step + 1}: {e}")
                    yield {"type": "error", "message": f"LLM error at step {step + 1}: {e}"}
                    return

            if getattr(msg, "cancelled", False):
                log.info(f"[{rid}] cancelled mid-stream step {step + 1}")
                yield {"type": "final", "result": _final_answer(last_content, hermes_mode),
                       "exit_requested": exit_requested, "reason": "cancelled"}
                return

            if not (msg.content or msg.tool_calls):  # empty completion — preserve the M3 guard
                log.error(f"[{rid}] empty response step {step + 1}")
                yield {"type": "error", "message": f"LLM returned an empty response at step {step + 1}"}
                return

            content = msg.content or ""
            last_content = content
            steps_done += 1
            tokens_est += _estimate_tokens(content)
            tool_calls = msg.tool_calls
            if not tool_calls and "<tool_call>" in content:
                tool_calls = _parse_hermes_tool_calls(content)

            log.info(f"[{rid}] step {step + 1} content_len={len(content)} tool_calls={len(tool_calls or [])}")

            # No tool calls — final answer.
            if not tool_calls:
                final = _strip_tool_calls(content) if hermes_mode else content
                log.info(f"[{rid}] final len={len(final)}")
                yield {"type": "final", "result": final,
                       "exit_requested": exit_requested, "reason": "answer"}
                return

            call_ids = [_call_id(tc, step, idx) for idx, tc in enumerate(tool_calls)]
            for tc, cid in zip(tool_calls, call_ids):
                yield {"type": "tool_call", "call_id": cid,
                       "name": tc.function.name, "arguments": tc.function.arguments}

            # NE0 — approval is per-tool-call and event-driven (see _dispatch_with_approval): the loop
            # emits approval_required and consults the injected approve callback. No blocking input()
            # here, so this works under the TUI/server, not only an interactive console.
            if hermes_mode:
                messages.append({"role": "assistant", "content": content})
                tool_results = []
                for tc, cid in zip(tool_calls, call_ids):
                    if cancel.cancelled():
                        yield {"type": "final", "result": _final_answer(last_content, hermes_mode),
                               "exit_requested": exit_requested, "reason": "cancelled"}
                        return
                    if tc.function.name in exit_on_tools:
                        exit_requested = True
                    result = yield from _dispatch_with_approval(
                        tc, cid, registry=registry, context=run_ctx,
                        agency=effective_agency, approve=approve, log=log, rid=rid)
                    tools_run += 1
                    tokens_est += _estimate_tokens(result)
                    tool_results.append(
                        f'<tool_response>{{"name": "{tc.function.name}", "content": {json.dumps(result)}}}</tool_response>'
                    )
                messages.append({"role": "user", "content": "\n".join(tool_results)})
            else:
                messages.append(build_assistant_message(msg))
                for tc, cid in zip(tool_calls, call_ids):
                    if cancel.cancelled():
                        yield {"type": "final", "result": _final_answer(last_content, hermes_mode),
                               "exit_requested": exit_requested, "reason": "cancelled"}
                        return
                    if tc.function.name in exit_on_tools:
                        exit_requested = True
                    result = yield from _dispatch_with_approval(
                        tc, cid, registry=registry, context=run_ctx,
                        agency=effective_agency, approve=approve, log=log, rid=rid)
                    tools_run += 1
                    tokens_est += _estimate_tokens(result)
                    messages.append(build_tool_message(tc, result))

        log.warning(f"[{rid}] stopped after {max_steps} steps without a final answer")
        print(f"Agent stopped after {max_steps} steps without a final answer.", file=sys.stderr)
        yield {"type": "final", "result": None, "exit_requested": exit_requested, "reason": "max_steps"}
    finally:
        _restore_interrupt_handler(prev_sigint)
        # N5 — one metrics line per run so a single `grep <rid>` reconstructs it end to end.
        log.info(
            f"[{rid}] done steps={steps_done} tools={tools_run} tokens~={tokens_est} "
            f"ms={(time.monotonic() - t_start) * 1000:.0f} registry_build_ms={reg_build_ms:.0f}"
        )


def run_agent(
    goal: str,
    config: dict,
    role: str = None,
    agency: str = None,
    exit_on_tools: set = None,
    registry=None,
    stream: bool = False,
    history: list = None,
    cancel: "CancelToken" = None,
    run_id: str = None,
    approve=None,
    owner: str = None,
    agent_depth: int = 0,
    scope: str = None,
) -> tuple[str | None, bool]:
    """Blocking wrapper over run_agent_events for the CLI: prints tool previews to stderr,
    streams/echoes the final answer to stdout, and returns (result, exit_requested).

    NE0 — `approve` is passed through neutrally (default None → fail-closed deny). The wrapper does
    NOT auto-install a console approver: the same wrapper serves the HTTP server (bob_agent_server),
    which must never prompt on its own console. The interactive CLI entry (main) installs the console
    approver explicitly on a TTY; the NE2 shell will pass its own TUI approver."""
    effective_agency = agency or config.get("agent", {}).get("agency", "show")
    result = None
    exit_requested = False
    streamed_any = False
    for ev in run_agent_events(
        goal, config, role=role, agency=agency,
        exit_on_tools=exit_on_tools, registry=registry, stream=stream, history=history,
        cancel=cancel, run_id=run_id, approve=approve, owner=owner, agent_depth=agent_depth,
        scope=scope,
    ):
        t = ev["type"]
        if t == "token":
            sys.stdout.write(ev["text"])
            sys.stdout.flush()
            streamed_any = True
        elif t == "tool_call":
            if effective_agency != "silent":
                preview = ev["arguments"][:120].replace("\n", " ")
                print(f"\033[36m  → {ev['name']}({preview})\033[0m", file=sys.stderr)
        elif t == "tool_result":
            if effective_agency != "silent":
                preview = ev["result"][:100] + ("..." if len(ev["result"]) > 100 else "")
                print(f"\033[90m    {preview}\033[0m", file=sys.stderr)
        elif t == "final":
            result = ev["result"]
            exit_requested = ev.get("exit_requested", False)
            if streamed_any:
                print()  # newline after streamed tokens
            elif result is not None:
                print(result)
        elif t == "error":
            print(ev["message"], file=sys.stderr)
            return None, exit_requested
    return result, exit_requested


def main():
    args = parse_args()
    from bob_core import load_config

    try:
        config = load_config()
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    goal = " ".join(args.goal)
    exit_on_tools = set(t.strip() for t in args.exit_on_tool.split(",") if t.strip()) if args.exit_on_tool else None
    # NE0 — interactive CLI: approve gated tools at the console when attached to a TTY (piped/CI → None
    # → fail-closed). The server passes no approver and so never prompts on its own console.
    approve = _console_approve if getattr(sys.stdin, "isatty", lambda: False)() else None
    result, exit_requested = run_agent(
        goal,
        config,
        role=args.role,
        agency=args.agency,
        exit_on_tools=exit_on_tools,
        stream=args.stream,
        approve=approve,
    )

    if args.notify and result:
        logs_dir = REPO / "logs"
        logs_dir.mkdir(exist_ok=True)
        # M16 — temp + atomic replace (same pattern as config.json) so a concurrent toast
        # reader never observes a half-written result file.
        dst = logs_dir / ".last-agent-result.txt"
        tmp = logs_dir / f".last-agent-result.{os.getpid()}.tmp"
        tmp.write_text(
            result[: config.get("agent", {}).get("maxResultChars", 500)],
            encoding="utf-8",
        )
        os.replace(tmp, dst)

    if exit_requested:
        sys.exit(42)


if __name__ == "__main__":
    main()
