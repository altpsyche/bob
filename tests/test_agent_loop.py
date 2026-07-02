"""M13 — agent loop event generator (M15 refactor): tool step, final, streaming, history."""
import unittest
from types import SimpleNamespace

import _common
import bob_core
import bob_loop


class TestAgentLoop(unittest.TestCase):
    def setUp(self):
        self.cfg = _common.fake_config()
        self._orig_check = bob_core.check_litellm
        self._orig_client = bob_core.get_llm_client
        bob_core.check_litellm = lambda config=None: True

    def tearDown(self):
        bob_core.check_litellm = self._orig_check
        bob_core.get_llm_client = self._orig_client

    def test_tool_step_then_final(self):
        turns = [
            '<tool_call>{"name": "echo", "arguments": {"x": "hi"}}</tool_call>',
            "All done.",
        ]
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(turns)
        reg = _common.FakeRegistry({"echo": "echoed hi"})
        events = list(bob_loop.run_agent_events("go", self.cfg, agency="silent", registry=reg))
        types = [e["type"] for e in events]
        self.assertEqual(types, ["tool_call", "tool_result", "final"])
        self.assertEqual(events[0]["name"], "echo")
        self.assertEqual(events[1]["result"], "echoed hi")
        self.assertEqual(events[-1]["result"], "All done.")

    def test_run_agent_wrapper_returns_result(self):
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(["Just answer."])
        result, exit_req = bob_loop.run_agent("q", self.cfg, agency="silent",
                                              registry=_common.FakeRegistry())
        self.assertEqual(result, "Just answer.")
        self.assertFalse(exit_req)

    def test_streaming_emits_tokens_and_final(self):
        bob_core.get_llm_client = lambda config=None: _common.stream_client(["Hel", "lo ", "world."])
        events = list(bob_loop.run_agent_events("hi", self.cfg, agency="silent",
                                                registry=_common.FakeRegistry(), stream=True))
        tokens = [e["text"] for e in events if e["type"] == "token"]
        final = [e for e in events if e["type"] == "final"][-1]
        self.assertEqual("".join(tokens), "Hello world.")
        self.assertEqual(final["result"], "Hello world.")

    # --- streaming robustness (N6) -------------------------------------------
    def test_final_answer_containing_marker_streams_in_full(self):
        # A real final answer that merely mentions the literal marker (no closing tag).
        deltas = ["Use ", "the <tool", "_call> ", "syntax."]
        full = "".join(deltas)
        bob_core.get_llm_client = lambda config=None: _common.stream_client(deltas)
        events = list(bob_loop.run_agent_events(
            "hi", self.cfg, agency="silent", registry=_common.FakeRegistry(), stream=True))
        tokens = "".join(e["text"] for e in events if e["type"] == "token")
        final = [e for e in events if e["type"] == "final"][-1]
        self.assertEqual(tokens, full)          # nothing swallowed
        self.assertEqual(final["result"], full)  # unpaired literal survives _strip_tool_calls

    def test_real_tool_step_suppresses_markup_but_dispatches(self):
        turns = [
            ['<tool_call>{"name": "echo", "arguments": {"x": "hi"}}</tool_call>'],
            ["All done."],
        ]
        bob_core.get_llm_client = lambda config=None: _common.multi_turn_stream_client(turns)
        reg = _common.FakeRegistry({"echo": "echoed"})
        events = list(bob_loop.run_agent_events(
            "go", self.cfg, agency="silent", registry=reg, stream=True))
        tokens = "".join(e["text"] for e in events if e["type"] == "token")
        self.assertNotIn("<tool_call>", tokens)   # markup suppressed from the stream
        self.assertIn("echo", [e["name"] for e in events if e["type"] == "tool_call"])
        self.assertEqual(events[-1]["result"], "All done.")

    def test_marker_split_across_two_chunks(self):
        turns = [
            ["<tool", '_call>{"name": "echo", "arguments": {}}</tool_call>'],  # split marker
            ["All done."],
        ]
        bob_core.get_llm_client = lambda config=None: _common.multi_turn_stream_client(turns)
        reg = _common.FakeRegistry({"echo": "ok"})
        events = list(bob_loop.run_agent_events(
            "go", self.cfg, agency="silent", registry=reg, stream=True))
        tokens = "".join(e["text"] for e in events if e["type"] == "token")
        self.assertNotIn("<tool", tokens)   # split marker still detected, never leaked
        self.assertIn("echo", [e["name"] for e in events if e["type"] == "tool_call"])
        self.assertEqual(events[-1]["result"], "All done.")

    def test_mid_stream_error_one_error_no_final(self):
        from types import SimpleNamespace

        class _Boom:
            def __iter__(self):
                yield _common._content_chunk("partial")
                raise RuntimeError("stream broke")

            def close(self):
                pass

        class _C:
            def __init__(self):
                self.chat = SimpleNamespace(completions=self)

            def create(self, model, messages, tools, stream, timeout):
                return _Boom()

        bob_core.get_llm_client = lambda config=None: _C()
        events = list(bob_loop.run_agent_events(
            "hi", self.cfg, agency="silent", registry=_common.FakeRegistry(), stream=True))
        self.assertEqual(sum(e["type"] == "error" for e in events), 1)
        self.assertEqual(sum(e["type"] == "final" for e in events), 0)

    def test_preflight_failure_yields_error(self):
        bob_core.check_litellm = lambda config=None: False
        events = list(bob_loop.run_agent_events("x", self.cfg, agency="silent",
                                                registry=_common.FakeRegistry()))
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("not reachable", events[-1]["message"])

    # --- tool-call wire formats (N8) -----------------------------------------
    def test_hermes_tool_response_wire_format(self):
        from types import SimpleNamespace
        turns = ['<tool_call>{"name": "echo", "arguments": {"x": "hi"}}</tool_call>', "done"]
        captured = []
        state = {"i": 0}

        class _C:
            def __init__(self):
                self.chat = SimpleNamespace(completions=self)

            def create(self, model, messages, tools, stream, timeout):
                captured.append([dict(m) for m in messages])
                i = state["i"]
                state["i"] += 1
                return _common._FakeStream([_common._content_chunk(turns[min(i, len(turns) - 1)])])

        bob_core.get_llm_client = lambda config=None: _C()
        reg = _common.FakeRegistry({"echo": "echoed-hi"})
        list(bob_loop.run_agent_events("go", self.cfg, agency="silent", registry=reg))
        second = captured[1]  # messages sent back after the tool ran
        self.assertEqual(second[-2]["role"], "assistant")
        self.assertIn("<tool_call>", second[-2]["content"])          # raw assistant markup kept
        self.assertEqual(second[-1]["role"], "user")
        self.assertIn("<tool_response>", second[-1]["content"])      # hermes wire format
        self.assertIn('"name": "echo"', second[-1]["content"])
        self.assertIn("echoed-hi", second[-1]["content"])

    def test_openai_tool_call_path(self):
        from types import SimpleNamespace
        cfg = _common.fake_config()
        cfg["agent"]["toolFormat"] = "openai"
        captured = []
        state = {"i": 0}

        def tc_chunk():
            d = SimpleNamespace(index=0, id="call_1",
                                function=SimpleNamespace(name="echo", arguments='{"x":"hi"}'))
            return SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content=None, tool_calls=[d]))])

        def final_chunk():
            return SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content="done", tool_calls=None))])

        class _C:
            def __init__(self):
                self.chat = SimpleNamespace(completions=self)

            def create(self, model, messages, tools, stream, timeout):
                captured.append([dict(m) for m in messages])
                i = state["i"]
                state["i"] += 1
                return iter([tc_chunk()]) if i == 0 else iter([final_chunk()])

        bob_core.get_llm_client = lambda config=None: _C()
        reg = _common.FakeRegistry({"echo": "echoed"})
        events = list(bob_loop.run_agent_events("go", cfg, agency="silent", registry=reg))
        types = [e["type"] for e in events]
        self.assertIn("tool_call", types)
        self.assertIn("tool_result", types)
        self.assertEqual(events[-1]["result"], "done")
        second = captured[1]
        self.assertEqual(second[-2]["role"], "assistant")           # OpenAI assistant + tool_calls
        self.assertTrue(second[-2].get("tool_calls"))
        self.assertEqual(second[-1]["role"], "tool")                # tool-role reply
        self.assertEqual(second[-1]["content"], "echoed")

    # --- cancellation (N3) ---------------------------------------------------
    def test_cancel_stops_stream_within_1s(self):
        import time
        from bob_loop import CancelToken
        tok = CancelToken()
        client = _common.slow_stream_client(
            ["a", "b", "c", "d", "e"], sleep_s=0.05,
            on_chunk=lambda i: tok.cancel() if i == 1 else None,
        )
        bob_core.get_llm_client = lambda config=None: client
        t0 = time.monotonic()
        events = list(bob_loop.run_agent_events(
            "hi", self.cfg, agency="silent", registry=_common.FakeRegistry(),
            stream=True, cancel=tok))
        dt = time.monotonic() - t0
        finals = [e for e in events if e["type"] == "final"]
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0]["reason"], "cancelled")
        self.assertLess(dt, 1.0)
        self.assertTrue(client.last_stream.closed)  # abort closed the stream

    def test_cancel_before_next_tool_dispatch(self):
        from bob_loop import CancelToken
        tok = CancelToken()
        calls = []

        class Reg(_common.FakeRegistry):
            def dispatch_call(self, name, args, context=None):
                calls.append(name)
                tok.cancel()  # trip after the first tool
                return "ok"

        turn = ('<tool_call>{"name":"echo","arguments":{}}</tool_call>'
                '<tool_call>{"name":"echo2","arguments":{}}</tool_call>')
        bob_core.get_llm_client = lambda config=None: _common.scripted_client([turn, "done"])
        events = list(bob_loop.run_agent_events(
            "go", self.cfg, agency="silent", registry=Reg(), cancel=tok))
        self.assertEqual(calls, ["echo"])  # second tool skipped after cancel
        self.assertEqual(events[-1]["type"], "final")
        self.assertEqual(events[-1]["reason"], "cancelled")

    def test_cancel_final_strips_hermes_markup(self):
        # N-review: a cancelled run must not return raw <tool_call> markup as the "answer".
        from bob_loop import CancelToken
        tok = CancelToken()

        class Reg(_common.FakeRegistry):
            def dispatch_call(self, name, args, context=None):
                tok.cancel()  # cancel after the first tool so the 2nd is skipped
                return "ok"

        turn = ('<tool_call>{"name":"echo","arguments":{}}</tool_call>'
                '<tool_call>{"name":"echo2","arguments":{}}</tool_call>')
        bob_core.get_llm_client = lambda config=None: _common.scripted_client([turn, "done"])
        events = list(bob_loop.run_agent_events(
            "go", self.cfg, agency="silent", registry=Reg(), cancel=tok))
        final = events[-1]
        self.assertEqual(final["reason"], "cancelled")
        self.assertNotIn("<tool_call>", final["result"] or "")  # markup stripped

    def test_sigint_handler_restored(self):
        import signal
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(["answer"])
        before = signal.getsignal(signal.SIGINT)
        list(bob_loop.run_agent_events("q", self.cfg, agency="silent",
                                       registry=_common.FakeRegistry()))
        self.assertEqual(signal.getsignal(signal.SIGINT), before)

    # --- observability (N5) --------------------------------------------------
    def test_metrics_line_and_rid_propagation(self):
        import logging
        log = logging.getLogger("bob.agent")
        old_handlers, old_prop = log.handlers[:], log.propagate
        records = []

        class Cap(logging.Handler):
            def emit(self, r):
                records.append(r.getMessage())

        log.handlers = [Cap()]          # pre-seeding handlers stops _agent_logger adding a file one
        log.propagate = False
        try:
            bob_core.get_llm_client = lambda config=None: _common.scripted_client(["done"])
            list(bob_loop.run_agent_events(
                "q", self.cfg, agency="silent", registry=_common.FakeRegistry(),
                run_id="abcd1234"))
        finally:
            log.handlers, log.propagate = old_handlers, old_prop
        msgs = "\n".join(records)
        self.assertIn("[abcd1234] start", msgs)              # run id propagated
        self.assertIn("[abcd1234] done steps=", msgs)        # one metrics line
        self.assertIn("tokens~=", msgs)
        self.assertIn("registry_build_ms=", msgs)

    def test_history_is_seeded(self):
        captured = {}

        class Recorder:
            def __init__(self):
                self.chat = type("C", (), {"completions": self})()

            def create(self, model, messages, tools, stream, timeout):
                captured["messages"] = messages
                from types import SimpleNamespace
                chunk = SimpleNamespace(choices=[SimpleNamespace(
                    delta=SimpleNamespace(content="ok", tool_calls=None))])
                return iter([chunk])

        bob_core.get_llm_client = lambda config=None: Recorder()
        history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "reply"}]
        list(bob_loop.run_agent_events("now", self.cfg, agency="silent",
                                       registry=_common.FakeRegistry(), history=history))
        roles = [m["role"] for m in captured["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "user"])
        self.assertEqual(captured["messages"][-1]["content"], "now")


class TestApproval(unittest.TestCase):
    """NE0 — event-driven approval: the loop asks approve() before a gated tool and fails closed
    (deny) when no approver is wired. Replaces the old blocking input() confirm/shell prompts."""

    def setUp(self):
        self.cfg = _common.fake_config()
        self._orig_check = bob_core.check_litellm
        self._orig_client = bob_core.get_llm_client
        bob_core.check_litellm = lambda config=None: True

    def tearDown(self):
        bob_core.check_litellm = self._orig_check
        bob_core.get_llm_client = self._orig_client

    def _registry(self, approval_tools=None):
        calls = []

        class Reg(_common.FakeRegistry):
            def __init__(self):
                super().__init__({"echo": "echoed"})
                self.approval_required_tools = set(approval_tools or [])

            def dispatch_call(self, name, args, context=None):
                calls.append(name)
                return super().dispatch_call(name, args)

        return Reg(), calls

    _TURNS = ['<tool_call>{"name": "echo", "arguments": {"x": "hi"}}</tool_call>', "done"]

    def test_confirm_deny_skips_tool_and_continues(self):
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(self._TURNS)
        reg, calls = self._registry()
        events = list(bob_loop.run_agent_events(
            "go", self.cfg, agency="confirm", registry=reg, approve=lambda a: False))
        self.assertIn("approval_required", [e["type"] for e in events])
        self.assertEqual(calls, [])  # denied → the tool never ran
        tr = next(e for e in events if e["type"] == "tool_result")
        self.assertIn("denied", tr["result"])
        self.assertEqual(events[-1]["reason"], "answer")  # run continued to a final answer
        self.assertEqual(events[-1]["result"], "done")

    def test_confirm_approve_runs_tool(self):
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(self._TURNS)
        reg, calls = self._registry()
        events = list(bob_loop.run_agent_events(
            "go", self.cfg, agency="confirm", registry=reg, approve=lambda a: True))
        self.assertEqual(calls, ["echo"])
        self.assertEqual(next(e for e in events if e["type"] == "tool_result")["result"], "echoed")

    def test_requires_approval_tool_fails_closed_without_approver(self):
        # agency 'silent', but the tool self-declares REQUIRES_APPROVAL → still gated; approve=None → deny
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(self._TURNS)
        reg, calls = self._registry(approval_tools={"echo"})
        events = list(bob_loop.run_agent_events("go", self.cfg, agency="silent", registry=reg))
        self.assertEqual(calls, [])
        self.assertIn("approval_required", [e["type"] for e in events])

    def test_non_gated_tool_runs_without_approval(self):
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(self._TURNS)
        reg, calls = self._registry()
        events = list(bob_loop.run_agent_events("go", self.cfg, agency="silent", registry=reg))
        self.assertEqual(calls, ["echo"])
        self.assertNotIn("approval_required", [e["type"] for e in events])

    def test_call_id_correlates_tool_call_and_result(self):
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(self._TURNS)
        reg, _ = self._registry()
        events = list(bob_loop.run_agent_events("go", self.cfg, agency="silent", registry=reg))
        tc = next(e for e in events if e["type"] == "tool_call")
        tr = next(e for e in events if e["type"] == "tool_result")
        self.assertIn("call_id", tc)
        self.assertEqual(tc["call_id"], tr["call_id"])


class TestCancelTokenLink(unittest.TestCase):
    """NE0/O1 seam — a child token is cancelled when its parent is (sub-agent teardown)."""

    def test_parent_cancel_propagates_to_child(self):
        from bob_loop import CancelToken
        parent = CancelToken()
        child = parent.child()
        self.assertFalse(child.cancelled())
        parent.cancel()
        self.assertTrue(child.cancelled())      # parent cancel reaches the child
        self.assertTrue(parent.cancelled())

    def test_child_cancel_does_not_affect_parent(self):
        from bob_loop import CancelToken
        parent = CancelToken()
        child = parent.child()
        child.cancel()
        self.assertTrue(child.cancelled())
        self.assertFalse(parent.cancelled())    # child cancel is local


class TestProfileInjection(unittest.TestCase):
    """MEM-3 — the once-per-session profile block is injected into the system prompt only when the
    session is starting (history empty), not on continuation turns."""

    def setUp(self):
        self.cfg = _common.fake_config()
        self.cfg["memory"] = {"enabled": True, "injectProfileAtStart": True}
        self._orig_check = bob_core.check_litellm
        self._orig_client = bob_core.get_llm_client
        self._orig_pb = bob_core.memory_profile_block
        bob_core.check_litellm = lambda config=None: True
        # Stub the block so the test doesn't touch the DB/embed server — the loop's history gate is
        # what's under test, not the block's content.
        bob_core.memory_profile_block = lambda owner=None, config=None: "PROFILE_SENTINEL"

    def tearDown(self):
        bob_core.check_litellm = self._orig_check
        bob_core.get_llm_client = self._orig_client
        bob_core.memory_profile_block = self._orig_pb

    def _system_prompt_for(self, history, scope=None):
        captured = {}

        class _C:
            def __init__(self):
                self.chat = SimpleNamespace(completions=self)

            def create(self, model, messages, tools, stream, timeout):
                captured.setdefault("messages", [dict(m) for m in messages])
                return _common._FakeStream([_common._content_chunk("done")])

        bob_core.get_llm_client = lambda config=None: _C()
        list(bob_loop.run_agent_events("hi", self.cfg, agency="silent",
                                       registry=_common.FakeRegistry(), history=history, scope=scope))
        return captured["messages"][0]["content"]

    def test_injected_on_empty_history(self):
        self.assertIn("PROFILE_SENTINEL", self._system_prompt_for(None))

    def test_not_injected_when_history_present(self):
        history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "ok"}]
        self.assertNotIn("PROFILE_SENTINEL", self._system_prompt_for(history))

    def test_project_memory_injected_only_when_scope_set(self):
        # MEM-7b: BOB.md block injects at session start when a project scope is present, not otherwise.
        orig = bob_core.project_memory_block
        bob_core.project_memory_block = lambda project_dir, config=None: (
            "BOBMD_SENTINEL" if project_dir else None)
        try:
            self.assertIn("BOBMD_SENTINEL", self._system_prompt_for(None, scope="/repoA"))
            self.assertNotIn("BOBMD_SENTINEL", self._system_prompt_for(None, scope=None))
        finally:
            bob_core.project_memory_block = orig

    def test_autorecall_uses_run_owner_and_scope(self):
        # A1 regression (MEM-7): autoRecall must recall as the run's owner/scope, not the 'local'
        # default, and only after owner is resolved.
        import bob_core
        self.cfg["memory"] = {"enabled": True, "autoRecall": True, "injectProfileAtStart": False}
        captured = {}
        orig = bob_core.memory_recall
        bob_core.memory_recall = (lambda query, k=5, config=None, owner=None, scope=None:
                                  captured.update(owner=owner, scope=scope) or "(no results)")

        class _C:
            def __init__(self):
                self.chat = SimpleNamespace(completions=self)

            def create(self, model, messages, tools, stream, timeout):
                return _common._FakeStream([_common._content_chunk("done")])

        bob_core.get_llm_client = lambda config=None: _C()
        try:
            list(bob_loop.run_agent_events("hi", self.cfg, agency="silent",
                                           registry=_common.FakeRegistry(), owner="alice", scope="/repoA"))
        finally:
            bob_core.memory_recall = orig
        self.assertEqual(captured.get("owner"), "alice")
        self.assertEqual(captured.get("scope"), "/repoA")

    def test_not_injected_into_subagent_depth(self):
        # MEM-6 gate: an O1 sub-agent starts with empty isolated history but must NOT get the profile.
        captured = {}

        class _C:
            def __init__(self):
                self.chat = SimpleNamespace(completions=self)

            def create(self, model, messages, tools, stream, timeout):
                captured.setdefault("messages", [dict(m) for m in messages])
                return _common._FakeStream([_common._content_chunk("done")])

        bob_core.get_llm_client = lambda config=None: _C()
        list(bob_loop.run_agent_events("hi", self.cfg, agency="silent",
                                       registry=_common.FakeRegistry(), history=None, agent_depth=1))
        self.assertNotIn("PROFILE_SENTINEL", captured["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main()
