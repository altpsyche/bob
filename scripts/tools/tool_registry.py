"""Bob ToolRegistry — discover, validate, and serve tool definitions.

Replaces the loose discover_tools() pattern. Build once at startup; pass the
registry to every run_agent() call so tool modules aren't re-imported per request.

Lifecycle:
  Phase 1  Import    — load module from disk; distinct error from Phase 3
  Phase 2  Contract  — TOOL_DEFS, DISPATCH, configure() present and consistent
  Phase 3  Configure — call configure(config) with full runtime config

Usage:
    registry = ToolRegistry.build(config, disabled_names={"play"})
    # pass to run_agent(goal, config, registry=registry)
    result = registry.dispatch_call("memory_recall", '{"query": "todo list"}')
"""
import contextvars
import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent.parent

_SKIP_STEMS = frozenset({"tool_loader", "tool_registry"})

# NE0/O1 seam — the run-scoped context (cancel token, config, registry, run_id, approve callback) that
# dispatch_call sets around a tool call. Tools that need it (e.g. a future sub-agent tool) read it via
# get_run_context() without any change to their fn(**args) signature. None outside a dispatched call.
_RUN_CONTEXT: contextvars.ContextVar = contextvars.ContextVar("bob_run_context", default=None)


def get_run_context():
    """Return the RunContext for the tool call currently executing, or None. NE0 seam for O1."""
    return _RUN_CONTEXT.get()


