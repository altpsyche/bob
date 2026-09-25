"""Context budgets, output reservation, truncation, usage accounting, role/vision fallbacks and the
shared gates the front doors dispatch through. Fake clients and registries; no model, no network."""
import json
import unittest
from types import SimpleNamespace
from unittest import mock

import _common
import bob_core
import bob_loop


def _chunk(text=None, finish=None, reasoning=None, usage=None, empty=False):
    if empty:
        return SimpleNamespace(choices=[], usage=usage)
    delta = SimpleNamespace(content=text, tool_calls=None, reasoning_content=reasoning)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)], usage=usage)


class _Stream(list):
    def close(self):
        pass


def _client(turns, calls):
    """Each create() records its kwargs and streams the next turn (a list of chunks)."""
    state = {"i": 0}

    class _C:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)

        def create(self, **kw):
            calls.append(kw)
            i = min(state["i"], len(turns) - 1)
            state["i"] += 1
            return _Stream(turns[i])

    return _C()


class _Base(_common.LLMStubMixin, unittest.TestCase):
    def cfg(self, **agent):
        cfg = _common.fake_config()
        cfg["agent"].update(agent)
        return cfg

    def run_events(self, turns, cfg=None, window=40960, **kw):
        calls = []
        self.stub_llm(_client(turns, calls))
        with mock.patch.object(bob_core, "role_window", return_value=window):
            evs = list(bob_loop.run_agent_events("do the task", cfg or self.cfg(), agency="silent",
                                                 registry=kw.pop("registry", _common.FakeRegistry()), **kw))
        return evs, calls


class TestContextBudget(_Base):
    def test_auto_budget_follows_the_role_window(self):
        history = [{"role": "user" if i % 2 else "assistant", "content": "h" * 4000} for i in range(20)]
        evs, calls = self.run_events([[_chunk("ok", "stop")]], window=4096, history=history)
        sent = calls[0]["messages"]
        total = sum(bob_loop._message_tokens(m) for m in sent)
        self.assertLessEqual(total + calls[0]["max_tokens"], 4096)
        self.assertEqual(sent[-1]["content"], "do the task")        # the goal is always sent

    def test_explicit_budget_is_capped_at_the_window(self):
        with mock.patch.object(bob_core, "role_window", return_value=4096):
            total, out, send = bob_loop._context_budget({}, "chat", 100000, 1024, 100)
        self.assertEqual((total, out, send), (4096, 1024, 4096 - 1024 - 100))
        with mock.patch.object(bob_core, "role_window", return_value=4096):
            self.assertEqual(bob_loop._context_budget({}, "chat", 2000, 1024, 0)[0], 2000)
            self.assertEqual(bob_loop._context_budget({}, "chat", "auto", 1024, 0)[0], 4096)

    def test_output_reserve_clamped_to_the_window(self):
        _evs, calls = self.run_events([[_chunk("ok", "stop")]], cfg=self.cfg(outputReserveTokens=4000),
                                      window=2048)
        self.assertEqual(calls[0]["max_tokens"], 1024)

    def test_image_block_costs_a_flat_estimate(self):
        huge = "data:image/png;base64," + "A" * 400000
        m = {"role": "user", "content": [{"type": "text", "text": "look"},
                                         {"type": "image_url", "image_url": {"url": huge}}]}
        self.assertLess(bob_loop._message_tokens(m), 2000)

    def test_summarize_keep_last_never_exceeds_the_budget(self):
        msgs = [{"role": "system", "content": "S"}] + [
            {"role": "user" if i % 2 else "assistant", "content": f"t{i} " + "x" * 400} for i in range(20)]
        with mock.patch.object(bob_loop, "_compact_span", lambda d, m, t: "NOTE"):
            out = bob_loop.truncate_history(msgs, 100, 600, compaction="summarize", keep_last=10,
                                            summary_max_tokens=50)
        self.assertLessEqual(sum(bob_loop._message_tokens(m) for m in out), 600)

    def test_compaction_goes_through_complete(self):
        seen = {}

        def fake_complete(cfg, role, messages, max_out, **kw):
            seen.update(role=role, max_out=max_out)
            return "summary", "stop"

        with mock.patch.object(bob_core, "complete", fake_complete):
            note = bob_loop._compact_span([{"role": "user", "content": "a"}], "chat", 128, config={})
        self.assertEqual(note, "summary")
        self.assertEqual(seen, {"role": "chat", "max_out": 128})

    def test_compaction_failure_is_logged_not_silent(self):
        def boom(*a, **k):
            raise bob_core.CompletionError("down")
        with mock.patch.object(bob_core, "complete", boom), \
                self.assertLogs("bob.agent", level="WARNING"):
            self.assertEqual(bob_loop._compact_span([{"role": "user", "content": "a"}], "chat", 8,
                                                    config={}), "")


