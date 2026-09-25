#!/usr/bin/env python3
"""Bob agent loop — LLM reasons about what tools to call, executes them, loops until done.

Entered via `bob agent <goal>` (cli.py) and, in-process, by the scheduled-agent runner
(scripts/tools/schedule.py:run_due_schedules, fired by scripts/bob_agent_runner.py).
All config is resolved by bob_core.load_config (config/defaults.json + config/user.json; one Python
resolver).
"""
import base64
import hashlib
import json
import logging
import mimetypes
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

from bob_tracing import Tracer, make_tracer   # import-light span seam (stdlib-only at rest)

# Fallback tracer for a dispatch whose context carries none (older callers / unit tests). Disabled,
# so every span() is the shared no-op — behaviorally inert (identical to running with tracing off).
_NOOP_TRACER = Tracer(enabled=False)


# The one token estimator lives in bob_core; the loop keeps its historical name for it.
from bob_core import est_tokens as _estimate_tokens, tokens_to_chars  # noqa: E402

# Flat token cost of one image content block. An image's prompt cost depends on its pixels (the vision
# encoder's patch count), not on the length of its base64 text, so it is charged at a fixed estimate
# sized for bob_vision.resize_image's 1024px output.
_IMAGE_BLOCK_TOKENS = 1024


def _content_tokens(content) -> int:
    """Estimated tokens of a message's content: a string by length; an OpenAI content-block list by its
    text parts plus _IMAGE_BLOCK_TOKENS per image block."""
    if isinstance(content, str):
        return _estimate_tokens(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, dict) and block.get("type") == "image_url":
                total += _IMAGE_BLOCK_TOKENS
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                total += _estimate_tokens(block["text"])
            else:
                total += _estimate_tokens(json.dumps(block))
        return total
    return _estimate_tokens(json.dumps(content)) if content else 0


def _message_tokens(m: dict) -> int:
    """Estimated token cost of a single chat message, including tool-call payloads."""
    total = _content_tokens(m.get("content") or "") + 4  # per-message role/format overhead
    for tc in (m.get("tool_calls") or []):
        total += _estimate_tokens(json.dumps(tc))
    return total


# Floor the history budget never drops below, even when the system prompt alone eats it: sending zero
# conversational turns leaves the model nothing to answer. Scaled down for small budgets so a tight
# explicit maxContextTokens is still honoured.
_MIN_TAIL_TOKENS = 512
_CLAMP_MARKER = "\n\n[...middle truncated to fit the context budget...]\n\n"


def _tools_payload_tokens(tools) -> int:
    """Estimated cost of the `tools=` request payload. In OpenAI tool mode (and on a grammar-
    constrained hermes call) the schemas ride on the REQUEST, not inside a message, so the history
    budget has to reserve room for them or the assembled prompt overflows the backend's context
    window. Hermes mode bakes its addendum into the system message, where _message_tokens counts it."""
    if not tools:
        return 0
    return _estimate_tokens(json.dumps(tools)) + 4


def _clamp_message(m: dict, budget: int) -> dict:
    """A copy of `m` whose text content fits `budget` tokens, with the middle dropped and a marker
    left in its place. Used when the newest message alone exceeds the whole history budget (a pasted
    reference document, say): sending it whole is what overflows the backend, and dropping it leaves
    the model nothing to work from. Head and tail both survive, since the instruction usually opens
    the message and the recent ask usually closes it. Non-string content (image blocks) is returned
    unchanged, because cutting it would corrupt the block structure."""
    content = m.get("content")
    if not isinstance(content, str) or budget <= 0:
        return m
    overhead = _message_tokens({**m, "content": ""}) + _estimate_tokens(_CLAMP_MARKER)
    room = budget - overhead
    if room <= 0:
        return m
    chars = tokens_to_chars(room)
    if len(content) <= chars:
        return m
    head = chars // 2
    return {**m, "content": content[:head] + _CLAMP_MARKER + content[-(chars - head):]}