class ToolRegistry:
    def __init__(self):
        self.tool_schemas: list = []
        self.dispatch: dict = {}
        self.exit_voice_tools: set = set()
        # NE0 — tools that always require approval before running (module sets REQUIRES_APPROVAL=True,
        # e.g. shell_run). The agent loop asks the approve callback before dispatching these; the tool
        # itself no longer prompts on stdin. O6 layers richer per-tool risk policy on top of this set.
        self.approval_required_tools: set = set()
        # (tool_name, phase, message) — phase: "import" | "contract" | "configure"
        self.errors: list[tuple[str, str, str]] = []
        self._loaded_names: set = set()
        # M7 — per-result cap (chars). Derived from agent.maxToolResultTokens in build().
        self.max_result_chars: int = 4000
        # NE0/O3 seam — full text of results that dispatch_call had to truncate, addressable by handle
        # so the trimmed tail is retained (not silently lost) for a future read_result tool (O3 wires it
        # as model-callable). Bounded to the most recent few to keep memory flat.
        self._result_store: dict = {}
        self._result_seq: int = 0
        self._result_store_max: int = 8

    # ------------------------------------------------------------------
    # Discovery — single source shared by build() and the loader CLI (M16)
    # ------------------------------------------------------------------

    @staticmethod
    def iter_all_tools():
        """Yield (name, kind, path) for every discoverable tool file (system + plugin),
        unfiltered. Discovery lives only here so build() and tool_loader's CLI agree."""
        tools_dir = REPO / "scripts" / "tools"
        for f in sorted(tools_dir.glob("*.py")):
            if f.stem not in _SKIP_STEMS:
                yield f.stem, "system", f
        plugins_dir = REPO / "plugins"
        if plugins_dir.exists():
            for d in sorted(plugins_dir.iterdir()):
                if d.is_dir() and (d / "tool.py").exists():
                    yield d.name, "plugin", d / "tool.py"

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def build(cls, config: dict, disabled_names: set = None, quiet: bool = False) -> "ToolRegistry":
        """Discover all tools, validate the contract, configure them.

        disabled_names: tool directory/stem names to skip entirely.
        quiet: suppress the "[bob] tools: …" startup summary (the interactive shell wants a clean
            splash — genuine per-tool load warnings still print). Errors remain on the registry.
        """
        registry = cls()
        disabled = disabled_names or set()
        # M7 — token-aware per-result cap (approx 4 chars/token) so one large tool output
        # can't blow the context budget. maxToolResultTokens defaults to keep the prior 4000-char cap.
        registry.max_result_chars = int(config.get("agent", {}).get("maxToolResultTokens", 1000)) * 4

        all_tools = list(cls.iter_all_tools())

        # Warn about disabled names that match no discoverable tool (likely a typo).
        all_discoverable = {name for name, _, _ in all_tools}
        for name in sorted(disabled - all_discoverable):
            print(
                f"[warn] '{name}' in disabledTools but no matching tool file found",
                file=sys.stderr,
            )

        # Load each non-disabled tool.
        for tool_name, _kind, path in all_tools:
            if tool_name in disabled:
                continue
            registry._load_one(tool_name, path, config)

        if not quiet:
            registry._print_startup_summary()
        return registry

    # ------------------------------------------------------------------
    # Internal loading phases
    # ------------------------------------------------------------------

    def _load_one(self, tool_name: str, path: Path, config: dict) -> None:
        # Phase 1: Import
        try:
            spec = importlib.util.spec_from_file_location(f"bob_tool_{tool_name}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as e:
            self.errors.append((tool_name, "import", str(e)))
            print(f"[warn] tool '{tool_name}' failed to import: {e}", file=sys.stderr)
            return

        # Feature gate (optional): a tool tied to a config-gated feature may export
        # `enabled(config) -> bool`. When it returns False the tool is silently NOT registered (not an
        # error) — e.g. the memory tools stay out of the agent's toolset while memory.enabled is false,
        # so the model can't recall/recite disabled-feature data. A raising predicate loads normally.
        enabled_fn = getattr(mod, "enabled", None)
        if callable(enabled_fn):
            try:
                if not enabled_fn(config):
                    return
            except Exception:
                pass

        # Phase 2: Contract check
        tool_defs = getattr(mod, "TOOL_DEFS", None)
        mod_dispatch = getattr(mod, "DISPATCH", None)
        configure_fn = getattr(mod, "configure", None)

        if tool_defs is None:
            self.errors.append((tool_name, "contract", "missing TOOL_DEFS"))
            print(f"[warn] tool '{tool_name}' missing TOOL_DEFS", file=sys.stderr)
            return
        if mod_dispatch is None:
            self.errors.append((tool_name, "contract", "missing DISPATCH"))
            print(f"[warn] tool '{tool_name}' missing DISPATCH", file=sys.stderr)
            return
        if not callable(configure_fn):
            self.errors.append((tool_name, "contract", "missing configure()"))
            print(f"[warn] tool '{tool_name}' missing configure() function", file=sys.stderr)
            return

        # Cross-check TOOL_DEFS function names against DISPATCH keys.
        defs_names = {
            td.get("function", {}).get("name")
            for td in tool_defs
            if td.get("function", {}).get("name")
        }
        missing_in_dispatch = defs_names - set(mod_dispatch.keys())
        if missing_in_dispatch:
            # M9 — hard contract error: a TOOL_DEFS name with no DISPATCH entry would load and
            # then fail at call time with "Unknown tool". Fail loudly at load and skip the tool.
            self.errors.append(
                (tool_name, "contract",
                 f"TOOL_DEFS declares {missing_in_dispatch} with no matching DISPATCH key")
            )
            print(
                f"[warn] tool '{tool_name}': TOOL_DEFS declares {missing_in_dispatch}"
                f" but DISPATCH has no matching key — skipping tool (contract error)",
                file=sys.stderr,
            )
            return

        # Phase 3: Configure
        try:
            configure_fn(config)
        except Exception as e:
            self.errors.append((tool_name, "configure", str(e)))
            print(f"[warn] tool '{tool_name}' configure() failed: {e}", file=sys.stderr)
            return

        # All phases passed — register.
        self._loaded_names.add(tool_name)
        self.tool_schemas.extend(tool_defs)
        self.dispatch.update(mod_dispatch)

        if getattr(mod, "EXIT_VOICE", False):
            for td in tool_defs:
                fn_name = td.get("function", {}).get("name")
                if fn_name:
                    self.exit_voice_tools.add(fn_name)

        if getattr(mod, "REQUIRES_APPROVAL", False):
            for td in tool_defs:
                fn_name = td.get("function", {}).get("name")
                if fn_name:
                    self.approval_required_tools.add(fn_name)

    def _print_startup_summary(self) -> None:
        names = sorted(self._loaded_names)
        if names:
            print(f"[bob] tools: {' '.join(names)} ({len(names)})", file=sys.stderr)
        else:
            print("[bob] tools: none loaded", file=sys.stderr)
        if self.errors:
            print(
                f"[bob] {len(self.errors)} tool(s) had load errors — see warnings above",
                file=sys.stderr,
            )

    # ------------------------------------------------------------------
    # Runtime dispatch
    # ------------------------------------------------------------------

    def dispatch_call(self, tool_name: str, arguments_json: str, context=None) -> str:
        """Execute a named tool call. Always returns a string (the format agents expect).

        `context` (NE0): an optional RunContext made reachable to the tool via get_run_context()
        for the duration of the call — carries cancel/config/registry/run_id/approve without changing
        tool signatures. Backward compatible: dispatch_call(name, json) still works (context=None).

        Handles the __parse_error__ pseudo-name injected by _parse_hermes_tool_calls
        when the LLM emits invalid JSON inside a <tool_call> block.
        """
        if tool_name == "__parse_error__":
            try:
                info = json.loads(arguments_json)
                return (
                    f"Your previous tool call contained malformed JSON "
                    f"({info.get('error', 'parse error')}). "
                    f"Please fix the JSON syntax and retry."
                )
            except Exception:
                return "Your previous tool call contained malformed JSON. Please retry with valid JSON."

        fn = self.dispatch.get(tool_name)
        if fn is None:
            return f"Unknown tool: {tool_name}"
        token = _RUN_CONTEXT.set(context)
        try:
            out = str(fn(**json.loads(arguments_json)))
            if len(out) > self.max_result_chars:
                out = self._truncate_and_retain(out)
            return out
        except json.JSONDecodeError as e:
            return f"Bad arguments JSON for {tool_name}: {e}"
        except Exception as e:
            return f"Tool error ({tool_name}): {e}"
        finally:
            _RUN_CONTEXT.reset(token)

    def _truncate_and_retain(self, out: str) -> str:
        """Cap a result to max_result_chars but RETAIN the full text under a handle (NE0/O3 seam), so
        the trimmed tail is recoverable via read_result() instead of being discarded."""
        self._result_seq += 1
        handle = f"r{self._result_seq}"
        self._result_store[handle] = out
        if len(self._result_store) > self._result_store_max:
            oldest = min(self._result_store, key=lambda k: int(k[1:]))
            self._result_store.pop(oldest, None)
        cut = len(out) - self.max_result_chars
        return out[: self.max_result_chars] + f"\n[...truncated {cut} chars; retained as {handle}]"

    def read_result(self, handle: str, offset: int = 0, length: int = 4000) -> str:
        """O3 seam — return a window of a previously-truncated result so the trimmed tail can be
        re-read rather than lost. Not yet exposed as a model-callable tool (O3 wires that)."""
        full = self._result_store.get(handle)
        if full is None:
            return f"Unknown result handle: {handle}"
        return full[offset: offset + length]

    def filtered(self, deny=None, allow=None) -> "_RegistryView":
        """NE0/O1 seam — a lightweight VIEW of this registry with a narrowed tool set, so a sub-agent
        (O1) can run with fewer tools (e.g. deny={'shell_run'}) WITHOUT a full ~140 ms rebuild. The
        view shares the already-imported dispatch, result store, and caps; only the visible schema
        list and the callable set are restricted. Keyed on function names (what the model calls).
        `allow` (if given) is a whitelist; `deny` is a blacklist; both may be combined."""
        deny_set = set(deny or ())
        allow_set = set(allow) if allow is not None else None

        def visible(name: str) -> bool:
            if allow_set is not None and name not in allow_set:
                return False
            return name not in deny_set

        return _RegistryView(self, visible)


class _RegistryView:
    """Restricted view over a ToolRegistry (see ToolRegistry.filtered). Presents the same interface
    the agent loop uses (tool_schemas / dispatch_call / exit_voice_tools / approval_required_tools)
    but hides denied tools and refuses to dispatch them, while sharing the base's imported dispatch."""

    def __init__(self, base: "ToolRegistry", visible):
        self._base = base
        self._visible = visible
        self.tool_schemas = [
            s for s in base.tool_schemas if visible(s.get("function", {}).get("name"))
        ]
        self.exit_voice_tools = {n for n in base.exit_voice_tools if visible(n)}
        self.approval_required_tools = {n for n in base.approval_required_tools if visible(n)}
        self.errors = base.errors
        self._loaded_names = base._loaded_names  # informational count reflects the underlying registry

    def dispatch_call(self, tool_name: str, arguments_json: str, context=None) -> str:
        if tool_name != "__parse_error__" and not self._visible(tool_name):
            return f"Tool '{tool_name}' is not available in this context."
        return self._base.dispatch_call(tool_name, arguments_json, context=context)

    def read_result(self, handle: str, offset: int = 0, length: int = 4000) -> str:
        return self._base.read_result(handle, offset, length)
