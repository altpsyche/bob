"""Sub-agents / delegation. `spawn_agent` runs an ISOLATED nested run_agent_events (fresh system +
the subtask, no parent transcript), inherits owner/scope + the permission approver, bounds recursion via
agent.maxAgentDepth, propagates parent cancel to a child token, and returns a STRUCTURED summary
(result/steps/tools_used) — never the raw sub-transcript. Gated on agent.subAgents (default off) so the
default toolset is unchanged. A sub-run never consolidates/creates a session (run_agent_events doesn't)."""
import json
import unittest

import _common
import bob_core
import bob_loop
import spawn_agent as spawn_tool
import tool_registry
from bob_loop import CancelToken, RunContext
from tool_registry import ToolRegistry


class _SpawnableRegistry(_common.FakeRegistry):
    """FakeRegistry + a filtered() that returns a usable view (here, itself) for the sub-run."""
    def filtered(self, deny=None, allow=None):
        return self


def _ctx(cfg, *, depth=0, cancel=None, registry=None, owner="alice", scope="proj"):
    return RunContext(cancel=cancel or CancelToken(), config=cfg, registry=registry,
                      run_id="root", approve=None, owner=owner, agent_depth=depth,
                      scope=scope, policy=None)


def _cfg(**agent_over):
    cfg = _common.fake_config()
    cfg["agent"] = dict(cfg["agent"], agency="silent", **agent_over)
    return cfg


class TestGuards(unittest.TestCase):
    def test_no_run_context(self):
        self.assertIn("unavailable", spawn_tool._spawn_agent("do x"))

    def test_empty_task(self):
        tok = tool_registry._RUN_CONTEXT.set(_ctx(_cfg(), registry=_SpawnableRegistry()))
        try:
            self.assertIn("non-empty", spawn_tool._spawn_agent("   "))
        finally:
            tool_registry._RUN_CONTEXT.reset(tok)

    def test_depth_cap_refuses_a_level_past_the_cap(self):
        # cap=2: a sub-agent already at depth 2 asking for depth 3 is refused before any sub-run starts.
        ctx = _ctx(_cfg(maxAgentDepth=2), depth=2, registry=_SpawnableRegistry())
        tok = tool_registry._RUN_CONTEXT.set(ctx)
        try:
            out = spawn_tool._spawn_agent("recurse forever")
            self.assertIn("depth limit reached", out)
            self.assertIn("maxAgentDepth=2", out)
        finally:
            tool_registry._RUN_CONTEXT.reset(tok)


class TestSubRun(unittest.TestCase):
    def setUp(self):
        self._orig_check = bob_core.check_litellm
        self._orig_client = bob_core.get_llm_client
        bob_core.check_litellm = lambda config=None: True

    def tearDown(self):
        bob_core.check_litellm = self._orig_check
        bob_core.get_llm_client = self._orig_client

    def _run(self, ctx, task="summarize the repo"):
        tok = tool_registry._RUN_CONTEXT.set(ctx)
        try:
            return json.loads(spawn_tool._spawn_agent(task))
        finally:
            tool_registry._RUN_CONTEXT.reset(tok)

    def test_returns_structured_summary_not_transcript(self):
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(["the answer"])
        out = self._run(_ctx(_cfg(), registry=_SpawnableRegistry()))
        self.assertEqual(out["result"], "the answer")
        self.assertEqual(out["steps"], 0)
        self.assertEqual(out["tools_used"], [])
        self.assertNotIn("error", out)

    def test_summary_reports_tools_used(self):
        turns = ['<tool_call>{"name": "echo", "arguments": {}}</tool_call>', "done"]
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(turns)
        reg = _SpawnableRegistry({"echo": "echoed"})
        out = self._run(_ctx(_cfg(), registry=reg))
        self.assertEqual(out["result"], "done")
        self.assertEqual(out["steps"], 1)
        self.assertEqual(out["tools_used"], ["echo"])

    def test_parent_cancel_propagates_to_sub_run(self):
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(["never reached"])
        cancel = CancelToken()
        cancel.cancel()                      # parent already cancelled
        out = self._run(_ctx(_cfg(), registry=_SpawnableRegistry(), cancel=cancel))
        # The child token is cancelled too, so the sub-run stops before producing a final answer.
        self.assertIn("no final answer", out["result"])