def _schema(name, words=60):
    return {"type": "function", "function": {
        "name": name, "description": ("does a thing " * words).strip(),
        "parameters": {"type": "object", "properties": {
            f"arg{i}": {"type": "string", "description": "an argument " * 10} for i in range(4)},
            "required": ["arg0"]}}}


# A tool set like the full registry's: ~50 tools, core ones among them.
_CORE = ["file_read", "file_write", "file_edit", "shell_run", "memory_store", "memory_recall", "web_search",
         "web_fetch", "todo_write"]
_MANY = _CORE + [f"plugin_{i}" for i in range(42)]


def _reg(names=_MANY):
    reg = _common.FakeRegistry()
    reg.tool_schemas = [_schema(n) for n in names]
    return reg


def _prompt_tokens(call):
    total = sum(bob_loop._message_tokens(m) for m in call["messages"])
    return total + bob_loop._tools_payload_tokens(call.get("tools"))


class TestOutputPerRole(_Base):
    """A pro role asks for its peer's maxOutputTokens (what gen-litellm writes as its max_tokens), clamped to
    half its window; a local role keeps agent.outputReserveTokens."""

    PEERS = {"d": {"contextWindow": 1000000, "maxOutputTokens": 32768,
                   "pro": {"chat": {"model": "flash"}, "ponder": {"model": "pro", "maxOutputTokens": 65536}}}}

    def _view(self):
        return mock.patch.object(bob_core, "_models_view", return_value=(
            {"defaults": {}, "peers": self.PEERS}, "16gb", {"chat": {"ctx": 40960}}))

    def test_role_output_tokens(self):
        with self._view():
            self.assertEqual(bob_core.role_output_tokens(self.cfg(), "chat-pro"), 32768)
            self.assertEqual(bob_core.role_output_tokens(self.cfg(), "ponder-pro"), 65536)   # role wins
            self.assertEqual(bob_core.role_output_tokens(self.cfg(), "chat"), 1024)
            self.assertEqual(bob_core.role_output_tokens(self.cfg(), "writer-pro"), 1024)    # no peer

    def test_a_pro_run_sends_the_peer_cap(self):
        with self._view():
            _evs, calls = self.run_events([[_chunk("ok", "stop")]], window=1000000, role="ponder-pro")
        self.assertEqual(calls[0]["max_tokens"], 65536)

    def test_a_pro_cap_is_clamped_to_half_its_window(self):
        with self._view():
            _evs, calls = self.run_events([[_chunk("ok", "stop")]], window=65536, role="ponder-pro")
        self.assertEqual(calls[0]["max_tokens"], 32768)

    def test_a_local_run_keeps_the_reserve(self):
        with self._view():
            _evs, calls = self.run_events([[_chunk("ok", "stop")]], window=40960, role="chat")
        self.assertEqual(calls[0]["max_tokens"], 1024)


class TestCompleteWindow(unittest.TestCase):
    def _complete(self, cfg, window, max_out):
        seen = {}

        class _C:
            def __init__(self):
                self.chat = SimpleNamespace(completions=self)

            def create(self, **kw):
                seen.update(kw)
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="x"),
                                                                finish_reason="stop")])

        with mock.patch.object(bob_core, "role_window", return_value=window), \
                mock.patch.object(bob_core, "get_llm_client", return_value=_C()), \
                mock.patch.object(bob_core, "served_role", side_effect=lambda c, r, **k: r):
            bob_core.complete(cfg, "chat", [{"role": "user", "content": "hi"}], max_out)
        return seen["max_tokens"]

    def test_complete_honours_max_context_tokens(self):
        self.assertEqual(self._complete({"agent": {"maxContextTokens": 2048}}, 40960, 4000), 1024)
        self.assertEqual(self._complete({"agent": {"maxContextTokens": 0}}, 40960, 4000), 4000)

    def test_one_half_window_clamp(self):
        self.assertEqual(bob_core.cap_output(4000, 4096), 2048)
        self.assertEqual(bob_core.cap_output(4000, 0), 4000)
        self.assertEqual(bob_loop._context_budget({}, "x", 0, 4000, 0)[1], bob_core.cap_output(4000, 8192))