def _tail_budget(max_tokens: int, head_tokens: int, reserve: int = 0) -> int:
    """The token budget left for the sliding tail after the always-kept head (system messages, plus
    any pinned goal) and `reserve` (a compaction note). Never returns less than the floor, and says
    so in the log when the head has eaten the budget, because that is a misconfiguration the artist
    of the config needs to see rather than an overflow at the backend."""
    budget = max_tokens - head_tokens - reserve
    floor = min(_MIN_TAIL_TOKENS, max(1, max_tokens // 4))
    if budget < floor:
        logging.getLogger("bob.agent").warning(
            "context budget exhausted before history: maxContextTokens=%d head=%d reserve=%d; "
            "flooring the tail at %d tokens", max_tokens, head_tokens, reserve, floor)
        budget = floor
    return budget


def _image_content_block(src: str) -> dict:
    """Normalize one image source into an OpenAI `image_url` content block. Accepts a
    `data:` / `http(s)://` URL (passed through unchanged) or a local file path (read + base64 → a
    `data:` URI). Single home for image encoding so the goal-level path, the (future) tool-result
    path, and the describe/screenshot doors all emit byte-identical blocks (DRY)."""
    if src.startswith(("data:", "http://", "https://")):
        url = src
    else:
        data = Path(src).read_bytes()
        mime = mimetypes.guess_type(src)[0] or "image/png"
        url = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    return {"type": "image_url", "image_url": {"url": url}}


def _split_tool_result_images(result) -> tuple:
    """The tool-result image contract. A tool signals image output by returning a JSON object
    `{"__images__": ["<src>"...], "text": "<summary>"}` (src = path / data: / http(s) URL). Returns
    `(text_for_transcript, [image_srcs])`. Any other result — a plain string, or JSON without the
    reserved key — returns `(result, [])` unchanged, so non-image tools stay byte-identical. The fast
    substring guard avoids JSON-parsing every ordinary tool result."""
    if not isinstance(result, str) or "__images__" not in result:
        return result, []
    try:
        obj = json.loads(result)
    except (ValueError, TypeError):
        return result, []
    if not isinstance(obj, dict) or not isinstance(obj.get("__images__"), list):
        return result, []
    images = [s for s in obj["__images__"] if isinstance(s, str)]
    text = obj.get("text") or obj.get("content") or f"[tool returned {len(images)} image(s)]"
    return str(text), images


def _is_transient(e) -> bool:
    """True for LLM errors worth a retry: connection / timeout / 5xx / 429. A llama-swap model swap
    500s the first request after an idle-unload ('upstream command exited prematurely') and recovers
    once the subprocess relaunches — so these are retryable after a short backoff."""
    if type(e).__name__ in (
        "APIConnectionError", "APITimeoutError", "InternalServerError",
        "RateLimitError", "ConnectionError", "Timeout", "ReadTimeout",
    ):
        return True
    return getattr(e, "status_code", None) in (429, 500, 502, 503, 504)


def _is_unsupported_constraint(e) -> bool:
    """True when a request failed specifically because the backend didn't accept the tool-call
    constraint kwargs (tools / tool_choice): a 400/BadRequest whose message names one of them, or a
    client that doesn't accept the kwarg at all ('unexpected keyword argument'). Lets the loop drop the
    constraint for the rest of the run and fall back to today's hermes-text parse, so a non-grammar
    endpoint degrades gracefully instead of erroring. Distinct from _is_transient (do NOT retry-loop)."""
    msg = str(e).lower()
    named = any(k in msg for k in ("tool_choice", "tools", "response_format", "grammar",
                                   "json_schema", "not supported", "unsupported"))
    status = getattr(e, "status_code", None)
    return ("unexpected keyword argument" in msg
            or ((status == 400 or "badrequest" in type(e).__name__.lower()) and named))


def _sleep_cancellable(seconds: float, cancel=None, tick: float = 0.25) -> None:
    """Sleep up to `seconds`, returning early if `cancel` trips — keeps retry backoff from delaying
    an abort by more than `tick` (responsiveness)."""
    end = time.monotonic() + max(0.0, seconds)
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0 or (cancel is not None and cancel.cancelled()):
            return
        time.sleep(min(tick, remaining))


def _first_json_object(s: str) -> str | None:
    """Return the first balanced {...} JSON object at the start of `s` (ignoring leading space), or None.
    Brace-counts with string/escape awareness so a small model that omits the closing </tool_call> tag
    still yields a parseable object."""
    s = s.lstrip()
    if not s.startswith("{"):
        return None
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[: i + 1]
    return None


def _unclosed_tool_call_blocks(content: str) -> list:
    """Fallback when no well-formed <tool_call>…</tool_call> pairs exist: small models often emit the
    opening tag and the JSON but drop the closing tag. Take the JSON object after each opening tag."""
    out = []
    for part in content.split(_TOOL_OPEN)[1:]:
        obj = _first_json_object(part.split("</tool_call>")[0])
        if obj:
            out.append(obj)
    return out


def _parse_hermes_tool_calls(content: str) -> list | None:
    """Parse <tool_call> blocks from Hermes-format content.
    Handles both JSON-inside and XML-sub-element variants.
    Tolerates a missing closing </tool_call> tag (common with small models), so the call is executed
    instead of leaking as raw text in the answer.
    Malformed JSON blocks are returned as __parse_error__ calls so the LLM
    can see the failure and self-correct, rather than being silently dropped.
    """
    blocks = re.findall(r"<tool_call>(.*?)</tool_call>", content, re.DOTALL)
    if not blocks and _TOOL_OPEN in content:
        blocks = _unclosed_tool_call_blocks(content)   # missing close tag → recover the JSON object
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
                    # Mirror the JSON path: surface malformed <arguments> as a parse-error
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
    # Only paired tags are stripped; a real unclosed <tool_call>{json} is routed to the tool path by
    # _parse_hermes_tool_calls (so it never reaches here as an answer), while a bare literal mention of
    # "<tool_call>" in a genuine answer must survive intact.
    return re.sub(r"<tool_call>.*?</tool_call>", "", content, flags=re.DOTALL).strip()


def _final_answer(text, hermes: bool):
    """Clean a (possibly partial) final answer: in hermes mode strip tool-call markup so a
    cancelled/interrupted run never returns raw <tool_call> text as if it were the answer."""
    return _strip_tool_calls(text) if (hermes and text) else text


def _visible_text(content: str, hermes: bool) -> str:
    """The answer text of a reply cut off mid-output: paired tool-call markup removed and, in hermes
    mode, everything from an unclosed <tool_call> onward dropped (that call was never finished)."""
    if not content:
        return ""
    if not hermes:
        return content.strip()
    text = _strip_tool_calls(content)
    cut = text.find(_TOOL_OPEN)
    return (text[:cut] if cut != -1 else text).strip()


# Context window assumed for a model whose window the registry can't tell us (a cloud peer with no
# contextWindow, an unreadable registry), when agent.maxContextTokens is auto.
_FALLBACK_WINDOW = 8192


def _context_budget(config: dict, role: str, explicit, output_tokens: int, tools_tokens: int) -> tuple:
    """(total, output, send) token budgets for one request on `role`. `total` is the model's per-request
    window (bob_core.role_window) when agent.maxContextTokens is 0/'auto', else min(explicit, window).
    `output` is the generation asked for (max_tokens), clamped to half the window. `send` is what the
    history may use: total minus the output reservation and the request-borne tool schemas."""
    from bob_core import cap_output, request_window

    total = request_window(config, role, explicit) or _FALLBACK_WINDOW
    output = cap_output(output_tokens, total)
    return total, output, max(0, total - tools_tokens - output)


def _compact_schema(fn: dict) -> dict:
    """Strip verbose descriptions from a function schema, keeping the callable contract
    (name, param names, types/enums, required). Used when the tool count is high so the
    fixed per-turn prompt overhead doesn't grow linearly with the number of tools."""
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
    a large plugin set doesn't silently eat a small local context window."""
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


# A small window (the cpu profile's 4096 tokens, a 4096-token vision model) cannot hold a large tool set,
# a reply and some history at once. The reply the fit keeps room for, at least, before tools are dropped:
_MIN_OUTPUT_TOKENS = 256
# compact_after value that compacts every schema, whatever the tool count.
_ALWAYS_COMPACT = -1
# Tools a fitted run keeps however small the window: reading and editing files, the shell, memory, the
# web and the TODO list. Everything else is dropped first, largest schema first.
_CORE_TOOL_PREFIXES = ("file_", "shell_", "memory_", "web_", "todo_")


def _tool_name(schema: dict) -> str:
    return (schema.get("function") or {}).get("name", "")


def _tail_reserve(window: int) -> int:
    """History room a request keeps beyond its system head: the floor _tail_budget never goes below."""
    return min(_MIN_TAIL_TOKENS, max(1, window // 4))


def _schema_head_tokens(system_prompt: str, tool_schemas: list, *, hermes: bool, compact_after: int,
                        constrained: bool) -> tuple:
    """(system-message tokens, request-borne schema tokens) for a tool set: hermes mode bakes the schemas
    into the system message; OpenAI mode, and a grammar-constrained hermes call, send them as `tools=`."""
    base = system_prompt
    if hermes and tool_schemas:
        base += _hermes_tool_system_addendum(tool_schemas, compact_after)
    payload = None
    if tool_schemas and (not hermes or constrained):
        payload = _openai_tools_payload(tool_schemas, compact_after)
    return _message_tokens({"role": "system", "content": base}), _tools_payload_tokens(payload)


def _fit_tools_to_window(system_prompt: str, tool_schemas: list, *, window: int, output: int, hermes: bool,
                         compact_after: int, constrained: bool, reserve: int = 0) -> tuple:
    """Fit the fixed head (system prompt + tool schemas) so a reply of min(output, _MIN_OUTPUT_TOKENS), the
    minimal history and `reserve` more tokens (a pending image turn) still fit `window`: first compact every
    schema, then drop non-core tools (_CORE_TOOL_PREFIXES are kept), largest schema first. Returns
    (tool_schemas, compact_after, dropped names); unchanged when the head already fits."""
    reply = min(max(1, int(output)), _MIN_OUTPUT_TOKENS)

    def fits(schemas, ca):
        sys_t, req_t = _schema_head_tokens(system_prompt, schemas, hermes=hermes, compact_after=ca,
                                           constrained=constrained)
        return sys_t + req_t + reply + _tail_reserve(window) + reserve <= window

    if not tool_schemas or fits(tool_schemas, compact_after):
        return tool_schemas, compact_after, []
    compact_after = _ALWAYS_COMPACT
    schemas = list(tool_schemas)
    droppable = sorted((s for s in schemas if not _tool_name(s).startswith(_CORE_TOOL_PREFIXES)),
                       key=lambda s: len(json.dumps(s)), reverse=True)
    dropped = []
    for victim in droppable:
        if fits(schemas, compact_after):
            break
        schemas.remove(victim)
        dropped.append(_tool_name(victim))
    return schemas, compact_after, dropped


class RunContext:
    """Run-scoped services carried into dispatch_call and reachable by a tool via
    tool_registry.get_run_context(): the cancel token, the resolved config, the tool registry, the
    run id, and the approval callback. Lets a tool (a future sub-agent tool) reach these without any
    change to its fn(**args) signature."""
    __slots__ = ("cancel", "config", "registry", "run_id", "approve", "owner", "agent_depth", "scope",
                 "policy", "todos", "tracer", "trace_span", "allowed_roles", "session_id", "unattended_allow")

    def __init__(self, cancel, config, registry, run_id, approve, owner="local", agent_depth=0,
                 scope=None, policy=None, todos=None, tracer=None, trace_span=None, allowed_roles=None,
                 session_id=None, unattended_allow=None):
        self.cancel = cancel
        self.config = config
        self.registry = registry
        self.run_id = run_id
        self.approve = approve
        # The run's Tracer + its current parent span, so dispatch_call can open a child tool span
        # and spawn_agent can parent a sub-run's span. Disabled tracer => all no-op (behaviorally inert).
        self.tracer = tracer if tracer is not None else _NOOP_TRACER
        self.trace_span = trace_span
        # Run-local living TODO list (list of {task, status}); the `todo` tool mutates it and the
        # recitation hook re-emits the open items at the context tail. Per-run (sub-agents get their own).
        self.todos = todos if todos is not None else []
        # The allow|ask|deny PermissionPolicy for this run (built from config once; None == the
        # default where only the approval floor prompts). Reachable by dispatch via get_run_context().
        self.policy = policy
        # The identity this run acts as (memory scoping) and its delegation depth. `agent_depth`
        # is 0 for a root run; sub-agent spawning sets it >0 and propagates owner/cancel parent->child.
        self.owner = owner
        self.agent_depth = agent_depth
        # Project scope (git-root/cwd key) for this run; None = global. Threaded from the
        # shell/CLI; the memory tools read it via get_run_context() to scope project-type facts.
        self.scope = scope
        # Model roles this run's caller may use (the agent API token's `role:<name>` scopes); None means
        # unrestricted. spawn_agent enforces it so a sub-run can't reach a role the caller couldn't.
        self.allowed_roles = set(allowed_roles) if allowed_roles else None
        # The persisted session (shell / agent API) this run belongs to, stamped on transcript rows so
        # `bob memory forget --session` reaches them. None for a stateless run.
        self.session_id = session_id
        # A surface with no operator (MCP): the tool names agent.mcpAllowTools lets run there. None for an
        # attended run. dispatch_with_approval refuses gated tools not in it; spawn_agent hands it to the sub-run.
        self.unattended_allow = frozenset(unattended_allow) if unattended_allow is not None else None


def _call_id(tc, step: int, idx: int) -> str:
    """Stable id correlating a tool_call event with its tool_result (and approval_required). OpenAI
    tool_calls carry an `id`; hermes-parsed calls don't, so fall back to step.idx. Forward-compat for
    parallel tools, where results arrive out of order and must be matched by id."""
    return getattr(tc, "id", None) or f"{step}.{idx}"


# The approval gate is shared by every front door (loop, MCP, `bob --run`, skill steps) and lives in
# bob_permissions; these names are the loop's handles on it.
from bob_permissions import (approval_required as _approval_required, audit as _audit,  # noqa: E402
                             dispatch_with_approval as _dispatch_with_approval,
                             fire_post_hooks as _fire_post_hooks, fire_pre_hooks as _fire_pre_hooks,
                             render_preview as _render_preview, resolve_approval as _resolve_approval,
                             unattended_refusal as _unattended_refusal)


def _fire_stop_hooks(registry, final, context, log, rid):
    """Run Stop hooks; each may return {'inject': str} to nudge the run to continue. Returns the first
    injected string, or None to finalize. Fail-safe."""
    for hook in getattr(registry, "hooks", {}).get("Stop", []):
        try:
            out = hook(final, context)
        except Exception as e:
            log.warning(f"[{rid}] Stop hook error (ignored): {e}")
            continue
        if out and isinstance(out.get("inject"), str) and out["inject"].strip():
            return out["inject"]
    return None


def _parallel_cap(max_parallel) -> int:
    """Effective parallel worker count: 1 (sequential — the default behavior) unless
    maxParallelTools>1, then min(maxParallelTools, cpu-2) with a floor of 1."""
    try:
        mp = int(max_parallel)
    except (TypeError, ValueError):
        mp = 1
    if mp <= 1:
        return 1
    cpu = os.cpu_count() or 2
    return max(1, min(mp, max(cpu - 2, 1)))


def _parallel_eligible(name: str, registry, ctx, agency: str) -> bool:
    """A call may run concurrently only if it is side-effect-free AND unconditionally allowed —
    not in mutating_tools, not approval-gated (approval floor), and the policy resolves to 'allow'.
    Anything that could deny/ask or mutate stays sequential (it needs the event/approval flow, and a
    mutation must not race another tool)."""
    if name in getattr(registry, "mutating_tools", set()):
        return False
    if _approval_required(name, agency, registry):
        return False
    unattended = getattr(ctx, "unattended_allow", None)
    if unattended is not None and _unattended_refusal(registry, name, unattended):
        return False
    policy = getattr(ctx, "policy", None)
    if policy is not None and policy.resolve(
            name, owner=getattr(ctx, "owner", "local"),
            agent_depth=getattr(ctx, "agent_depth", 0), mutating=False) != "allow":
        return False
    return True


def _call_sig(tc) -> str:
    """Normalized signature of a tool call (name + canonical args) for duplicate detection — so
    identical calls are recognised regardless of key order / whitespace in the arguments JSON."""
    raw = tc.function.arguments or ""
    try:
        args = json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":"))
    except Exception:  # noqa: BLE001 — malformed args: compare the raw text
        args = raw.strip()
    return f"{tc.function.name}:{args}"


_BLOCKED_DUP_MSG = ("(Repeated call blocked — you already ran this exact tool call earlier in this "
                    "turn and it returned the same result. Do NOT call it again; use what you have "
                    "and give your final answer now.)")
_STUCK_ANSWER_MSG = ("I got stuck repeating the same lookup and couldn't make progress. Could you "
                     "rephrase or give me a bit more detail?")


def _run_tool_calls(tool_calls, call_ids, *, registry, run_ctx, agency, approve, log, rid,
                    cancel, exit_on_tools, max_parallel, call_counts=None, dup_limit=0):
    """Dispatch a step's tool calls, yielding tool_call-result / approval events and RETURNING an
    ordered result list (via StopIteration.value). When maxParallelTools>1, side-effect-free
    'allow' calls run concurrently in a bounded ThreadPoolExecutor while mutating / ask / deny /
    approval-gated calls stay sequential through _dispatch_with_approval. Results are appended in
    ORIGINAL order so the transcript is deterministic regardless of completion order; cap==1 is
    byte-identical to the sequential loop.

    Returns dict(results=[(tc, cid, result), ...], exit_requested, tools_run, tokens_est, cancelled).
    On cancel it stops before dispatching the next call, abandons not-yet-started futures (a running
    tool can't be preempted), and sets cancelled=True for the caller to end the run."""
    out = {"results": [], "exit_requested": False, "tools_run": 0, "tokens_est": 0, "cancelled": False,
           "blocked": 0}
    call_counts = call_counts if call_counts is not None else {}
    cap = _parallel_cap(max_parallel)
    # Duplicate guard: a call whose signature has already run dup_limit times this run is BLOCKED —
    # short-circuited with a nudge instead of re-dispatched — so a model fixating on one tool can't
    # spin forever. Computed from the pre-step counts (identical calls WITHIN one step still run).
    blocked = [dup_limit > 0 and call_counts.get(_call_sig(tc), 0) >= dup_limit for tc in tool_calls]
    eligible = ([(not blocked[i]) and _parallel_eligible(tc.function.name, registry, run_ctx, agency)
                 for i, tc in enumerate(tool_calls)]
                if cap > 1 else [False] * len(tool_calls))

    ex = None
    futs = {}
    try:
        if cap > 1 and any(eligible):
            from concurrent.futures import ThreadPoolExecutor
            ex = ThreadPoolExecutor(max_workers=cap)
            for i, tc in enumerate(tool_calls):
                if eligible[i] and not cancel.cancelled():
                    futs[i] = ex.submit(registry.dispatch_call, tc.function.name,
                                        tc.function.arguments, run_ctx)
        for i, (tc, cid) in enumerate(zip(tool_calls, call_ids)):
            if cancel.cancelled():
                out["cancelled"] = True
                break
            if blocked[i]:
                log.info(f"[{rid}] tool {tc.function.name} BLOCKED (repeat >= {dup_limit}) (call_id={cid})")
                out["blocked"] += 1
                out["tools_run"] += 1
                out["results"].append((tc, cid, _BLOCKED_DUP_MSG))
                yield {"type": "tool_result", "call_id": cid, "name": tc.function.name,
                       "result": _BLOCKED_DUP_MSG}
                continue
            call_counts[_call_sig(tc)] = call_counts.get(_call_sig(tc), 0) + 1
            if tc.function.name in exit_on_tools:
                out["exit_requested"] = True
            if i in futs:
                # Parallel, side-effect-free 'allow' path — audit still recorded, then the
                # already-running dispatch is awaited and its result surfaced in original order.
                _audit(log, rid, tc.function.name, tc.function.arguments, "allow", getattr(run_ctx, "owner", "local"))
                result = futs[i].result()
                log.info(f"[{rid}] tool {tc.function.name} -> {len(result)}c (call_id={cid}) [parallel]")
                yield {"type": "tool_result", "call_id": cid, "name": tc.function.name, "result": result}
            else:
                result = yield from _dispatch_with_approval(
                    tc, cid, registry=registry, context=run_ctx,
                    agency=agency, approve=approve, log=log, rid=rid)
            out["tools_run"] += 1
            out["tokens_est"] += _estimate_tokens(result)
            out["results"].append((tc, cid, result))
    finally:
        if ex is not None:
            ex.shutdown(wait=False, cancel_futures=True)
    # The whole step was repeats (no fresh dispatch) — the caller uses this to force a final answer.
    out["fully_blocked"] = bool(tool_calls) and out["blocked"] == len(tool_calls)
    return out


def _console_approve(action: dict) -> bool:
    """Console approver for the interactive CLI, installed by main() only when stdin is a TTY (never
    by the shared run_agent wrapper, so the server can't prompt on its own console). The interactive shell
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
                   help="Stream the final answer token-by-token to stdout")
    p.add_argument("--deep", action="store_true",
                   help="Enable plan + verify + self-repair for this run")
    return p.parse_args()


# Structured-compaction prompt: preserve the categories that matter for RESUMING a long task,
# not a lossy prose blob. Reused via bob_memory.summarize_turns (the consolidation core), no new model dep.
_COMPACT_FRAME = "[conversation so far — compacted]"
_COMPACT_SYSTEM = (
    "You are compacting an agent's earlier conversation so it fits a token budget without losing "
    "what's needed to continue the task. Write a terse note capturing ONLY: the user's goal, key "
    "decisions made, concrete facts/values discovered, files or resources touched, and any unresolved "
    "problems, TODOs, or errors. Omit small talk and verbatim tool output. Use short bullet lines."
)


# Optional plan/verify turns (default off). Both reuse the streaming path (so the fake test
# clients + cancel handling apply) but surface NO 'token' events — they're internal reasoning turns.
_PLAN_SYSTEM = (
    "You are planning how an AI agent with tools should accomplish the user's task. Output a SHORT "
    "numbered list (3-6 steps) of concrete actions — no prose, no preamble. If the task is trivial, "
    "output a single step."
)
_VERIFY_SYSTEM = (
    "You are reviewing whether the assistant's final answer fully satisfies the user's goal and that "
    "no tool silently failed or was skipped. If it is complete and correct, reply with exactly the "
    "word DONE. Otherwise reply with a terse list of what is missing or wrong."
)


def _is_local_model(model: str, config: dict) -> bool:
    """True if `model` is a locally served (llama-swap) role rather than a cloud peer. Scopes the
    reasoning chat-template kwarg to local models: llama-server consumes `enable_thinking`, cloud peers
    (DeepSeek/GLM) have their own reasoning behavior and don't take it. Best-effort: on any lookup
    failure treat it as local, so local reasoning suppression (the common path) still applies."""
    from bob_core import is_local_role
    return is_local_role(model, config)


def _single_turn(client, role, messages, cancel, timeout, hermes, extra_body=None) -> tuple:
    """One internal, non-emitting LLM turn (plan or verify). Consumed as a stream so `cancel` is
    honored, but yields no 'token' events. Returns (content, error): content is '' on empty / cancel,
    and `error` is the failure text when the call raised (None otherwise), so the caller can say so
    instead of mistaking a failed verify for an accepted answer. `extra_body` carries the same
    reasoning-mode kwarg as the main loop so internal turns reason consistently."""
    try:
        kwargs = dict(model=role, messages=messages, tools=None, stream=True, timeout=timeout)
        if extra_body is not None:
            kwargs["extra_body"] = extra_body
        resp = client.chat.completions.create(**kwargs)
        gen = _consume_stream(resp, cancel=cancel, emit_tokens=False, hermes=hermes)
        msg = None
        while True:
            try:
                next(gen)
            except StopIteration as stop:
                msg = stop.value
                break
        if msg is None or getattr(msg, "cancelled", False):
            return "", None
        return getattr(msg, "content", None) or "", None
    except Exception as e:  # noqa: BLE001 (reported to the caller, which decides how to degrade)
        return "", str(e) or type(e).__name__


def _recitation_block(goal: str, todos, max_items: int = 10, max_task_chars: int = 200) -> str:
    """The goal-recitation text re-emitted at the CONTEXT TAIL each step (beats 'lost in the
    middle'): the standing goal plus the still-open TODO items (the plan phase seeds them; the `todo` tool
    keeps them living). Bounded. Always returns at least the goal reminder when recitation is on."""
    lines = [f"[reminder] Current goal: {goal.strip()[:400]}"]
    open_items = [t for t in (todos or []) if t.get("status") != "done"]
    if open_items:
        lines.append("Open TODO (keep working until these are done):")
        for t in open_items[:max_items]:
            mark = "~" if t.get("status") == "in_progress" else " "
            lines.append(f"- [{mark}] {str(t.get('task', '')).strip()[:max_task_chars]}")
    return "\n".join(lines)



def _compact_span(dropped: list, model: str, max_tokens: int, config: dict = None) -> str:
    """Summarize the dropped span into a structured compaction note through bob_core.complete (input
    fitted to the model's window, thinking off). A failure is logged and returns "", so the caller falls
    back to plain truncation instead of failing the run."""
    from bob_core import complete, load_config

    convo = []
    for m in dropped:
        content = m.get("content")
        if content is None or m.get("role") == "system":
            continue
        text = content if isinstance(content, str) else json.dumps(content)
        convo.append({"role": m.get("role"), "content": text})
    if not convo:
        return ""
    try:
        cfg = config if config is not None else load_config()
        text, _finish = complete(cfg, model, [{"role": "system", "content": _COMPACT_SYSTEM},
                                              {"role": "user", "content": json.dumps(convo)}],
                                 max_tokens, timeout=int(cfg.get("agent", {}).get("requestTimeout", 600)))
    except Exception as e:  # noqa: BLE001 (compaction is best-effort by design)
        logging.getLogger("bob.agent").warning("compaction summary failed (model=%s): %s", model, e)
        return ""
    return (text or "").strip()


def _suffix_that_fits(msgs: list, budget, limit: int) -> int:
    """The largest n <= `limit` such that the newest n of `msgs` fit `budget` tokens (budget None or 0
    means unbounded)."""
    n, used = 0, 0
    for m in reversed(msgs[len(msgs) - min(limit, len(msgs)):] if limit else []):
        t = _message_tokens(m)
        if budget and used + t > budget:
            break
        used += t
        n += 1
    return n


def _summarize_keep(original: list, tail: list, keep_last: int, budget) -> list:
    """The verbatim tail kept by summarize compaction: the budget window's `tail`, grown back towards the
    last `keep_last` messages only as far as they still fit `budget`, so the kept turns plus the summary
    never exceed the budget the window was computed for."""
    grow = _suffix_that_fits(original, budget, max(keep_last, len(tail)))
    if grow > len(tail):
        return original[-grow:]
    return tail


def _truncate_stable_prefix(messages: list, max_msgs: int, max_tokens: int, *,
                            keep_last: int, summary_max_tokens: int, summary_model: str,
                            pin_goal: dict, summarize: bool, config: dict = None) -> list:
    """Prefix-cache-aware variant of truncate_history (stablePrefix=on).

    Keeps a FROZEN head — base system message(s) + the single compaction summary block + the pinned
    goal — byte-stable across turns, and only slides the recent tail. On a compaction event the
    summary block is *appended to* (its existing bytes never rewritten), so llama.cpp's KV prefix
    cache is reused up to the previous divergence point instead of being busted every turn. Between
    compaction events the whole head is byte-identical, so the shared prefix keeps growing with the
    tail. See truncate_history for the semantics of each window step; only the layout differs."""
    sys_msgs = [m for m in messages if m.get("role") == "system"]
    base_sys, prior_summary = [], None
    for m in sys_msgs:
        if prior_summary is None and str(m.get("content", "")).startswith(_COMPACT_FRAME):
            prior_summary = m           # the one canonical, append-only compaction block
        else:
            base_sys.append(m)
    rest = [m for m in messages if m.get("role") != "system"]
    goal_msg = None
    if pin_goal is not None:
        for i, m in enumerate(rest):
            if m is pin_goal:           # identity match — the goal never falls out of the prefix
                goal_msg = rest.pop(i)
                break

    # Frozen head: systems (incl. the summary block) grouped first, then the pinned goal.
    head = list(base_sys) + ([prior_summary] if prior_summary is not None else [])
    if goal_msg is not None:
        head.append(goal_msg)

    original_tail = list(rest)
    tail = original_tail

    # 1. Message-count window on the tail (head is always kept).
    if len(head) + len(tail) > max_msgs:
        tail = tail[-max(0, max_msgs - len(head)):]

    # 2. Token-budget window on the tail; reserve room for a (possibly new) summary block.
    budget = None
    if max_tokens:
        budget = _tail_budget(max_tokens, sum(_message_tokens(m) for m in head),
                              summary_max_tokens if (summarize and prior_summary is None) else 0)
        kept, running = [], 0
        for m in reversed(tail):
            t = _message_tokens(m)
            if running + t > budget:
                if kept:
                    break
                kept.append(_clamp_message(m, budget))   # newest alone overflows -> clamp, don't send whole
                break
            running += t
            kept.append(m)
        tail = list(reversed(kept))

    if summarize:
        tail = _summarize_keep(original_tail, tail, keep_last, budget)
        dropped = original_tail[: len(original_tail) - len(tail)]
        while tail and tail[0].get("role") == "tool":
            dropped.append(tail.pop(0))
        if dropped:
            note = (_compact_span(dropped, summary_model, summary_max_tokens) if config is None
                    else _compact_span(dropped, summary_model, summary_max_tokens, config=config))
            if note:
                # APPEND to the frozen block (prior bytes unchanged) — or create it after base system.
                content = (prior_summary["content"] + "\n" + note if prior_summary is not None
                           else f"{_COMPACT_FRAME}\n{note}")
                summary_msg = {"role": "system", "content": content}
                out = list(base_sys) + [summary_msg]
                if goal_msg is not None:
                    out.append(goal_msg)
                return out + tail
        # empty note / nothing dropped -> head (goal + any prior summary still pinned) + tail.
        return head + tail

    # Truncate mode with a stable prefix: goal + system pinned, tail slid, no summary.
    while tail and tail[0].get("role") == "tool":
        tail.pop(0)
    return head + tail


def _clear_hermes_responses(content: str, registry) -> tuple:
    """Rewrite each <tool_response>{...}</tool_response> in a hermes tool-result message,
    replacing any whose inner content is a clearable (retained) result with a compact stub. Returns
    (new_content, changed). A segment that doesn't parse as JSON is left untouched (safe no-op)."""
    changed = False

    def _repl(match):
        nonlocal changed
        try:
            obj = json.loads(match.group(1))
        except Exception:
            return match.group(0)
        c = obj.get("content")
        if isinstance(c, str):
            stub = registry.clear_stub(c)
            if stub:
                obj["content"] = stub
                changed = True
                return f"<tool_response>{json.dumps(obj)}</tool_response>"
        return match.group(0)

    return re.sub(r"<tool_response>(.*?)</tool_response>", _repl, content, flags=re.DOTALL), changed


def _clear_old_tool_results(messages: list, registry, keep_last: int, hermes: bool) -> list:
    """Context editing: replace OLD bulky tool-result messages with compact stubs that stay
    re-fetchable via read_result, shrinking the biggest context hogs while keeping the last `keep_last`
    messages verbatim. Returns a new list; a message is cleared only when its result carried a retained
    handle still resolvable (else left as-is, so nothing becomes unrecoverable). Idempotent — an
    already-cleared stub has no 'retained as rN]' handle for clear_stub to match."""
    if not messages or not hasattr(registry, "clear_stub"):
        return messages
    protect_from = max(0, len(messages) - keep_last)
    out = list(messages)
    for i in range(protect_from):
        m = out[i]
        content = m.get("content")
        if not isinstance(content, str):
            continue
        if hermes:
            if m.get("role") != "user" or "<tool_response>" not in content:
                continue
            new_content, changed = _clear_hermes_responses(content, registry)
            if changed:
                out[i] = {**m, "content": new_content}
        elif m.get("role") == "tool":
            stub = registry.clear_stub(content)
            if stub:
                out[i] = {**m, "content": stub}
    return out


def truncate_history(messages: list, max_msgs: int, max_tokens: int = 0, *,
                     compaction: str = "truncate", keep_last: int = 6,
                     summary_max_tokens: int = 512, summary_model: str = "chat",
                     stable_prefix: bool = False, pin_goal: dict = None, config: dict = None) -> list:
    """Sliding window that keeps the system message(s), the pinned goal, and the most recent turns.

    Trims by message count first (max_msgs), then by an optional token budget (max_tokens): drop oldest
    non-system messages until the estimated total fits. The system message is always kept, and so is
    `pin_goal` (matched by identity): the turn's goal never falls out of the window, whatever the tool
    turns after it cost, so the model always knows what it was asked. An orphaned leading tool-response
    (whose assistant call got trimmed) is dropped so the remaining sequence stays valid for the OpenAI
    tool format.

    `compaction='summarize'` (opt-in) replaces the dropped oldest span with ONE compact
    "conversation so far" system note (via _compact_span) instead of discarding it, keeps up to the last
    `keep_last` turns verbatim as far as they fit the budget, and reserves `summary_max_tokens` from the
    budget so the note itself can't re-overflow. `compaction='truncate'` (**default**) is the lossy
    drop-oldest window. This owns the rolling TRANSCRIPT; the budget_injection helper bounds SAVED-memory
    injection, kept distinct, no double-summarizing. `config` is handed to the summarizer (loaded when
    omitted).

    `stable_prefix=True` (opt-in, default off) routes to the prefix-cache-aware layout: a frozen
    head (system + append-only summary block + `pin_goal`) so llama.cpp reuses the KV prefix across
    turns."""
    if stable_prefix:
        return _truncate_stable_prefix(
            messages, max_msgs, max_tokens, keep_last=keep_last,
            summary_max_tokens=summary_max_tokens, summary_model=summary_model,
            pin_goal=pin_goal, summarize=(compaction == "summarize"), config=config)
    system = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    goal, n_after = None, 0
    if pin_goal is not None:
        for i, m in enumerate(rest):
            if m is pin_goal:
                goal = rest.pop(i)
                n_after = len(rest) - i          # messages that came after the goal
                break
    original_rest = list(rest)
    summarize = compaction == "summarize"
    pinned = [goal] if goal is not None else []

    # 1. Message-count window.
    if len(system) + len(pinned) + len(rest) > max_msgs:
        keep = max(0, max_msgs - len(system) - len(pinned))
        rest = rest[-keep:] if keep else []

    # 2. Token-budget window: keep as many recent messages as fit under the budget. In summarize mode,
    # reserve room for the compaction note so summary + kept stays within max_tokens.
    budget = None
    if max_tokens:
        head_tokens = sum(_message_tokens(m) for m in system)
        if goal is not None:
            room = max_tokens - head_tokens - (summary_max_tokens if summarize else 0)
            if _message_tokens(goal) > room // 2:        # a goal bigger than half the room is clamped
                goal = _clamp_message(goal, max(1, room // 2))
                pinned = [goal]
            head_tokens += _message_tokens(goal)
        budget = _tail_budget(max_tokens, head_tokens, summary_max_tokens if summarize else 0)
        kept: list = []
        running = 0
        for m in reversed(rest):
            t = _message_tokens(m)
            if running + t > budget:
                if kept:
                    break
                kept.append(_clamp_message(m, budget))   # newest alone overflows -> clamp, don't send whole
                break
            running += t
            kept.append(m)
        rest = list(reversed(kept))

    def _assemble(tail: list) -> list:
        # `tail` is a suffix of before-goal + after-goal messages: put the goal back where it sat.
        if goal is None:
            return tail
        split = len(tail) - min(n_after, len(tail))
        return tail[:split] + [goal] + tail[split:]

    if summarize:
        rest = _summarize_keep(original_rest, rest, keep_last, budget)
        dropped = original_rest[: len(original_rest) - len(rest)]
        # Drop an orphaned leading tool response from the KEPT tail before summarizing/returning.
        while rest and rest[0].get("role") == "tool":
            dropped.append(rest.pop(0))
        if dropped:
            note = (_compact_span(dropped, summary_model, summary_max_tokens) if config is None
                    else _compact_span(dropped, summary_model, summary_max_tokens, config=config))
            if note:
                summary_msg = {"role": "system", "content": f"{_COMPACT_FRAME}\n{note}"}
                return system + [summary_msg] + _assemble(rest)
        # empty note (no LLM / failure) -> fall through to plain truncation semantics.
        return system + _assemble(rest)

    # 3. Don't leave an orphaned tool response at the front (truncate mode).
    while rest and rest[0].get("role") == "tool":
        rest.pop(0)

    return system + _assemble(rest)


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


# --- structured logging, graceful interrupt --------------------------
#
# Cold-start: the old module-level _REGISTRY_CACHE was removed — it was dead. Every real
# caller either passes a prebuilt registry (the server, built once at startup) or runs in a fresh
# process (CLI/voice), so an in-process singleton never amortized anything. Measured cold-start:
# interpreter ~31ms + import chain ~32ms + registry build ~140ms (cold) vs ~16ms (warm). The
# 140ms registry build is the dominant amortizable cost, and the path that actually amortizes it
# already exists: `bob agent serve` builds once and reuses it across turns. Voice / high-frequency
# clients should route through the server rather than paying a fresh cold build per invocation.


def _agent_logger(config: dict):
    """A 'bob.agent' logger writing structured lines to logs/bob-agent.log (per-run id lives in
    each message). Rotates at agent.logMaxBytes (keeping agent.logBackupCount old files) so the
    log can't grow unbounded across many runs. Human-facing stderr previews stay separate
    for interactive use."""
    log = logging.getLogger("bob.agent")
    if not log.handlers:
        log.setLevel(logging.INFO)
        agent = config.get("agent", {})
        from bob_core import state_path
        path = state_path(agent.get("logFile") or "logs/bob-agent.log")
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
    """Cooperative cancel shared by the loop, its LLM stream, and tool dispatch. Thread-safe
    (wraps threading.Event) so a server request thread or a SIGINT handler can trip it while the
    loop reads it. A long in-flight tool can't be preempted, but the loop stops before the next
    tool and the LLM stream stops within one chunk (~1s).

    A token may be LINKED to a parent: cancelling the parent also cancels the child
    (checked lazily in cancelled()), so a sub-agent run started via child() is torn down when the
    parent run is cancelled. child() returns a fresh linked token to hand to the sub-run."""

    def __init__(self, parent: "CancelToken" = None):
        self._e = threading.Event()
        self._parent = parent
        self._inbox = []                  # queued mid-run steer messages (see steer/drain_steer)
        self._lock = threading.Lock()

    def cancel(self) -> None:
        self._e.set()

    def cancelled(self) -> bool:
        return self._e.is_set() or (self._parent is not None and self._parent.cancelled())

    def steer(self, message: str) -> None:
        """Queue a user message to be injected at the next step boundary WITHOUT cancelling the run.
        A driver holding this token (shell/server) can nudge a running loop. Thread-safe."""
        if message and message.strip():
            with self._lock:
                self._inbox.append(message)

    def drain_steer(self) -> list:
        """Return and clear all queued steer messages (called at the step boundary). Thread-safe."""
        with self._lock:
            msgs, self._inbox = self._inbox, []
        return msgs

    def child(self) -> "CancelToken":
        """A new token linked to this one — cancelling this (parent) cancels the child too. The steer
        inbox is NOT inherited: steering targets the root conversation, not sub-agent runs."""
        return CancelToken(parent=self)


def _close_stream(stream_resp) -> None:
    """Best-effort close of a streaming response to abort the underlying HTTP request."""
    try:
        stream_resp.close()
    except Exception:
        pass


_NO_PREV_HANDLER = object()  # sentinel: we did NOT install (off main thread / signal error)


def _install_interrupt_handler(cancel: "CancelToken"):
    """Install a SIGINT handler that trips the shared cancel token instead of raising, so the loop
    aborts the in-flight step (stream stops within a chunk) and exits cleanly rather than dying
    mid-write. No-op off the main thread (a server request), where signal.signal is
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


# --- unified completion call with optional token streaming --------------

_TOOL_OPEN = "<tool_call>"


def _prefix_overlap(s: str, marker: str) -> int:
    """Largest k (0..len(marker)-1) such that s ends with marker[:k] — i.e. s's tail could be the
    start of `marker`. Lets the hermes streamer hold back only the minimal tail that might begin a
    <tool_call> even when the marker is split across chunks."""
    k = min(len(s), len(marker) - 1)
    while k > 0:
        if s[-k:] == marker[:k]:
            return k
        k -= 1
    return 0


def _consume_stream(stream_resp, cancel=None, emit_tokens=True, hermes=True):
    """Iterate a streaming chat completion (the loop always streams internally now — non-stream
    agent mode passes emit_tokens=False and drops the tokens). Polls `cancel` between chunks and
    closes the stream promptly when tripped, so an in-flight call aborts within ~1s. Yields
    ('token', text) content deltas (only when emit_tokens) and returns
    SimpleNamespace(content, tool_calls, cancelled, finish_reason, usage, reasoning_chars): usage is the
    final chunk's token usage when the backend reports it (stream_options.include_usage), else None;
    reasoning_chars counts the separate reasoning channel, so an answer-less reply can say why.

    A reply cut off at the output limit (finish_reason 'length') carries no tool calls: a truncated
    <tool_call> (or structured call) is neither parsed as a call nor flushed as answer text.

    Tool-call boundary handling:
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
    finish_reason = None
    usage = None
    reasoning_chars = 0

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
        usage = getattr(chunk, "usage", None) or usage
        if not chunk.choices:
            continue
        finish_reason = getattr(chunk.choices[0], "finish_reason", None) or finish_reason
        delta = chunk.choices[0].delta
        reasoning = getattr(delta, "reasoning_content", None)
        if isinstance(reasoning, str):
            reasoning_chars += len(reasoning)
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
        return SimpleNamespace(content=content, tool_calls=None, cancelled=True, finish_reason=finish_reason,
                               usage=usage, reasoning_chars=reasoning_chars)
    if finish_reason == "length":
        # Cut off at the output limit: whatever call was being written is incomplete, so nothing runs
        # and a withheld partial <tool_call> is never flushed as answer text.
        return SimpleNamespace(content=content, tool_calls=None, cancelled=False, finish_reason=finish_reason,
                               usage=usage, reasoning_chars=reasoning_chars)

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

    return SimpleNamespace(content=content, tool_calls=tool_calls, cancelled=False, finish_reason=finish_reason,
                           usage=usage, reasoning_chars=reasoning_chars)


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
    trace_parent=None,
    no_tools: bool = False,
    max_tokens: int = None,
    images: list = None,
    system_prompt: str = None,
    resume: str = None,
    think: bool = None,
    session_id: str = None,
    allowed_roles=None,
    unattended_allow=None,
):
    """Generator core of the agent loop. Yields event dicts:
        {"type": "token",             "text": str}                          # final-answer deltas (stream=True)
        {"type": "tool_call",         "call_id": str, "name": str, "arguments": str}
        {"type": "approval_required", "call_id": str, "tool": str, "arguments": str, "risk": str}
        {"type": "tool_result",       "call_id": str, "name": str, "result": str}
        {"type": "notice",            "message": str}                       # e.g. a role fallback
        {"type": "final",             "result": str|None, "exit_requested": bool, "reason": str,
                                      "usage": {...}}
        {"type": "error",             "message": str, "kind": str, "usage": {...}}
    `reason` is answer | truncated | forced_answer | cancelled | max_steps. An error's `kind` tells an
    upstream failure (upstream_unreachable, upstream_error, empty_response) from a run-level one
    (truncated, vision_unavailable, not_found, conflict). `usage` sums the backend-reported prompt +
    completion tokens over the run's LLM calls (estimated when the backend reports none).
    A terminal 'final' or 'error' is always the last event. run_agent() is the blocking wrapper
    used by the CLI; the server's SSE endpoint consumes these events directly. Pass a CancelToken
    to abort in-flight — SIGINT (CLI) and client-disconnect (server) both trip it; the run
    stops within ~1s with a final event reason='cancelled'.

    Approval is event-driven, not a blocking stdin prompt: before a tool that needs approval
    (agency=='confirm', or a tool with REQUIRES_APPROVAL like shell_run) the loop emits
    'approval_required' and consults `approve(action) -> bool`. `approve=None` fails closed (deny), so
    the server/scheduler never run a gated tool unattended; the CLI wrapper installs a console approver
    on a TTY, and the interactive shell will pass one that drives the TUI. `call_id` correlates
    tool_call↔approval_required↔tool_result (forward-compat for parallel tools)."""
    from bob_core import (MEMORY_CONTEXT_FRAME, _mem, _port, budget_injection, check_litellm,
                          core_blocks_block, get_llm_client, get_role, image_refusal, memory_profile_block,
                          memory_recall, project_memory_block, role_output_tokens, served_role)

    agent_cfg = config.get("agent", {})
    effective_role = role or config.get("routing", {}).get("agentRole", "chat")
    # An image-bearing turn routes to the vision role unless the caller pinned a role.
    if images and role is None:
        effective_role = get_role(config, "vision")
    # A role the active profile doesn't serve (the cpu profile has no coder/ponder) falls back to chat,
    # announced as a notice event rather than failing the request at the backend.
    notices: list = []
    effective_role = served_role(config, effective_role, notice=notices.append)
    if images:
        refusal = image_refusal(config, effective_role)
        if refusal:
            yield {"type": "error", "message": refusal, "kind": "vision_unavailable"}
            return
    for _n in notices:
        yield {"type": "notice", "message": _n}
    # Reasoning ("think") is a MODE on whichever model is active, not a model swap: forward the
    # `enable_thinking` chat-template kwarg to llama-server via extra_body. `think=None` -> the config
    # default (agent.think). llama-server is pinned to --reasoning-format deepseek, so any reasoning
    # lands in a separate `reasoning_content` field the stream reader drops, so it never reaches the
    # transcript or memory. Scoped to locally served models; cloud peers don't take the kwarg.
    enable_thinking = agent_cfg.get("think", False) if think is None else bool(think)

    def _reasoning_body(for_role):
        return ({"chat_template_kwargs": {"enable_thinking": enable_thinking}}
                if _is_local_model(for_role, config) else None)

    reasoning_extra_body = _reasoning_body(effective_role)
    effective_agency = agency or agent_cfg.get("agency", "show")
    max_steps = int(agent_cfg.get("maxSteps", 10))
    # Loop-pathology guard: a (small) model can fixate on one tool and call it with identical args
    # forever (e.g. memory_recall over and over) without ever answering. Allow a signature to run this
    # many times, then BLOCK further identical calls; 0 disables the guard.
    dup_limit = int(agent_cfg.get("maxDuplicateToolCalls", 2))
    max_hist = int(agent_cfg.get("maxHistoryMsgs", 40))
    # Context compaction. Default 'truncate' == drop-oldest (identical to running with summarize
    # compaction off); 'summarize' replaces the dropped span with one compact note via summarize_turns
    # (opt-in — it calls the LLM).
    compaction_mode = agent_cfg.get("compaction", "truncate")
    compact_keep_last = int(agent_cfg.get("compactKeepLastTurns", 6))
    compact_summary_max = int(agent_cfg.get("compactSummaryMaxTokens", 512))
    # Prefix-cache-aware context. Default False == the standard assembly (byte-identical to it). When on,
    # truncate_history freezes the head (system + append-only summary + pinned goal) so llama.cpp's
    # KV prefix cache is reused across turns; co-designed with the summarize compaction path.
    stable_prefix = bool(agent_cfg.get("stablePrefix", False))
    # Context editing: once the transcript passes clearToolResultsAfterTokens, replace OLD bulky
    # tool-result messages with compact stubs re-fetchable via read_result. Default off == disabled.
    clear_tool_results = bool(agent_cfg.get("clearToolResults", False))
    clear_after_tokens = int(agent_cfg.get("clearToolResultsAfterTokens", 4000))
    # Grammar-constrained tool calls: attach the structured `tools` payload + tool_choice='auto'
    # so a grammar-capable backend (llama.cpp) can only emit a well-formed tool call, killing the
    # malformed-JSON (__parse_error__) class, while still allowing free-text answers. Default off ==
    # the unconstrained request bytes. Capability-gated at runtime: an endpoint that rejects it is
    # detected once and the run falls back to today's hermes-text parse.
    constrained_tool_calls = bool(agent_cfg.get("constrainedToolCalls", False))
    # Optional plan / verify loop phases (self-repair is read per-call in _dispatch_with_approval).
    # All default false → the loop is byte-identical to the plain (no plan/verify) path. `bob agent --deep` flips them on.
    plan_enabled = bool(agent_cfg.get("plan", False))
    verify_enabled = bool(agent_cfg.get("verify", False))
    # Objective lint/test-fix gate: after a would-be final answer, run the configured check command(s),
    # and on failure feed the parsed failures back so the model fixes and re-runs. Default off, and inert
    # unless a testCmd/lintCmd is configured; distinct from the goal-satisfaction verify critic above.
    autofix_enabled = bool(agent_cfg.get("autoFix", False)) and bool(
        (agent_cfg.get("testCmd") or "").strip() or (agent_cfg.get("lintCmd") or "").strip())
    autofix_max_rounds = int(agent_cfg.get("autoFixRounds", 3))
    checkpoint_edits = bool(agent_cfg.get("checkpointEdits", False))
    # Durable run state: persist the message list + step + identity each step so a killed run can resume
    # (distinct from checkpointEdits, which snapshots file bytes for rewind). Default off == no run rows.
    checkpoint_run = bool(agent_cfg.get("checkpoint", False))
    # Goal recitation: re-emit the goal + open TODO at the context TAIL each step (default off ==
    # no recitation; the block is appended only to the per-step request, never stored in `messages`).
    recite_enabled = bool(agent_cfg.get("recite", False))
    # Transient-LLM-error retry (5xx/timeout/conn, incl. the llama-swap model-swap race). Total tries
    # per step = llmRetries + 1, with an escalating backoff so a restarting backend has time to come up.
    llm_attempts = max(1, int(agent_cfg.get("llmRetries", 2)) + 1)
    llm_backoff = float(agent_cfg.get("llmRetryBackoffSec", 2.0))
    # Token-aware context: the history budget is the serving model's per-request window (0/'auto') or
    # an explicit agent.maxContextTokens capped at that window; the injected tool schemas shrink once
    # the tool count crosses compactSchemasAfter.
    max_context_cfg = agent_cfg.get("maxContextTokens", 0)
    # llama.cpp charges prompt + n_predict against the same ctx, so the generation the step is about
    # to ask for comes out of the history budget too, and is sent as max_tokens so the reservation
    # holds (_budget_for). --max wins when the caller set it.
    compact_after = int(agent_cfg.get("compactSchemasAfter", 12))
    # Max concurrent side-effect-free tools per step. Default 1 = sequential.
    max_parallel_tools = int(agent_cfg.get("maxParallelTools", 1))
    # Client-side timeout must be >= the proxy's request_timeout (600s): thinking models
    # (ponder/R1) can run >2 min before first output. A low value would cut them off.
    request_timeout = int(agent_cfg.get("requestTimeout", 600))

    rid = run_id or uuid.uuid4().hex[:8]   # server passes its request id so one id spans client→server→loop
    log = _agent_logger(config)
    t_start = time.monotonic()
    reg_build_ms = 0.0

    # Resume path: rehydrate a checkpointed run's message list (tool results already embedded), step
    # index, and identity, then continue from the next step -- completed tools are NOT re-run. A run
    # lease guards against two processes resuming the same run at once (double-execution). Resume forces
    # checkpointing on so the continued run keeps persisting.
    resumed = None
    resume_store = None
    lease_token = None
    step_start = 0
    if resume:
        try:
            import bob_checkpoint
            resume_store = bob_checkpoint.CheckpointStore(
                db_path=agent_cfg.get("checkpointDbPath") or None, default_owner=owner or "local")
            resumed = resume_store.load_run(resume, owner)
        except Exception as _e:
            log.warning(f"[{rid}] resume load failed: {_e}")
        if resumed is None:
            yield {"type": "error", "message": f"cannot resume run {resume!r}: not found for this owner",
                   "kind": "not_found"}
            return
        lease_token = uuid.uuid4().hex
        if not resume_store.acquire_lease(resume, owner, holder=lease_token):
            yield {"type": "error",
                   "message": f"run {resume!r} is already active (held by another process)",
                   "kind": "conflict"}
            return
        rid = resume
        owner = resumed["owner"]
        scope = resumed["scope"]
        agent_depth = resumed["agent_depth"]
        goal = resumed["goal"]
        step_start = resumed["step"]
        checkpoint_run = True
        log.info(f"[{rid}] resuming from step {step_start}")

    # Build the registry if the caller didn't supply one (server passes its prebuilt, warm
    # registry). Timed for the metrics line / cold-start visibility.
    if registry is None:
        from tool_registry import ToolRegistry
        if no_tools:
            # Chat mode: skip the tool load entirely (an empty registry) so `bob chat` starts
            # fast and prints no tool summary; the run is a plain chat completion.
            registry = ToolRegistry()
        else:
            _t0 = time.monotonic()
            registry = ToolRegistry.from_config(config)
            reg_build_ms = (time.monotonic() - _t0) * 1000
    elif no_tools and hasattr(registry, "filtered"):
        # Chat mode over a caller-supplied registry (e.g. the shell): an EMPTY tool view via the
        # filtered() seam, no rebuild. Default no_tools=False -> registry unchanged.
        registry = registry.filtered(allow=[])
    # Root-cause the memory_recall spin: when autoRecall is on at ROOT depth, the recalled memory is
    # injected into the system prompt below, so ALSO offering the memory_recall TOOL is redundant that
    # turn -- small models double-dip and re-call it forever. Drop it from the offered toolset (the
    # model can't spin on a tool it was never given). The maxDuplicateToolCalls guard stays as
    # defense-in-depth. Sub-agents (depth>0) have no autoRecall, so they keep the tool.
    if config.get("memory", {}).get("autoRecall") and agent_depth == 0 and hasattr(registry, "filtered"):
        registry = registry.filtered(deny={"memory_recall"})
    tool_schemas = registry.tool_schemas
    exit_on_tools = exit_on_tools if exit_on_tools is not None else registry.exit_voice_tools
    exit_on_tools = exit_on_tools or set()

    # Pre-flight check
    if not check_litellm(config):
        port = _port(config, "litellmPort")
        msg = f"LiteLLM proxy not reachable at localhost:{port}. Run: bob up"
        log.error(f"[{rid}] preflight failed: {msg}")
        yield {"type": "error", "message": msg, "kind": "upstream_unreachable"}
        return

    # A caller (e.g. a typed sub-agent) may override the persona; default None reads the configured
    # persona so the plain path is byte-identical.
    system_prompt = system_prompt or config.get("persona", {}).get(
        "systemPrompt", "You are Bob, a helpful AI assistant."
    )

    # OPTIONAL per-turn memory injection, gated on memory.autoRecall (NOT memory.enabled).
    # `enabled` only makes the memory_recall/store TOOLS available so the model recalls *sporadically*
    # when a request needs it; autoRecall (default off) is the heavier "read memory every turn" mode.
    # Keeping them separate stops the model reciting the same notes on every prompt. Best-effort: a
    # memory/embed failure is logged and skipped, never fatal to the run.
    mem_cfg = config.get("memory", {})
    # Resolve the identity (owner) + project scope this run acts as BEFORE any memory read.
    # Owner defaults to agent.defaultOwner (server passes the authed owner, the shell its self.owner,
    # a sub-agent the parent's); scope is the project key threaded from the shell/CLI (None = global).
    owner = owner or config.get("agent", {}).get("defaultOwner", "local")

    # Gather every injected-memory block, then fit them into memory.maxInjectedTokens before
    # concatenating into the one system message truncate_history always keeps (so injected memory can't
    # overflow the context window). Priority (kept longest): coreBlocks > BOB.md > profile > autoRecall.
    inject_blocks: list = []   # (label, text, priority)
    inject_budget = int(_mem(mem_cfg, "maxInjectedTokens"))

    # autoRecall is a ROOT-run behavior only: a sub-agent runs an isolated
    # transcript by design and must not pull the owner's saved notes every turn (mirrors the
    # profile/BOB.md gate below). Sporadic recall via the memory_recall TOOL is still available to it.
    if mem_cfg.get("autoRecall") and agent_depth == 0:
        try:
            # Recall as the RUN's owner/scope, not the 'local' default.
            recalled = memory_recall(goal, k=int(_mem(mem_cfg, "recallK")), config=config,
                                     owner=owner, scope=scope)
            if recalled and recalled.strip() and recalled != "(no results)":
                recalled = recalled[: tokens_to_chars(inject_budget)]   # hard-cap autoRecall length
                inject_blocks.append(("autoRecall", MEMORY_CONTEXT_FRAME + "\n" + recalled, 1))
        except Exception as e:
            log.warning(f"[{rid}] memory recall skipped: {e}")
            print(f"[warn] memory recall skipped: {e}", file=sys.stderr)

    # Once-per-session profile injection. History-empty == a genuine session start (a resumed
    # session repopulates history, so this fires once at the real start, not every process launch).
    # agent_depth==0 restricts it to ROOT runs — a sub-agent starts with empty isolated history by
    # design and must NOT inherit the profile block. Gated on memory.injectProfileAtStart (distinct
    # from autoRecall).
    if not history and agent_depth == 0:
        try:
            profile = memory_profile_block(owner=owner, config=config)
            if profile:
                inject_blocks.append(("profile", profile, 2))
        except Exception as e:
            log.warning(f"[{rid}] profile injection skipped: {e}")

        # Per-project instruction file(s) (BOB.md), once at session start on a root run.
        # `scope` is the project dir (git root/cwd); None → not in a project → skip.
        if scope:
            try:
                pm = project_memory_block(scope, config=config)
                if pm:
                    inject_blocks.append(("bobmd", pm, 3))
            except Exception as e:
                log.warning(f"[{rid}] project memory skipped: {e}")

    # Agent-editable core-memory blocks: unlike profile/BOB.md these inject on EVERY root turn (not just
    # session start) since the agent rewrites them mid-run and must keep seeing the current text. Top
    # priority so they survive budget trimming ahead of profile/BOB.md. Off (empty coreBlocks) → no-op.
    if agent_depth == 0:
        try:
            cb = core_blocks_block(owner=owner, scope=scope, config=config)
            if cb:
                inject_blocks.append(("coreBlocks", cb, 4))
        except Exception as e:
            log.warning(f"[{rid}] core-memory blocks skipped: {e}")

    if inject_blocks:
        joined, kept, dropped = budget_injection(inject_blocks, inject_budget)
        if joined:
            system_prompt += "\n\n" + joined
            log.info(f"[{rid}] injected memory {kept} ({len(joined)}c)")
        if dropped:
            log.info(f"[{rid}] memory injection over budget; dropped {dropped}")

    tool_fmt = agent_cfg.get("toolFormat", "hermes").lower()
    hermes_mode = tool_fmt == "hermes"
    # Fit the tool set to a small window before it is baked into the prompt: compact every schema, then
    # leave out the least essential tools, so the head never crowds out the reply (see _fit_tools_to_window).
    output_request = max_tokens or role_output_tokens(config, effective_role)
    if tool_schemas and resumed is None:
        fit_window = _context_budget(config, effective_role, max_context_cfg, output_request, 0)[0]
        tool_schemas, compact_after, dropped_tools = _fit_tools_to_window(
            system_prompt, tool_schemas, window=fit_window, output=output_request, hermes=hermes_mode,
            compact_after=compact_after, constrained=constrained_tool_calls)
        if dropped_tools:
            if hasattr(registry, "filtered"):
                registry = registry.filtered(allow={_tool_name(t) for t in tool_schemas})
            yield {"type": "notice", "message": (
                f"The {effective_role} model's {fit_window}-token window can't hold all the tools; left out "
                f"{len(dropped_tools)} for this run: {', '.join(dropped_tools)}. List tools you don't need in "
                "agent.disabledTools (config/user.json) to choose the set yourself.")}
            log.info(f"[{rid}] window {fit_window}: dropped {len(dropped_tools)} tools {dropped_tools}")
    base_system = (
        system_prompt + _hermes_tool_system_addendum(tool_schemas, compact_after)
        if hermes_mode and tool_schemas
        else system_prompt
    )
    # Prior session turns are seeded between the system prompt and the new goal;
    # truncate_history keeps the whole thing within the token budget.
    # On resume the persisted transcript (its own system prompt + all turns, tool results embedded) is
    # used verbatim so completed steps are not rebuilt or re-run; `history`/images are ignored.
    if resumed is not None:
        # The persisted transcript already ends with the pending work; pin its last user turn (if any)
        # so truncate_history's stable-prefix logic has a goal reference to hold.
        messages = resumed["messages"]
        goal_msg = next((m for m in reversed(messages) if m.get("role") == "user"),
                        {"role": "user", "content": goal})
    else:
        messages = [{"role": "system", "content": base_system}]
        if history:
            messages.extend(history)
        # Keep a reference to the goal message so truncate_history can pin it (by identity) into the
        # stable prefix when stable_prefix is on; a plain user turn otherwise.
        # With images attached, the user turn is an OpenAI content-block list ([text, image_url…]);
        # with none it stays a plain string (byte-identical to the text-only path). `goal` itself stays a
        # str, so recall / recitation / logging are untouched.
        goal_content = goal
        if images:
            goal_content = [{"type": "text", "text": goal}] + [_image_content_block(s) for s in images]
        goal_msg = {"role": "user", "content": goal_content}
        messages.append(goal_msg)

    # Conversation-paging capture (opt-in): persist every turn — user, assistant, AND intermediate tool
    # turns — to the owner-scoped transcript store AS IT HAPPENS, before compaction can drop it, so
    # conversation_search can page it back. Runs on the stateless CLI path too (rid always set; owner
    # defaults to 'local'), closing the "CLI persists nothing" gap. Best-effort + deduped: never breaks a run.
    paging_on = bool(agent_cfg.get("conversationPaging", False))
    cap_owner = owner or "local"
    _capped: set = set()

    def _cap(role, content, tool_name=None):
        if not paging_on or content is None or not str(content).strip():
            return
        key = (role, tool_name, str(content))
        if key in _capped:
            return
        _capped.add(key)
        try:
            from bob_core import _get_db_path
            import bob_memory  # type: ignore
            bob_memory.transcript_append(rid, role, str(content), _get_db_path(config),
                                         owner=cap_owner, scope=scope, tool_name=tool_name,
                                         session_id=session_id)
        except Exception as e:
            log.warning(f"[{rid}] transcript capture skipped: {e}")

    _cap("user", goal)

    client = get_llm_client(config)
    exit_requested = resumed["exit_requested"] if resumed is not None else False
    last_content = None
    steps_done = 0     # metrics
    tools_run = 0
    tokens_est = 0

    log.info(
        f"[{rid}] start role={effective_role} agency={effective_agency} "
        f"tools={len(tool_schemas)} stream={stream} imgs={len(images or [])} goal={goal[:200]!r}"
    )

    # OpenAI-mode tool payload, computed once (constant across steps): compacted past compact_after
    # so the per-turn schema tokens stay bounded (hermes mode injects schemas via base_system instead).
    openai_tools = (
        _openai_tools_payload(tool_schemas, compact_after)
        if (tool_schemas and not hermes_mode) else None
    )

    # The structured tool payload the constraint rides on. In openai mode it's already `openai_tools`;
    # in hermes mode (where malformed <tool_call> JSON actually occurs) it's built on demand. `constrain_active`
    # is a run-local latch flipped off if the backend rejects the constraint (see _is_unsupported_constraint).
    constrain_tools_payload = None
    if constrained_tool_calls and tool_schemas:
        constrain_tools_payload = openai_tools or _openai_tools_payload(tool_schemas, compact_after)
    constrain_active = constrain_tools_payload is not None
    # Schemas that ride on the REQUEST rather than inside a message: openai mode's `tools=`, or the
    # grammar-constraint payload on a hermes call. They share the backend's context window with the
    # history, so they come out of the budget before the window trims. Constant across steps.
    request_tools_tokens = _tools_payload_tokens(openai_tools or constrain_tools_payload)

    def _budget_for(for_role):
        """The budget of a request on `for_role`: out (max_tokens), send (history budget), fits, total
        (window), head (system tokens) and shrunk. The output (--max, else the role's default: a pro peer's
        maxOutputTokens, else agent.outputReserveTokens) shrinks to what the window leaves after the system
        head, the schemas and the minimal history, so prompt + max_tokens never exceeds the window; `shrunk`
        says it did, and `fits` is False when not even a _MIN_OUTPUT_TOKENS reply is left."""
        total, out, send = _context_budget(config, for_role, max_context_cfg,
                                           max_tokens or role_output_tokens(config, for_role),
                                           request_tools_tokens)
        head = sum(_message_tokens(m) for m in messages if m.get("role") == "system")
        room = total - request_tools_tokens - head - _tail_reserve(total)
        fits = room >= min(out, _MIN_OUTPUT_TOKENS)
        shrunk = out > room
        if shrunk:
            out = max(1, room)
            send = max(0, total - request_tools_tokens - out)
        log.info(f"[{rid}] context budget {send} (role={for_role} window={total} head={head} "
                 f"schemas={request_tools_tokens} output={out})")
        return SimpleNamespace(out=out, send=send, fits=fits, total=total, head=head, shrunk=shrunk)

    budget = _budget_for(effective_role)
    output_tokens, send_budget = budget.out, budget.send

    def _raise_output_hint():
        """How to get a longer reply. When the window, not the setting, capped the output, raising
        outputReserveTokens would change nothing: the fixed head is what has to shrink."""
        if budget.shrunk:
            return (f"free room in the {budget.total}-token window, which the system prompt and tools "
                    f"fill ({budget.head + request_tools_tokens} tokens): list tools you don't need in "
                    "agent.disabledTools (config/user.json) or use a profile with a larger window")
        return "raise agent.outputReserveTokens (or pass --max)"
    if not budget.fits:
        yield {"type": "error", "kind": "context_overflow", "message": (
            f"The {effective_role} model's context window ({budget.total} tokens) is too small for the system "
            f"prompt and tool schemas ({budget.head + request_tools_tokens} tokens) plus a reply. List tools "
            "you don't need in agent.disabledTools (config/user.json), shorten the persona or injected "
            "memory, or use a profile with a larger window.")}
        return

    cancel = cancel or CancelToken()
    # Resolve the allow|ask|deny policy once per run from config (empty config -> everything
    # allow, i.e. no policy configured). Carried on the RunContext and consulted in _dispatch_with_approval.
    from bob_permissions import PermissionPolicy
    policy = PermissionPolicy(config)
    # Build the run's tracer (disabled == no-op == behaviorally inert) and open the root run span,
    # parented to trace_parent when a sub-agent passes its caller's span (cross-run nesting).
    tracer = make_tracer(config)
    run_span = tracer.start("agent.run",
                            {"run_id": rid, "owner": owner or "local", "role": effective_role,
                             "agent_depth": agent_depth, "goal_chars": len(goal or "")},
                            parent=trace_parent)
    # Run-scoped context handed to each tool call (reachable via tool_registry.get_run_context()),
    # and the approve callback the loop consults before a tool that requires approval.
    run_ctx = RunContext(cancel=cancel, config=config, registry=registry, run_id=rid,
                         approve=approve, owner=owner, agent_depth=agent_depth, scope=scope,
                         policy=policy, tracer=tracer, trace_span=run_span,
                         allowed_roles=allowed_roles, session_id=session_id,
                         unattended_allow=unattended_allow)
    if resumed is not None and resumed.get("todos"):
        run_ctx.todos = resumed["todos"]   # restore the living TODO list so recitation/recall continue
    # Plan phase: one bounded ponder turn whose step list is injected as context before the loop.
    if plan_enabled and resumed is None:
        plan_text, plan_err = _single_turn(client, effective_role,
                                           [{"role": "system", "content": _PLAN_SYSTEM},
                                            {"role": "user", "content": goal}],
                                           cancel, request_timeout, hermes_mode, extra_body=reasoning_extra_body)
        if plan_err:
            log.warning(f"[{rid}] plan turn failed (continuing without a plan): {plan_err}")
            yield {"type": "notice", "message": f"planning step failed ({plan_err}); continuing without a plan"}
        if plan_text.strip():
            messages.insert(1, {"role": "system", "content": f"Plan for this task:\n{plan_text.strip()}"})
            log.info(f"[{rid}] plan injected ({len(plan_text)}c)")

    # One CheckpointStore serves two independent concerns: per-step file snapshots (checkpointEdits, for
    # rewind) and durable run state (checkpoint, for resume). Built when either gate is on. Default off ->
    # no store (byte-identical).
    checkpoint_store = resume_store   # reuse the store opened for resume (holds this run's lease)
    if checkpoint_store is None and (checkpoint_edits or checkpoint_run):
        try:
            import bob_checkpoint
            checkpoint_store = bob_checkpoint.CheckpointStore(
                db_path=agent_cfg.get("checkpointDbPath") or None, default_owner=owner or "local")
        except Exception as _e:
            log.warning(f"[{rid}] checkpoint store unavailable ({_e}); edits/run state not persisted")

    # Durable-run bookkeeping: track the terminal status/result (recorded before each terminal yield via
    # _term, written in the finally) and the run-state save helper. A row is written up front so a run that
    # fails before completing a step is still recorded and resumable.
    run_persist = checkpoint_store is not None and checkpoint_run

    def _run_metrics():
        return {"steps": steps_done, "tools": tools_run, "tokens_est": tokens_est}

    # Token usage over the run's LLM calls: what the backend reported (stream_options.include_usage),
    # or an estimate of the prompt sent + the reply when it reports none.
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "estimated": False}

    def _usage_view():
        return {**usage, "total_tokens": usage["prompt_tokens"] + usage["completion_tokens"]}

    def _save_run(status, next_step):
        if not run_persist:
            return
        try:
            checkpoint_store.save_run(rid, owner, status, goal, messages, next_step,
                                      exit_requested=exit_requested, scope=scope,
                                      agent_depth=agent_depth, todos=run_ctx.todos,
                                      metrics=_run_metrics())
        except Exception as _e:
            log.warning(f"[{rid}] run-state persist failed (continuing): {_e}")

    end_status, end_result = "running", None

    def _term(ev):
        """Record a run's terminal status/result before yielding its final/error event, so the finally
        block can persist it even if the consumer abandons the generator right after the event."""
        nonlocal end_status, end_result
        ev.setdefault("usage", _usage_view())
        if run_persist:
            if ev["type"] == "error":
                end_status, end_result = "failed", ev.get("message")
            elif ev.get("reason") == "cancelled":
                end_status, end_result = "cancelled", ev.get("result")
            else:
                end_status, end_result = "done", ev.get("result")
        yield ev

    _save_run("running", step_start)

    verified = False   # verify pass runs at most once per run (bounded)
    verify_state = None   # 'passed' | 'failed' once the verify pass ran; reported on the final event
    stop_hook_fired = False   # Stop hooks may nudge the run to continue, at most once (bounded)
    autofix_rounds = 0        # objective test-fix gate: bounded by autofix_max_rounds
    autofix_last_sig = None   # forward-progress guard: same failure twice in a row -> stop re-running
    call_counts: dict = {}   # loop-guard: tool-call signature -> times dispatched this run
    force_answer = False      # set after a fully-repeated step: next turn must answer, not call tools
    prev_sigint = _install_interrupt_handler(cancel)
    try:
        for step in range(step_start, max_steps):
            # `step` is rebound to the tool-results dict later in the body; keep the integer index for
            # checkpointing the next resume boundary.
            step_index = step
            if cancel.cancelled():
                log.info(f"[{rid}] cancelled before step {step + 1}")
                yield from _term({"type": "final", "result": _final_answer(last_content, hermes_mode),
                                  "exit_requested": exit_requested, "reason": "cancelled"})
                return

            # Mid-run steering: inject any queued steer messages as user turns at this step boundary
            # (never mid-tool-batch), so an operator can nudge the run without cancelling it.
            for _msg in cancel.drain_steer():
                log.info(f"[{rid}] steer injected ({len(_msg)}c)")
                messages.append({"role": "user", "content": _msg})
                yield {"type": "steer", "message": _msg}

            # Clear old bulky tool results (context editing) BEFORE the window trims, so the
            # freed budget lets more conversational turns survive. Only fires past the token trigger.
            if clear_tool_results and sum(_message_tokens(m) for m in messages) > clear_after_tokens:
                messages = _clear_old_tool_results(messages, registry, compact_keep_last, hermes_mode)
            messages = truncate_history(messages, max_hist, send_budget,
                                        compaction=compaction_mode, keep_last=compact_keep_last,
                                        summary_max_tokens=compact_summary_max, summary_model=effective_role,
                                        stable_prefix=stable_prefix, pin_goal=goal_msg, config=config)
            # The recitation rides only on THIS request (never persisted to `messages`, so it can't
            # accumulate or disturb truncate/the stable prefix); rebuilt each step from the live TODOs.
            send_messages = messages
            if recite_enabled:
                send_messages = messages + [{"role": "system",
                                             "content": _recitation_block(goal, run_ctx.todos)}]
            tools = openai_tools

            # Unified LLM call: always consume as a stream so `cancel` is polled between
            # chunks and an in-flight call aborts within ~1s. emit_tokens=stream gates whether
            # content deltas surface as 'token' events. One transient retry only when NOT emitting
            # (nothing surfaced yet); never mid-stream (that would re-emit tokens).
            msg = None
            # Retry transient errors with backoff. Safe to retry (even while streaming) ONLY while
            # nothing has been emitted this step — once tokens surfaced, a retry would double them.
            for attempt in range(llm_attempts):
                emitted = False
                try:
                    # We never pass cache_prompt, so llama.cpp's default (prompt/KV caching ON)
                    # applies; the stable-prefix assembly above is what makes that reuse pay off. Adding
                    # that kwarg would change request bytes and break OpenAI-compat, so we don't.
                    base_kwargs = dict(model=effective_role, messages=send_messages, tools=tools,
                                       stream=True, timeout=request_timeout,
                                       # the output reservation the history budget was computed with
                                       max_tokens=output_tokens,
                                       stream_options={"include_usage": True})
                    # Reasoning mode: forwarded via extra_body so the OpenAI client passes the
                    # non-standard chat-template kwarg straight through the proxy to llama-server. The
                    # enable_thinking value is fixed per run and the prompt bytes are unchanged, so the
                    # KV prefix cache is unaffected. Absent for cloud peers (reasoning_extra_body=None).
                    if reasoning_extra_body is not None:
                        base_kwargs["extra_body"] = reasoning_extra_body
                    if constrain_active:
                        # Attach the structured tools + tool_choice='auto' so the backend
                        # grammar-constrains any tool call. On rejection, latch the constraint off for
                        # the run and retry unconstrained (NOT a transient retry — no attempt consumed).
                        try:
                            stream_resp = client.chat.completions.create(
                                **{**base_kwargs, "tools": constrain_tools_payload, "tool_choice": "auto"})
                        except Exception as ce:
                            if not _is_unsupported_constraint(ce):
                                raise
                            log.warning(f"[{rid}] backend rejected tool-call constraint; "
                                        f"falling back to unconstrained parse: {ce}")
                            constrain_active = False
                            stream_resp = client.chat.completions.create(**base_kwargs)
                    else:
                        stream_resp = client.chat.completions.create(**base_kwargs)
                    gen = _consume_stream(stream_resp, cancel=cancel, emit_tokens=stream, hermes=hermes_mode)
                    while True:
                        try:
                            _kind, text = next(gen)
                            emitted = True
                            yield {"type": "token", "text": text}
                        except StopIteration as stop:
                            msg = stop.value
                            break
                    break
                except Exception as e:
                    if attempt + 1 < llm_attempts and _is_transient(e) and not emitted:
                        delay = llm_backoff * (attempt + 1)
                        log.warning(f"[{rid}] transient LLM error step {step + 1} (retry in {delay:.0f}s): {e}")
                        print(f"[retry] transient LLM error at step {step + 1} (retry in {delay:.0f}s): {e}",
                              file=sys.stderr)
                        _sleep_cancellable(delay, cancel)
                        if cancel is not None and cancel.cancelled():
                            yield from _term({"type": "final",
                                              "result": _final_answer(last_content, hermes_mode),
                                              "exit_requested": exit_requested, "reason": "cancelled"})
                            return
                        continue
                    log.error(f"[{rid}] LLM error step {step + 1}: {e}")
                    kind = ("upstream_unreachable" if type(e).__name__ in ("APIConnectionError", "ConnectionError")
                            else "upstream_error")
                    yield from _term({"type": "error", "message": f"LLM error at step {step + 1}: {e}",
                                      "kind": kind})
                    return

            if getattr(msg, "cancelled", False):
                log.info(f"[{rid}] cancelled mid-stream step {step + 1}")
                yield from _term({"type": "final", "result": _final_answer(last_content, hermes_mode),
                                  "exit_requested": exit_requested, "reason": "cancelled"})
                return

            rep_usage = getattr(msg, "usage", None)
            if rep_usage is not None and getattr(rep_usage, "prompt_tokens", None) is not None:
                usage["prompt_tokens"] += int(rep_usage.prompt_tokens or 0)
                usage["completion_tokens"] += int(getattr(rep_usage, "completion_tokens", 0) or 0)
            else:
                usage["estimated"] = True
                usage["prompt_tokens"] += sum(_message_tokens(m) for m in send_messages) + request_tools_tokens
                usage["completion_tokens"] += _estimate_tokens(msg.content or "")
            finish_reason = getattr(msg, "finish_reason", None)
            reasoned = int(getattr(msg, "reasoning_chars", 0) or 0)

            if not (msg.content or msg.tool_calls):  # empty completion: say why when we can tell
                log.error(f"[{rid}] empty response step {step + 1} finish_reason={finish_reason} "
                          f"reasoning_chars={reasoned}")
                if reasoned and finish_reason == "length":
                    why = (f"the model spent its whole output budget ({output_tokens} tokens) reasoning and "
                           f"returned no answer. Turn thinking off (/think off) or {_raise_output_hint()}")
                elif reasoned:
                    why = "the model returned only reasoning and no answer"
                elif finish_reason == "length":
                    why = f"the reply hit the output limit ({output_tokens} tokens) before any text"
                else:
                    why = f"finish_reason={finish_reason or 'unknown'}"
                yield from _term({"type": "error", "kind": "empty_response",
                                  "message": f"LLM returned an empty response at step {step + 1}: {why}."})
                return

            content = msg.content or ""
            if finish_reason == "length":
                # Cut off at the output limit. A partial tool call never runs; partial answer text is
                # returned marked as truncated rather than passed off as complete.
                visible = _visible_text(content, hermes_mode)
                last_content = content
                steps_done += 1
                tokens_est += _estimate_tokens(content)
                _cap("assistant", visible or content)
                log.warning(f"[{rid}] reply truncated at max_tokens={output_tokens} step {step + 1}")
                if not visible:
                    yield from _term({"type": "error", "kind": "truncated",
                                      "message": (f"The reply was cut off at the output limit ({output_tokens} "
                                                  "tokens) in the middle of a tool call, so nothing ran. To "
                                                  f"fix it, {_raise_output_hint()}.")})
                    return
                marker = (f"\n\n[reply truncated at the output limit ({output_tokens} tokens); for longer "
                          f"answers, {_raise_output_hint()}]")
                yield from _term({"type": "final", "result": visible + marker,
                                  "exit_requested": exit_requested, "reason": "truncated",
                                  "hint": _raise_output_hint()})
                return

            last_content = content
            _cap("assistant", content)      # every model turn, incl. the final answer (not appended to messages)
            steps_done += 1
            tokens_est += _estimate_tokens(content)
            tool_calls = msg.tool_calls
            if not tool_calls and "<tool_call>" in content:
                tool_calls = _parse_hermes_tool_calls(content)

            log.info(f"[{rid}] step {step + 1} content_len={len(content)} tool_calls={len(tool_calls or [])}")

            # No tool calls — final answer.
            if not tool_calls:
                final = _strip_tool_calls(content) if hermes_mode else content
                # Objective lint/test-fix gate (before the subjective verify critic): run the configured
                # check command(s) through the sandbox seam; on failure feed the parsed failures back so
                # the model fixes and re-runs. Bounded by autofix_max_rounds, with a forward-progress
                # guard that stops if the same failure repeats. This checks exit codes + output, NOT goal
                # satisfaction -- it is distinct from the verify critic below.
                if autofix_enabled and autofix_rounds < autofix_max_rounds and step + 1 < max_steps:
                    import bob_testfix
                    cid = f"autofix.{autofix_rounds}"
                    cmds = {"lintCmd": (agent_cfg.get("lintCmd") or "").strip(),
                            "testCmd": (agent_cfg.get("testCmd") or "").strip()}
                    action = {"call_id": cid, "tool": "run_checks",
                              "arguments": json.dumps(cmds), "risk": "confirm"}
                    yield {"type": "approval_required", **action}
                    if _resolve_approval(approve, action):
                        yield {"type": "tool_call", "call_id": cid, "name": "run_checks",
                               "arguments": json.dumps(cmds)}
                        summaries = bob_testfix.run_checks(config)
                        failed = [s for s in summaries if not s.passed]
                        feedback = ("\n\n".join(s.as_feedback() for s in failed) if failed
                                    else "All configured checks passed.")
                        yield {"type": "tool_result", "call_id": cid, "name": "run_checks",
                               "result": feedback}
                        if failed:
                            sig = "|".join(s.signature for s in failed)
                            if sig == autofix_last_sig:
                                log.info(f"[{rid}] test-fix: no forward progress (same failure) — stopping")
                            else:
                                autofix_rounds += 1
                                autofix_last_sig = sig
                                log.info(f"[{rid}] test-fix: checks failed (round {autofix_rounds}) — continuing")
                                messages.append({"role": "user", "content":
                                    f"Automated checks failed:\n{feedback}\n"
                                    "Fix the cause and continue; the checks will run again."})
                                continue
                # Verify pass (once): a critic turn checks the answer satisfies the goal / no tool
                # silently failed. On "not done" (and steps remain) inject the critique and continue.
                if verify_enabled and not verified and step + 1 < max_steps:
                    verified = True
                    critique, verify_err = _single_turn(client, effective_role,
                        [{"role": "system", "content": _VERIFY_SYSTEM},
                         {"role": "user", "content": f"Goal:\n{goal}\n\nProposed final answer:\n{final}"}],
                        cancel, request_timeout, hermes_mode, extra_body=reasoning_extra_body)
                    if verify_err or not critique.strip():
                        # A failed or empty review is not an acceptance: say the answer went unverified.
                        why = verify_err or "the reviewer returned nothing"
                        log.warning(f"[{rid}] verify: could not review the answer ({why})")
                        verify_state = "failed"
                        yield {"type": "notice", "message": f"verify step failed ({why}); answer is unverified"}
                    elif not critique.strip().upper().startswith("DONE"):
                        log.info(f"[{rid}] verify: not done — continuing ({critique[:120]!r})")
                        messages.append({"role": "user", "content":
                            f"A reviewer flagged issues with your answer:\n{critique.strip()}\n"
                            "Address them, using tools if needed, then give the corrected final answer."})
                        continue
                    else:
                        verify_state = "passed"
                # Stop hooks may nudge the run to keep going (inject context) instead of finalizing --
                # at most once, so a hook can't strand the run in a loop.
                if not stop_hook_fired and step + 1 < max_steps:
                    stop_hook_fired = True
                    inject = _fire_stop_hooks(registry, final, run_ctx, log, rid)
                    if inject:
                        log.info(f"[{rid}] Stop hook injected -- continuing")
                        messages.append({"role": "user", "content": inject})
                        continue
                log.info(f"[{rid}] final len={len(final)}")
                final_ev = {"type": "final", "result": final, "exit_requested": exit_requested,
                            "reason": "answer"}
                if verify_state is not None:
                    final_ev["verify"] = verify_state
                yield from _term(final_ev)
                return

            # Loop-guard: the previous step was nothing but repeated calls and we told the model to
            # stop. It emitted tool calls anyway — take its content as the answer instead of spinning.
            if force_answer:
                final = (_strip_tool_calls(content) if hermes_mode else content).strip() or _STUCK_ANSWER_MSG
                log.warning(f"[{rid}] forcing answer after repeated tool calls")
                yield from _term({"type": "final", "result": final,
                                  "exit_requested": exit_requested, "reason": "forced_answer"})
                return

            call_ids = [_call_id(tc, step, idx) for idx, tc in enumerate(tool_calls)]
            for tc, cid in zip(tool_calls, call_ids):
                yield {"type": "tool_call", "call_id": cid,
                       "name": tc.function.name, "arguments": tc.function.arguments}

            # Per-step checkpoint: before any mutating call in this step runs, snapshot the files it will
            # touch (via each tool's AFFECTS declaration) so the step can be rewound. One checkpoint per
            # (run_id, step); no-op when checkpointEdits is off or the step has no mutating calls.
            if checkpoint_store is not None and checkpoint_edits:
                affected = []
                for tc in tool_calls:
                    if tc.function.name in getattr(registry, "mutating_tools", set()):
                        fn = getattr(registry, "affects", {}).get(tc.function.name)
                        if fn:
                            try:
                                affected += fn(json.loads(tc.function.arguments or "{}"))
                            except Exception:
                                pass
                if affected:
                    try:
                        checkpoint_store.snapshot(rid, step, owner or "local", affected)
                    except Exception as _e:
                        log.warning(f"[{rid}] checkpoint snapshot failed (continuing): {_e}")

            # Approval is per-tool-call and event-driven (see _dispatch_with_approval); the loop
            # may run side-effect-free 'allow' calls concurrently (maxParallelTools). _run_tool_calls
            # yields the events and returns the ordered results; we append them per tool-format below.
            # The final `messages` content is order-identical to the sequential loop; when max_parallel==1
            # the whole path is byte-identical (every call goes through _dispatch_with_approval).
            step = yield from _run_tool_calls(
                tool_calls, call_ids, registry=registry, run_ctx=run_ctx,
                agency=effective_agency, approve=approve, log=log, rid=rid, cancel=cancel,
                exit_on_tools=exit_on_tools, max_parallel=max_parallel_tools,
                call_counts=call_counts, dup_limit=dup_limit)
            tools_run += step["tools_run"]
            tokens_est += step["tokens_est"]
            if step["exit_requested"]:
                exit_requested = True
            if step["cancelled"]:
                yield from _term({"type": "final", "result": _final_answer(last_content, hermes_mode),
                                  "exit_requested": exit_requested, "reason": "cancelled"})
                return
            # A tool may return image(s) via the {"__images__":[...], "text":...} contract;
            # split them out so the transcript carries the text summary and the images ride as
            # image_url blocks in a follow-up user turn (below). Non-image results are unchanged.
            step_images = []
            if hermes_mode:
                messages.append({"role": "assistant", "content": content})
                tool_results = []
                for (tc, cid, result) in step["results"]:
                    text, imgs = _split_tool_result_images(result)
                    step_images += imgs
                    tool_results.append(
                        f'<tool_response>{{"name": "{tc.function.name}", "content": {json.dumps(text)}}}</tool_response>')
                messages.append({"role": "user", "content": "\n".join(tool_results)})
            else:
                messages.append(build_assistant_message(msg))
                for (tc, cid, result) in step["results"]:
                    text, imgs = _split_tool_result_images(result)
                    step_images += imgs
                    messages.append(build_tool_message(tc, text))
            for (tc, _cid, result) in step["results"]:      # capture tool turns before compaction sees them
                _cap("tool", _split_tool_result_images(result)[0], tool_name=tc.function.name)
            # A tool that returned image(s) threads them into the next turn as image_url blocks and
            # flips the run to the vision role so the model can actually see them (image_url in a user
            # message is the portable form — works for both hermes and openai tool-result modes).
            if step_images:
                vision_role = get_role(config, "vision")
                refusal = image_refusal(config, vision_role)
                if refusal:
                    # No model here can see them: say so in the transcript instead of sending a request
                    # the backend would reject.
                    messages.append({"role": "user", "content":
                        f"[the tool above returned {len(step_images)} image(s), which were not shown: {refusal}]"})
                    log.info(f"[{rid}] tool returned {len(step_images)} image(s); vision unavailable")
                else:
                    image_msg = {"role": "user",
                                 "content": [{"type": "text", "text": "[image(s) returned by the tool above]"}]
                                            + [_image_content_block(s) for s in step_images]}
                    # The switch is tentative: if the vision window can't hold the head, the images and a
                    # reply, every piece of run state below goes back to what it was before the switch.
                    prev_state = (effective_role, reasoning_extra_body, budget, tool_schemas, compact_after,
                                  registry, base_system, openai_tools, constrain_tools_payload,
                                  constrain_active, request_tools_tokens)
                    sys_idx, prev_sys_msg, dropped_now = None, None, []
                    # The image turn rides in the history, charged at the flat per-image cost.
                    image_cost = _message_tokens(image_msg)
                    if vision_role != effective_role:
                        effective_role = vision_role
                        reasoning_extra_body = _reasoning_body(effective_role)
                        # The tools were fitted to the original role's window; a smaller vision window
                        # (a 4096-token vision model) needs them refitted before the image turn is sent.
                        # A resumed transcript carries its own system prompt, so only a head this run
                        # built is rewritten.
                        sys_idx = next((i for i, m in enumerate(messages) if m.get("role") == "system"
                                        and m.get("content") == base_system), None)
                        if tool_schemas and sys_idx is not None:
                            v_output = max_tokens or role_output_tokens(config, effective_role)
                            v_window = _context_budget(config, effective_role, max_context_cfg, v_output, 0)[0]
                            fitted, v_compact, dropped_now = _fit_tools_to_window(
                                system_prompt, tool_schemas, window=v_window, output=v_output,
                                hermes=hermes_mode, compact_after=compact_after,
                                constrained=constrained_tool_calls, reserve=image_cost)
                            if fitted is not tool_schemas or v_compact != compact_after:
                                tool_schemas, compact_after = fitted, v_compact
                                if dropped_now and hasattr(registry, "filtered"):
                                    registry = registry.filtered(allow={_tool_name(t) for t in tool_schemas})
                                    run_ctx.registry = registry
                                base_system = (system_prompt + _hermes_tool_system_addendum(tool_schemas, compact_after)
                                               if hermes_mode and tool_schemas else system_prompt)
                                prev_sys_msg = messages[sys_idx]
                                messages[sys_idx] = {**prev_sys_msg, "content": base_system}
                                openai_tools = (_openai_tools_payload(tool_schemas, compact_after)
                                                if (tool_schemas and not hermes_mode) else None)
                                constrain_tools_payload = None
                                if constrained_tool_calls and tool_schemas:
                                    constrain_tools_payload = (openai_tools
                                                               or _openai_tools_payload(tool_schemas, compact_after))
                                constrain_active = constrain_active and constrain_tools_payload is not None
                                request_tools_tokens = _tools_payload_tokens(openai_tools or constrain_tools_payload)
                        budget = _budget_for(effective_role)
                    # The reply shrinks to what the window leaves after the head, the schemas, the images
                    # and the minimal history.
                    room = (budget.total - request_tools_tokens - budget.head - _tail_reserve(budget.total)
                            - image_cost)
                    if not budget.fits or room < min(budget.out, _MIN_OUTPUT_TOKENS):
                        need = budget.head + request_tools_tokens + image_cost
                        (effective_role, reasoning_extra_body, budget, tool_schemas, compact_after, registry,
                         base_system, openai_tools, constrain_tools_payload, constrain_active,
                         request_tools_tokens) = prev_state
                        run_ctx.registry = registry
                        if prev_sys_msg is not None:
                            messages[sys_idx] = prev_sys_msg
                        refusal = (f"the {vision_role} model's context window is too small for them next to "
                                   f"the system prompt and tools (~{need} tokens plus a reply)")
                        messages.append({"role": "user", "content":
                            f"[the tool above returned {len(step_images)} image(s), which were not shown: {refusal}]"})
                        log.info(f"[{rid}] tool returned {len(step_images)} image(s); {refusal}")
                    else:
                        if dropped_now:
                            yield {"type": "notice", "message": (
                                f"The {effective_role} model's window can't hold all the tools; left out "
                                f"{len(dropped_now)} for the rest of this run: "
                                f"{', '.join(dropped_now)}.")}
                            log.info(f"[{rid}] vision window: dropped {len(dropped_now)} tools {dropped_now}")
                        output_tokens = min(budget.out, room)
                        send_budget = max(0, budget.total - request_tools_tokens - output_tokens)
                        messages.append(image_msg)
                        log.info(f"[{rid}] tool returned {len(step_images)} image(s); routing to vision={effective_role}")

            # Loop-guard: this step was ENTIRELY repeated calls (all blocked). Tell the model to stop
            # calling tools and answer; the force_answer latch makes the next turn finalize regardless.
            if step.get("fully_blocked"):
                log.warning(f"[{rid}] all tool calls were repeats (step {steps_done}) — forcing an answer")
                messages.append({"role": "user", "content":
                    "You are repeating identical tool calls that add no new information. Do NOT call any "
                    "tool now — answer me directly using what you already have."})
                force_answer = True

            # Step boundary reached with the tool results now in `messages`: persist so a killed run can
            # resume from the next step without re-running the tools whose results are already recorded.
            _save_run("running", step_index + 1)

        log.warning(f"[{rid}] stopped after {max_steps} steps without a final answer")
        print(f"Agent stopped after {max_steps} steps without a final answer.", file=sys.stderr)
        yield from _term({"type": "final", "result": None, "exit_requested": exit_requested,
                          "reason": "max_steps"})
    finally:
        _restore_interrupt_handler(prev_sigint)
        # Record the run's terminal status/result (recorded by _term before each terminal yield; left
        # 'running' if the run was interrupted with no terminal event, so it stays resumable).
        if run_persist:
            try:
                checkpoint_store.set_status(rid, owner, end_status, end_result)
            except Exception as _e:
                log.warning(f"[{rid}] run-state terminal status write failed: {_e}")
        if resume_store is not None and lease_token is not None:
            try:
                resume_store.release_lease(rid, owner, lease_token)
            except Exception as _e:
                log.warning(f"[{rid}] run lease release failed: {_e}")
        # Close the run span with the same counters as the metrics line (no-op when disabled).
        run_span.end(attributes={"steps": steps_done, "tools": tools_run, "tokens_est": tokens_est})
        # One metrics line per run so a single `grep <rid>` reconstructs it end to end.
        log.info(
            f"[{rid}] done steps={steps_done} tools={tools_run} tokens~={tokens_est} "
            f"ms={(time.monotonic() - t_start) * 1000:.0f} registry_build_ms={reg_build_ms:.0f}"
        )


class RunResult(SimpleNamespace):
    """What a run came to, folded from its event stream by fold_events: `result` (the final answer or
    None), `reason` (the final event's reason), `error` / `error_kind` (set when the run ended in an
    error event), `exit_requested`, `usage` (the run's token usage), `tools_used` (tool names in call
    order) and `steps` (tool calls made)."""


class AgentRunError(RuntimeError):
    """A run ended in an error event. `kind` is the event's kind (upstream_unreachable, upstream_error,
    empty_response, truncated, vision_unavailable, not_found, conflict)."""

    def __init__(self, message: str, kind: str = None):
        super().__init__(message)
        self.kind = kind or "error"


def fold_events(events, on_event=None) -> RunResult:
    """THE consumer for callers that only need a run's outcome (the agent API, the task worker, skills,
    sub-agents): drains the event stream into a RunResult, handing every event to `on_event(ev)` first
    when given. A raised exception becomes an error result rather than propagating."""
    out = RunResult(result=None, reason=None, error=None, error_kind=None, exit_requested=False,
                    usage=None, tools_used=[], steps=0)
    try:
        for ev in events:
            if on_event is not None:
                on_event(ev)
            t = ev.get("type")
            if t == "tool_call":
                out.tools_used.append(ev.get("name"))
                out.steps += 1
            elif t == "final":
                out.result = ev.get("result")
                out.reason = ev.get("reason")
                out.exit_requested = bool(ev.get("exit_requested", False))
                out.usage = ev.get("usage") or out.usage
            elif t == "error":
                out.error = ev.get("message") or "error"
                out.error_kind = ev.get("kind") or "error"
                out.usage = ev.get("usage") or out.usage
    except Exception as e:  # noqa: BLE001 (a crashing run is reported, never raised into the consumer)
        out.error = str(e) or type(e).__name__
        out.error_kind = "error"
    return out


def usage_tokens(usage, *fallback_texts) -> int:
    """Tokens a run spent: the usage's prompt + completion total, else an estimate over `fallback_texts`
    (e.g. goal + answer) when a run reported no usage at all."""
    if usage and usage.get("total_tokens"):
        return int(usage["total_tokens"])
    return sum(_estimate_tokens(t or "") for t in fallback_texts)


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
    no_tools: bool = False,
    max_tokens: int = None,
    images: list = None,
    resume: str = None,
    think: bool = None,
    quiet: bool = False,
    raise_on_error: bool = False,
    session_id: str = None,
    unattended_allow=None,
    allowed_roles=None,
) -> tuple[str | None, bool]:
    """Blocking wrapper over run_agent_events for the CLI: prints tool previews to stderr,
    streams/echoes the final answer to stdout, and returns (result, exit_requested).

    `quiet=True` prints nothing (a server or worker embedding the loop). `raise_on_error=True` raises
    AgentRunError (carrying the error event's kind) instead of returning (None, ...), so a caller can
    tell an upstream failure from a run that simply produced no answer.

    `approve` is passed through neutrally (default None -> fail-closed deny). The wrapper does
    NOT auto-install a console approver: the interactive CLI entry (main) installs the console
    approver explicitly on a TTY; the interactive shell passes its own TUI approver."""
    effective_agency = agency or config.get("agent", {}).get("agency", "show")
    state = {"streamed": False}

    def _echo(ev):
        if quiet:
            return
        t = ev["type"]
        if t == "token":
            sys.stdout.write(ev["text"])
            sys.stdout.flush()
            state["streamed"] = True
        elif t == "notice":
            print(ev["message"], file=sys.stderr)
        elif t == "tool_call":
            if effective_agency != "silent":
                preview = ev["arguments"][:120].replace("\n", " ")
                print(f"\033[36m  → {ev['name']}({preview})\033[0m", file=sys.stderr)
        elif t == "tool_result":
            if effective_agency != "silent":
                preview = ev["result"][:100] + ("..." if len(ev["result"]) > 100 else "")
                print(f"\033[90m    {preview}\033[0m", file=sys.stderr)
        elif t == "final":
            if state["streamed"]:
                if ev.get("reason") == "truncated":
                    print(ev["result"][ev["result"].rfind("\n\n["):] if ev.get("result") else "")
                else:
                    print()  # newline after streamed tokens
            elif ev.get("result") is not None:
                print(ev["result"])
        elif t == "error":
            print(ev["message"], file=sys.stderr)

    out = fold_events(run_agent_events(
        goal, config, role=role, agency=agency,
        exit_on_tools=exit_on_tools, registry=registry, stream=stream, history=history,
        cancel=cancel, run_id=run_id, approve=approve, owner=owner, agent_depth=agent_depth,
        scope=scope, no_tools=no_tools, max_tokens=max_tokens, images=images, resume=resume,
        think=think, session_id=session_id, unattended_allow=unattended_allow, allowed_roles=allowed_roles,
    ), on_event=_echo)
    if out.error is not None:
        if raise_on_error:
            raise AgentRunError(out.error, out.error_kind)
        return None, out.exit_requested
    return out.result, out.exit_requested


def main():
    args = parse_args()
    from bob_core import load_config

    try:
        config = load_config()
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    goal = " ".join(args.goal)
    # --deep turns on the plan/verify/self-repair phases for this CLI run (config default is off).
    if getattr(args, "deep", False):
        ag = config.setdefault("agent", {})
        ag["plan"] = ag["verify"] = ag["selfRepair"] = True
    exit_on_tools = set(t.strip() for t in args.exit_on_tool.split(",") if t.strip()) if args.exit_on_tool else None
    # Interactive CLI: approve gated tools at the console when attached to a TTY (piped/CI → None
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
        import osenv
        logs_dir = osenv.cache_dir()
        # Temp + atomic replace (same pattern as config.json) so a concurrent toast
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