class TestAutoRecallSuppressedAtDepth(unittest.TestCase):
    """Decision #4 — autoRecall is a root-run behavior; a sub-agent (depth>0) must not read saved notes
    every turn (mirrors the profile/BOB.md gate)."""

    def setUp(self):
        self._orig_check = bob_core.check_litellm
        self._orig_client = bob_core.get_llm_client
        self._orig_recall = bob_core.memory_recall
        bob_core.check_litellm = lambda config=None: True
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(["ok"])
        self.calls = []
        bob_core.memory_recall = lambda *a, **k: self.calls.append(1) or "some note"

    def tearDown(self):
        bob_core.check_litellm = self._orig_check
        bob_core.get_llm_client = self._orig_client
        bob_core.memory_recall = self._orig_recall

    def _cfg_autorecall(self):
        cfg = _common.fake_config()
        cfg["memory"] = {"enabled": True, "autoRecall": True, "recallK": 3, "maxInjectedTokens": 1200}
        return cfg

    def test_autorecall_runs_at_root(self):
        list(bob_loop.run_agent_events("go", self._cfg_autorecall(), agency="silent",
                                       registry=_common.FakeRegistry(), agent_depth=0))
        self.assertEqual(len(self.calls), 1)

    def test_autorecall_suppressed_in_sub_agent(self):
        list(bob_loop.run_agent_events("go", self._cfg_autorecall(), agency="silent",
                                       registry=_common.FakeRegistry(), agent_depth=1))
        self.assertEqual(len(self.calls), 0)


class TestParallelFanOut(unittest.TestCase):
    """spawn_agent is NOT mutating/approval-gated, so parallel dispatch runs several spawn_agent calls in one
    step (fan-out/fan-in) with no extra loop code — this asserts the eligibility that makes that free."""

    def test_spawn_agent_is_parallel_eligible(self):
        reg = _SpawnableRegistry()          # empty mutating_tools / approval_required_tools
        ctx = _ctx(_cfg(), registry=reg)
        self.assertTrue(bob_loop._parallel_eligible("spawn_agent", reg, ctx, "show"))

    def test_not_eligible_when_marked_mutating(self):
        reg = _SpawnableRegistry(mutating_tools={"spawn_agent"})
        ctx = _ctx(_cfg(), registry=reg)
        self.assertFalse(bob_loop._parallel_eligible("spawn_agent", reg, ctx, "show"))


class TestRegistryGating(unittest.TestCase):
    def test_spawn_agent_gated_on_subagents_flag(self):
        self.assertFalse(spawn_tool.enabled({"agent": {"subAgents": False}}))
        self.assertTrue(spawn_tool.enabled({"agent": {"subAgents": True}}))
        self.assertFalse(spawn_tool.enabled({}))

    def _cfg(self, on):
        cfg = _common.fake_config()
        cfg["agent"] = dict(cfg["agent"], subAgents=on)
        return cfg

    def test_tool_present_only_when_enabled(self):
        on = ToolRegistry.build(self._cfg(True), quiet=True)
        off = ToolRegistry.build(self._cfg(False), quiet=True)
        self.assertIn("spawn_agent", on.dispatch)
        self.assertNotIn("spawn_agent", off.dispatch)


class TestTypedRoles(unittest.TestCase):
    """agent.subAgentRoles gives a role a distinct prompt + per-role tool whitelist; any other role is a
    plain model-role override, limited to local roles unless agent.subAgentAllowPro is set."""

    def test_resolve_profile_known_and_unknown(self):
        cfg = {"subAgentRoles": {"reviewer": {"prompt": "You are a strict reviewer.",
                                              "tools": ["file_read"], "modelRole": "coder"}}}
        prof = spawn_tool.resolve_profile("reviewer", cfg)
        self.assertEqual(prof["prompt"], "You are a strict reviewer.")
        self.assertEqual(prof["tools"], ["file_read"])
        self.assertEqual(prof["modelRole"], "coder")
        plain = spawn_tool.resolve_profile("coder-pro", cfg)
        self.assertEqual(plain, {"prompt": None, "tools": None, "modelRole": "coder-pro"})

    def _drive(self, cfg, role):
        seen = {}

        class _Reg(_SpawnableRegistry):
            def filtered(self, deny=None, allow=None):
                seen["allow"] = allow
                return self

        def fake_events(goal, config, **kw):
            seen["role"] = kw.get("role")
            seen["system_prompt"] = kw.get("system_prompt")
            yield {"type": "final", "result": "ok"}

        ctx = _ctx(cfg, registry=_Reg())
        orig = bob_loop.run_agent_events
        bob_loop.run_agent_events = fake_events
        tok = tool_registry._RUN_CONTEXT.set(ctx)
        try:
            out = spawn_tool._spawn_agent("do it", role=role)
        finally:
            bob_loop.run_agent_events = orig
            tool_registry._RUN_CONTEXT.reset(tok)
        return seen, out

    def test_typed_role_passes_prompt_and_tool_view(self):
        cfg = _cfg(subAgents=True, subAgentRoles={
            "reviewer": {"prompt": "Be a reviewer.", "tools": ["file_read"], "modelRole": "coder"}})
        seen, out = self._drive(cfg, "reviewer")
        self.assertEqual(seen["allow"], ["file_read"])              # per-role tool whitelist
        self.assertEqual(seen["system_prompt"], "Be a reviewer.")  # distinct prompt
        self.assertEqual(seen["role"], "coder")                    # profile model role
        self.assertIn("ok", out)

    def test_unknown_role_is_model_override_only(self):
        seen, _ = self._drive(_cfg(subAgents=True), "coder")
        self.assertEqual(seen["role"], "coder")                    # role still means model-role
        self.assertIsNone(seen["system_prompt"])                    # persona unchanged

    def test_model_chosen_pro_role_refused_by_default(self):
        # A paid cloud peer is never reachable from a model-chosen role: the sub-run does not start.
        for role in ("coder-pro", "chat-pro", "ponder-pro"):
            seen, out = self._drive(_cfg(subAgents=True), role)
            self.assertNotIn("role", seen, role)                   # run_agent_events never reached
            self.assertIn("subAgentAllowPro", out)

    def test_unrecognized_role_refused(self):
        seen, out = self._drive(_cfg(subAgents=True), "some-cloud-alias")
        self.assertNotIn("role", seen)
        self.assertIn("unknown role", out)

    def test_pro_role_allowed_with_explicit_flag(self):
        seen, _ = self._drive(_cfg(subAgents=True, subAgentAllowPro=True), "coder-pro")
        self.assertEqual(seen["role"], "coder-pro")

    def test_configured_pro_route_counts_as_pro(self):
        # A routing override naming a cloud alias is still classed pro (not local) by the resolver.
        cfg = _cfg(subAgents=True)
        cfg["routing"] = dict(cfg["routing"], proCodeRole="glm-coder")
        local, pro = spawn_tool.local_roles(cfg)
        self.assertIn("glm-coder", pro)
        self.assertNotIn("glm-coder", local)
        self.assertIn("coder", local)

    def test_profile_model_role_is_trusted_config(self):
        # A user-authored profile may point at a pro role: that is config, not a model choice.
        cfg = _cfg(subAgents=True, subAgentRoles={"deep": {"modelRole": "ponder-pro"}})
        seen, _ = self._drive(cfg, "deep")
        self.assertEqual(seen["role"], "ponder-pro")