class TestSmallWindow(_Base):
    """A 4096-token window (the cpu profile, 16gb's vision model) with the full tool set: the request never
    asks for more than the window holds, schemas are compacted and non-core tools dropped with a notice."""

    def _check(self, cfg):
        reg = _reg()
        evs, calls = self.run_events([[_chunk("ok", "stop")]], cfg=cfg, window=4096, registry=reg)
        self.assertEqual(evs[-1]["type"], "final", evs[-1])
        call = calls[0]
        self.assertLessEqual(_prompt_tokens(call) + call["max_tokens"], 4096)
        self.assertGreaterEqual(call["max_tokens"], bob_loop._MIN_OUTPUT_TOKENS)
        return evs, call

    def test_hermes(self):
        evs, call = self._check(self.cfg())
        system = call["messages"][0]["content"]
        for name in _CORE:
            self.assertIn(f'"{name}"', system)
        notices = [e["message"] for e in evs if e["type"] == "notice"]
        self.assertEqual(len(notices), 1)
        self.assertIn("agent.disabledTools", notices[0])

    def test_constrained_tool_calls(self):
        self._check(self.cfg(constrainedToolCalls=True))

    def test_openai_tool_mode(self):
        self._check(self.cfg(toolFormat="openai"))

    def test_a_large_window_keeps_every_tool(self):
        evs, calls = self.run_events([[_chunk("ok", "stop")]], window=40960, registry=_reg())
        self.assertFalse([e for e in evs if e["type"] == "notice"])
        self.assertIn('"plugin_41"', calls[0]["messages"][0]["content"])

    def test_a_head_that_cannot_fit_errors_with_the_real_advice(self):
        cfg = self.cfg()
        cfg["persona"] = {"systemPrompt": "You are Bob. " + "Be thorough. " * 1500}
        evs, calls = self.run_events([[_chunk("ok", "stop")]], cfg=cfg, window=4096, registry=_reg(_CORE))
        self.assertEqual(calls, [])                  # nothing was sent that could overflow
        self.assertEqual(evs[-1]["kind"], "context_overflow")
        self.assertIn("agent.disabledTools", evs[-1]["message"])
        self.assertNotIn("outputReserveTokens", evs[-1]["message"])

    def _vision_switch(self, cfg, names=_MANY, images=1):
        """A tool returns image(s) mid-run on a 40960-token agent role; the vision role's window is 4096."""
        img = "data:image/png;base64,iVBORw0KGgo="
        reg = _reg(names)
        reg._results = {"file_read": json.dumps({"__images__": [img] * images, "text": "a screenshot"})}
        turns = [[_chunk('<tool_call>{"name": "file_read", "arguments": {}}</tool_call>', "stop")],
                 [_chunk("I see a cat.", "stop")]]
        calls = []
        self.stub_llm(_client(turns, calls))
        window = lambda c, role: 4096 if role == "vision" else 40960   # noqa: E731
        with mock.patch.object(bob_core, "role_window", side_effect=window):
            evs = list(bob_loop.run_agent_events("look", cfg, agency="silent", registry=reg))
        return evs, calls

    def test_a_vision_switch_refits_the_tools_to_the_vision_window(self):
        evs, calls = self._vision_switch(self.cfg())
        vision = calls[1]
        self.assertEqual(vision["model"], "vision")
        kept = vision["messages"][0]["content"].count('"name": "plugin_')
        self.assertLess(kept, len(_MANY) - len(_CORE))            # refitted to the 4096 window
        self.assertIn('"name": "file_read"', vision["messages"][0]["content"])
        self.assertTrue(any(b.get("type") == "image_url" for b in vision["messages"][-1]["content"]))
        self.assertLessEqual(_prompt_tokens(vision) + vision["max_tokens"], 4096)
        self.assertGreaterEqual(vision["max_tokens"], bob_loop._MIN_OUTPUT_TOKENS)
        self.assertTrue([e for e in evs if e["type"] == "notice"])
        self.assertEqual(evs[-1]["result"], "I see a cat.")

    def test_images_the_vision_window_cannot_hold_are_refused_not_sent(self):
        # Three images at the flat per-image cost overflow the 4096 window even with only the core tools.
        evs, calls = self._vision_switch(self.cfg(), names=_CORE, images=3)
        second = calls[1]
        self.assertNotEqual(second["model"], "vision")          # stays on the original role
        wire = json.dumps(second["messages"])
        self.assertNotIn("image_url", wire)
        self.assertIn("which were not shown", wire)
        self.assertGreaterEqual(second["max_tokens"], bob_loop._MIN_OUTPUT_TOKENS)
        self.assertEqual(evs[-1]["result"], "I see a cat.")

    def test_truncation_advice_when_the_window_capped_the_output(self):
        cfg = self.cfg()
        cfg["persona"] = {"systemPrompt": "You are Bob. " + "Be thorough. " * 700}   # leaves ~500 for output
        evs, calls = self.run_events([[_chunk("partial", "length")]], cfg=cfg, window=4096, registry=_reg(_CORE))
        self.assertLess(calls[0]["max_tokens"], 1024)
        self.assertEqual(evs[-1]["reason"], "truncated")
        self.assertIn("agent.disabledTools", evs[-1]["result"])
        self.assertIn("agent.disabledTools", evs[-1]["hint"])


class TestTruncationAndUsage(_Base):
    def test_truncated_text_is_marked(self):
        evs, _ = self.run_events([[_chunk("partial answer", "length")]])
        final = evs[-1]
        self.assertEqual(final["reason"], "truncated")
        self.assertTrue(final["result"].startswith("partial answer"))
        self.assertIn("truncated", final["result"])

    def test_truncated_tool_call_is_neither_run_nor_leaked(self):
        reg = _common.FakeRegistry()
        cut = '<tool_call>{"name": "file_read", "arguments": {"path": "/et'
        evs, _ = self.run_events([[_chunk(cut, "length")]], registry=reg, stream=True)
        self.assertEqual(reg.dispatched, [])
        self.assertEqual(evs[-1]["type"], "error")
        self.assertEqual(evs[-1]["kind"], "truncated")
        leaked = "".join(e["text"] for e in evs if e["type"] == "token")
        self.assertNotIn("<tool_call>", leaked)

    def test_reasoning_ate_the_budget_says_why(self):
        evs, _ = self.run_events([[_chunk(None, None, reasoning="thinking " * 50), _chunk(None, "length")]])
        self.assertEqual(evs[-1]["type"], "error")
        self.assertIn("reasoning", evs[-1]["message"])

    def test_usage_summed_from_the_backend(self):
        u = SimpleNamespace(prompt_tokens=700, completion_tokens=30)
        evs, calls = self.run_events([[_chunk("ok", "stop"), _chunk(empty=True, usage=u)]])
        self.assertEqual(evs[-1]["usage"]["total_tokens"], 730)
        self.assertFalse(evs[-1]["usage"]["estimated"])
        self.assertEqual(calls[0]["stream_options"], {"include_usage": True})

    def test_usage_estimated_when_unreported(self):
        evs, _ = self.run_events([[_chunk("ok", "stop")]])
        self.assertTrue(evs[-1]["usage"]["estimated"])
        self.assertGreater(evs[-1]["usage"]["total_tokens"], 0)

    def test_failed_verify_is_not_acceptance(self):
        state = {"n": 0}
        calls = []
        good = _client([[_chunk("answer", "stop")]], calls)

        class _C:
            chat = SimpleNamespace(completions=SimpleNamespace())

        def create(**kw):
            state["n"] += 1
            if state["n"] == 2:
                raise RuntimeError("verifier down")
            return good.create(**kw)
        _C.chat.completions.create = create
        self.stub_llm(_C())
        evs = list(bob_loop.run_agent_events("g", self.cfg(verify=True), agency="silent",
                                             registry=_common.FakeRegistry()))
        self.assertTrue(any(e["type"] == "notice" and "verify" in e["message"] for e in evs))
        self.assertEqual(evs[-1]["verify"], "failed")

    def test_session_id_reaches_the_transcript(self):
        import bob_memory
        seen = []
        with mock.patch.object(bob_memory, "transcript_append",
                               side_effect=lambda *a, **k: seen.append(k.get("session_id")) or 1):
            self.run_events([[_chunk("ok", "stop")]], cfg=self.cfg(conversationPaging=True),
                            session_id="sess-1")
        self.assertTrue(seen)
        self.assertEqual(set(seen), {"sess-1"})


class TestRoutingFallbacks(_Base):
    def test_role_not_on_profile_falls_back_with_notice(self):
        view = ({"defaults": {}, "peers": {}}, "cpu", {"chat": {"ctx": 4096}, "agent": {"ctx": 4096}})
        calls = []
        self.stub_llm(_client([[_chunk("ok", "stop")]], calls))
        with mock.patch.object(bob_core, "_models_view", return_value=view):
            evs = list(bob_loop.run_agent_events("q", self.cfg(), role="coder", agency="silent",
                                                 registry=_common.FakeRegistry()))
        self.assertEqual(calls[0]["model"], "chat")
        self.assertTrue(any(e["type"] == "notice" for e in evs))

    def test_image_on_profile_without_vision_is_refused(self):
        view = ({"defaults": {}, "peers": {}}, "8gb", {"chat": {"ctx": 4096}})
        calls = []
        self.stub_llm(_client([[_chunk("ok", "stop")]], calls))
        with mock.patch.object(bob_core, "_models_view", return_value=view):
            evs = list(bob_loop.run_agent_events("q", self.cfg(), agency="silent", images=["data:image/png;base64,AA"],
                                                 registry=_common.FakeRegistry()))
        self.assertEqual(evs[-1]["kind"], "vision_unavailable")
        self.assertEqual(calls, [])