class _DelegatingRegistry(_SpawnableRegistry):
    """Runs spawn_agent for real (with the dispatch's run context set, as ToolRegistry does); every other
    tool is recorded and answered from the scripted results."""

    def dispatch_call(self, name, arguments_json, context=None):
        if name != "spawn_agent":
            return super().dispatch_call(name, arguments_json, context)
        self.dispatched.append(name)
        tok = tool_registry._RUN_CONTEXT.set(context)
        try:
            return spawn_tool._spawn_agent(**json.loads(arguments_json or "{}"))
        finally:
            tool_registry._RUN_CONTEXT.reset(tok)


class TestUnattendedRuleHoldsInSubRuns(unittest.TestCase):
    """Over MCP (no operator) agent.mcpAllowTools is the allow-set. It rides on the RunContext, so a
    sub-agent spawned from an MCP call cannot run a gated tool the list does not name, and spawn_agent
    itself is refused unless listed."""

    GATED = ("stack_stop", "file_write", "memory_store")

    def setUp(self):
        self._orig_check = bob_core.check_litellm
        self._orig_client = bob_core.get_llm_client
        bob_core.check_litellm = lambda config=None: True

    def tearDown(self):
        bob_core.check_litellm = self._orig_check
        bob_core.get_llm_client = self._orig_client

    def _mcp(self, allow, sub_turns):
        import bob_permissions
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(sub_turns)
        reg = _DelegatingRegistry(mutating_tools=self.GATED)
        out = bob_permissions.run_gated(reg, "spawn_agent", json.dumps({"task": "tidy up"}),
                                        config=_cfg(subAgents=True), owner="mcp", agency="silent",
                                        allow_unattended=allow, surface="mcp")
        return reg, out

    def _calls(self, *names):
        calls = "".join(f'<tool_call>{{"name": "{n}", "arguments": {{}}}}</tool_call>' for n in names)
        return [calls, "done"]

    def test_spawn_agent_is_refused_unless_listed(self):
        reg, out = self._mcp([], self._calls(*self.GATED))
        self.assertIn("refused", out)
        self.assertEqual(reg.dispatched, [])

    def test_a_sub_run_cannot_run_unlisted_gated_tools(self):
        reg, out = self._mcp(["spawn_agent"], self._calls(*self.GATED))
        self.assertEqual(reg.dispatched, ["spawn_agent"])   # none of the gated tools reached dispatch
        self.assertEqual(json.loads(out)["result"], "done")

    def test_a_listed_tool_runs_in_the_sub_run(self):
        reg, _ = self._mcp(["spawn_agent", "file_write"], self._calls(*self.GATED))
        self.assertEqual(reg.dispatched, ["spawn_agent", "file_write"])

    def test_the_parallel_path_honours_the_rule(self):
        ctx = _ctx(_cfg(), registry=_common.FakeRegistry())
        ctx.unattended_allow = frozenset()
        self.assertFalse(bob_loop._parallel_eligible("spawn_agent", ctx.registry, ctx, "silent"))
        ctx.unattended_allow = None
        self.assertTrue(bob_loop._parallel_eligible("spawn_agent", ctx.registry, ctx, "silent"))


if __name__ == "__main__":
    unittest.main()