class TestFoldEvents(unittest.TestCase):
    def test_folds_final_error_usage_and_tools(self):
        out = bob_loop.fold_events(iter([
            {"type": "tool_call", "name": "a"}, {"type": "tool_call", "name": "b"},
            {"type": "final", "result": "r", "reason": "answer", "usage": {"total_tokens": 5}}]))
        self.assertEqual((out.result, out.reason, out.steps, out.tools_used), ("r", "answer", 2, ["a", "b"]))
        self.assertEqual(out.usage["total_tokens"], 5)
        err = bob_loop.fold_events(iter([{"type": "error", "message": "m", "kind": "upstream_error"}]))
        self.assertEqual((err.error, err.error_kind, err.result), ("m", "upstream_error", None))

    def test_run_agent_can_raise_distinct_errors(self):
        with _common.stubbed_llm(up=False):
            with self.assertRaises(bob_loop.AgentRunError) as ctx:
                bob_loop.run_agent("q", _common.fake_config(), quiet=True, raise_on_error=True,
                                   registry=_common.FakeRegistry())
        self.assertEqual(ctx.exception.kind, "upstream_unreachable")


class TestFrontDoorGates(unittest.TestCase):
    def test_run_invoker_denies_approval_tool_when_piped(self):
        from bob_permissions import run_gated
        reg = _common.FakeRegistry(approval_required_tools={"shell_run"})
        out = run_gated(reg, "shell_run", "{}", config=_common.fake_config(), approve=None)
        self.assertIn("did not run", out)
        self.assertEqual(reg.dispatched, [])

    def test_steps_skill_goes_through_the_gate(self):
        from bob_skills import SkillRegistry
        reg = _common.FakeRegistry(approval_required_tools={"shell_run"})
        skills = SkillRegistry()
        skills.skills["s"] = {"name": "s", "description": "d", "group": "g", "argument_hint": "",
                              "steps": [{"tool": "shell_run", "arguments": {"cmd": "ls"}},
                                        {"tool": "file_read", "arguments": {}}], "dir": "."}
        out = skills.run("s", reg, config=_common.fake_config())
        self.assertIn("did not run", out)
        self.assertEqual(reg.dispatched, ["file_read"])
        self.assertIn("[file_read ran]", skills.run("s", reg, config=_common.fake_config(),
                                                    approve=lambda a: True))
        self.assertIn("shell_run", reg.dispatched)

    def test_policy_applies_to_skill_steps(self):
        from bob_skills import SkillRegistry
        cfg = _common.fake_config()
        cfg["agent"]["permissions"] = {"tools": {"file_read": "deny"}}
        reg = _common.FakeRegistry()
        skills = SkillRegistry()
        skills.skills["s"] = {"name": "s", "description": "d", "group": "g", "argument_hint": "",
                              "steps": [{"tool": "file_read", "arguments": {}}], "dir": "."}
        self.assertIn("denied by policy", skills.run("s", reg, config=cfg))
        self.assertEqual(reg.dispatched, [])


class TestPluginCli(unittest.TestCase):
    def test_unregistered_verb_runs_the_plugin_main(self):
        from bob import cli
        with mock.patch.object(cli, "_run_plugin", return_value=7) as rp:
            self.assertEqual(cli.main(["summarise", "--length", "short"]), 7)
        rp.assert_called_once_with("summarise", ["--length", "short"])

    def test_path_like_verb_is_not_a_plugin(self):
        from bob import cli
        self.assertFalse(cli._plugin_invoke("../scripts").exists())

    def test_chat_known_roles_include_writer_agent_vision(self):
        from bob import cli
        roles = cli._chat_known_roles(_common.fake_config())
        for r in ("writer", "agent", "vision", "chat-pro", "writer-pro"):
            self.assertIn(r, roles)

    def test_memory_explicit_db_passes_through(self):
        from bob import cli
        import bob_memory
        seen = {}
        with mock.patch.object(bob_memory, "main", side_effect=lambda: seen.update(argv=list(__import__("sys").argv))):
            cli._handle_memory(["--db", "/tmp/x.db", "status"])
        self.assertEqual(seen["argv"][1:4], ["--db", "/tmp/x.db", "status"])


if __name__ == "__main__":
    unittest.main()
