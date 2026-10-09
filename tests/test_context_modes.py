"""Context modes: Quick/Deep policies resolve separately for local and API roles."""
import json
import os
import unittest
from unittest import mock

import _common  # noqa: F401  (adds scripts/tools to sys.path)
import bob_context
import bob_core
import bob_loop
import bob_context_callback


def _config(**agent):
    cfg = _common.fake_config()
    cfg["agent"].update({
        "contextMode": "quick",
        "contextModes": {
            "quick": {
                "label": "Quick",
                "local": {
                    "maxContextTokens": 16384, "maxHistoryMsgs": 40,
                    "outputReserveTokens": 512, "maxToolResultTokens": 600,
                    "memoryMaxInjectedTokens": 800, "compaction": "truncate",
                    "stablePrefix": False, "compactSchemasAfter": 8,
                },
                "api": {
                    "maxContextTokens": 65536, "maxHistoryMsgs": 80,
                    "outputReserveTokens": 4096, "maxToolResultTokens": 2000,
                    "memoryMaxInjectedTokens": 2000, "compaction": "truncate",
                    "stablePrefix": False, "compactSchemasAfter": 12,
                },
            },
            "deep": {
                "label": "Deep",
                "local": {
                    "maxContextTokens": 0, "maxHistoryMsgs": 200,
                    "outputReserveTokens": 2048, "maxToolResultTokens": 4000,
                    "memoryMaxInjectedTokens": 4000, "compaction": "summarize",
                    "stablePrefix": True, "compactSchemasAfter": 24,
                },
                "api": {
                    "maxContextTokens": 200000, "maxHistoryMsgs": 500,
                    "outputReserveTokens": 0, "maxToolResultTokens": 16000,
                    "memoryMaxInjectedTokens": 12000, "compaction": "summarize",
                    "stablePrefix": True, "compactSchemasAfter": 0,
                },
            },
        },
    })
    cfg["agent"].update(agent)
    return cfg


# Registry view with a local chat role and an enabled DeepSeek-style pro peer.
_VIEW = ({
    "defaults": {"parallel": 1},
    "peers": {"deepseek": {
        "enabled": True, "contextWindow": 1000000, "maxOutputTokens": 32768,
        "pro": {"chat": {"model": "deepseek-v4-pro"}},
    }},
}, "16gb", {"chat": {"ctx": 40960}, "vision": {"ctx": 4096}, "agent": {"ctx": 40960}})


class TestEstimatorGuarantees(unittest.TestCase):
    def test_clip_never_exceeds_the_budget(self):
        import bob_core
        text = "你好世界。这是一段中文文本，用于测试分词器和上下文预算。" * 30
        for budget in (5, 10, 20, 50, 100):
            self.assertLessEqual(bob_core.est_tokens(
                bob_core.clip_text_to_tokens(text, budget)), budget)

    def test_dense_content_gets_a_safety_margin(self):
        import bob_core
        uuid_blob = "a3f1c2d4-5e6b-7c8d-9e0f-1234567890ab " * 100
        self.assertGreaterEqual(bob_core.est_tokens(uuid_blob), 3501)


class TestResolve(unittest.TestCase):
    def setUp(self):
        self._view = mock.patch.object(bob_core, "_models_view", return_value=_VIEW)
        self._view.start()
        self.addCleanup(self._view.stop)

    def test_local_and_api_windows_use_separate_mode_blocks(self):
        cfg = _config()
        local = bob_context.resolve(cfg, "chat", "quick")
        self.assertEqual((local.backend, local.window(cfg, "chat")), ("local", 16384))
        wide_local = bob_context.resolve(cfg, "chat", "deep")
        self.assertEqual((wide_local.backend, wide_local.window(cfg, "chat")), ("local", 40960))
        api = bob_context.resolve(cfg, "chat-pro", "quick")
        self.assertEqual((api.backend, api.window(cfg, "chat-pro")), ("api", 65536))
        wide_api = bob_context.resolve(cfg, "chat-pro", "deep")
        self.assertEqual((wide_api.backend, wide_api.window(cfg, "chat-pro")), ("api", 200000))

    def test_api_never_inherits_the_local_cap(self):
        cfg = _config()
        with mock.patch.object(bob_core, "role_window", return_value=1_000_000):
            pol = bob_context.resolve(cfg, "chat-pro", "quick")
        self.assertEqual(pol.max_context_tokens, 65536)
        self.assertEqual(pol.window(cfg, "chat-pro"), 65536)

    def test_local_never_escapes_its_loaded_window(self):
        cfg = _config()
        pol = bob_context.resolve(cfg, "chat", "deep")   # deep local cap is 0 == full role window
        self.assertEqual(pol.window(cfg, "chat"), 40960)
        self.assertLessEqual(pol.window(cfg, "chat"), 40960)

    def test_output_caps_follow_backend(self):
        cfg = _config()
        self.assertEqual(bob_context.resolve(cfg, "chat", "quick").output_tokens(cfg, "chat"), 512)
        self.assertEqual(bob_context.resolve(cfg, "chat", "deep").output_tokens(cfg, "chat"), 2048)
        self.assertEqual(bob_context.resolve(cfg, "chat-pro", "quick").output_tokens(cfg, "chat-pro"), 4096)
        self.assertEqual(bob_context.resolve(cfg, "chat-pro", "deep").output_tokens(cfg, "chat-pro"), 32768)

    def test_mode_aliases(self):
        cfg = _config()
        self.assertEqual(bob_context.normalize_mode("fast", cfg), "quick")
        self.assertEqual(bob_context.normalize_mode("slow", cfg), "deep")
        self.assertEqual(bob_context.normalize_mode("lean", cfg), "quick")
        self.assertEqual(bob_context.normalize_mode("wide", cfg), "deep")

    def test_unknown_mode_is_loud(self):
        with self.assertRaises(bob_context.ContextModeError):
            bob_context.normalize_mode("turbo", _config())


class TestAliasGrammar(unittest.TestCase):
    def setUp(self):
        self._view = mock.patch.object(bob_core, "_models_view", return_value=_VIEW)
        self._view.start()
        self.addCleanup(self._view.stop)

    def test_parse_alias(self):
        self.assertEqual(bob_context.parse_model_alias("chat"), ("chat", None))
        self.assertEqual(bob_context.parse_model_alias("chat-quick"), ("chat", "quick"))
        self.assertEqual(bob_context.parse_model_alias("chat-pro-deep"), ("chat-pro", "deep"))

    def test_wire_names_and_windows(self):
        cfg = _config()
        self.assertEqual(bob_context.wire_model_names("chat"),
                         ["chat", "chat-quick", "chat-deep"])
        local = dict(bob_context.wire_model_variants("chat", 40960, "local", cfg))
        self.assertEqual(local, {"chat": 40960, "chat-quick": 16384, "chat-deep": 40960})
        api = dict(bob_context.wire_model_variants("chat-pro", 1000000, "api", cfg))
        self.assertEqual(api, {"chat-pro": 1000000, "chat-pro-quick": 65536,
                               "chat-pro-deep": 200000})


class TestApplyOpenAIRequest(unittest.TestCase):
    def setUp(self):
        self._view = mock.patch.object(bob_core, "_models_view", return_value=_VIEW)
        self._view.start()
        self.addCleanup(self._view.stop)

    def test_base_model_is_unchanged(self):
        body = {"model": "chat", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 99}
        self.assertEqual(bob_context.apply_openai_request(_config(), "chat", body), body)

    def _prompt(self, msgs):
        return sum(bob_core.message_tokens(m, pad=False) for m in msgs)

    def test_quick_alias_trims_history_and_clamps_output_to_the_window(self):
        cfg = _config()
        msgs = [{"role": "system", "content": "sys"}]
        for i in range(40):
            msgs.append({"role": "user" if i % 2 == 0 else "assistant", "content": "h" * 4000})
        body = {"model": "chat-quick", "messages": msgs, "max_tokens": 9999}
        out = bob_context.apply_openai_request(cfg, "chat-quick", body)
        self.assertLess(len(out["messages"]), len(msgs))
        self.assertEqual(out["messages"][-1], msgs[-1])
        # The client's ask is lowered only to what the 16384 window leaves after the prompt.
        self.assertLess(out["max_tokens"], 9999)
        self.assertLessEqual(self._prompt(out["messages"]) + out["max_tokens"], 16384)
        self.assertGreaterEqual(out["max_tokens"], 512)

    def test_a_max_tokens_that_fits_is_left_alone(self):
        body = {"model": "chat-quick", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 9999}
        out = bob_context.apply_openai_request(_config(), "chat-quick", body)
        self.assertEqual(out["max_tokens"], 9999)

    def test_no_max_tokens_stays_unset(self):
        body = {"model": "chat-quick", "messages": [{"role": "user", "content": "hi"}]}
        out = bob_context.apply_openai_request(_config(), "chat-quick", body)
        self.assertNotIn("max_tokens", out)
        self.assertNotIn("max_completion_tokens", out)

    def test_the_client_field_is_kept(self):
        body = {"model": "chat-pro-deep", "messages": [{"role": "user", "content": "hi"}],
                "max_completion_tokens": 999999}
        out = bob_context.apply_openai_request(_config(), "chat-pro-deep", body)
        self.assertNotIn("max_tokens", out)
        self.assertEqual(out["max_completion_tokens"], 32768)   # the peer's real maximum

    def test_api_deep_alias_uses_peer_output_cap(self):
        cfg = _config()
        body = {"model": "chat-pro-deep", "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 999999}
        out = bob_context.apply_openai_request(cfg, "chat-pro-deep", body)
        self.assertEqual(out["max_tokens"], 32768)

    def _tool_turns(self, n, args_chars=0, result_chars=6000):
        msgs = [{"role": "system", "content": "You are a coding agent."},
                {"role": "user", "content": "fix the bug"}]
        for i in range(n):
            msgs.append({"role": "assistant", "content": None, "tool_calls": [{
                "id": f"c{i}", "type": "function",
                "function": {"name": "write_file",
                             "arguments": json.dumps({"path": f"f{i}.py", "content": "x = 1\n" * (args_chars // 6)})}}]})
            msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "r" * result_chars})
        return msgs

    def _assert_valid(self, msgs):
        self.assertTrue(any(m["role"] == "user" for m in msgs))
        open_ids = set()
        for m in msgs:
            if m.get("tool_calls"):
                open_ids = {tc["id"] for tc in m["tool_calls"]}
            elif m["role"] == "tool":
                self.assertIn(m["tool_call_id"], open_ids, "orphan tool result")
        called = {tc["id"] for m in msgs for tc in (m.get("tool_calls") or [])}
        answered = {m["tool_call_id"] for m in msgs if m["role"] == "tool"}
        self.assertEqual(called, answered, "tool call without its result")

    def test_tool_turns_never_leave_an_orphan_tool_message(self):
        msgs = self._tool_turns(4, result_chars=40000)
        body = {"model": "chat-quick", "messages": msgs}
        out = bob_context.apply_openai_request(_config(), "chat-quick", body)["messages"]
        self.assertNotEqual([m["role"] for m in out], ["system", "tool"])
        self.assertEqual(out[:2], msgs[:2])                  # system + the goal survive
        self._assert_valid(out)
        self.assertLessEqual(self._prompt(out), 16384)

    def test_large_tool_call_arguments_are_counted_and_trimmed(self):
        msgs = self._tool_turns(6, args_chars=6000, result_chars=200)
        self.assertGreater(sum(bob_core.message_tokens(m) for m in msgs), 36000)
        out = bob_context.apply_openai_request(
            _config(), "chat-quick", {"model": "chat-quick", "messages": msgs})["messages"]
        self.assertLess(len(out), len(msgs))
        self.assertLessEqual(self._prompt(out), 16384)
        self._assert_valid(out)

    def test_pinned_messages_over_the_window_stay_valid(self):
        msgs = [{"role": "system", "content": "s " * 20000}, {"role": "user", "content": "go"},
                {"role": "assistant", "content": "a " * 100}, {"role": "user", "content": "again"}]
        out = bob_context.apply_openai_request(
            _config(), "chat-quick", {"model": "chat-quick", "messages": msgs})["messages"]
        self.assertEqual(out[0], msgs[0])                     # a system prompt is never cut
        self.assertEqual([m["role"] for m in out], ["system", "user", "user"])

    def test_padding_does_not_trim_a_prompt_the_harness_fitted(self):
        # Dense content sitting just under the advertised window by the plain tokenizer count would
        # read 25% over it with the safety margin; the hook must leave it alone.
        cfg = _config()
        line = "a3f1c2d4-5e6b-7c8d-9e0f-1234567890ab "
        msgs = [{"role": "user", "content": "go"}]
        pair = [{"role": "assistant", "content": line * 10}, {"role": "user", "content": line * 10}]
        while self._prompt(msgs + pair) < 15500:
            msgs += pair
        self.assertGreater(sum(bob_core.message_tokens(m) for m in msgs), 16384)
        out = bob_context.apply_openai_request(cfg, "chat-quick", {"model": "chat-quick", "messages": msgs,
                                                                   "max_tokens": 256})
        self.assertEqual(out["messages"], msgs)


class TestFitMessages(unittest.TestCase):
    """bob_core.fit_messages: whole turns, oldest first, the goal and current ask always kept."""

    def test_under_budget_is_unchanged(self):
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
        self.assertEqual(bob_core.fit_messages(msgs, 1000), msgs)

    def test_keeps_goal_and_last_user_and_drops_the_middle(self):
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "goal"}]
        for i in range(10):
            msgs += [{"role": "assistant", "content": f"a{i} " + "x " * 300},
                     {"role": "user", "content": f"u{i} " + "y " * 300}]
        msgs.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": "t", "type": "function", "function": {"name": "f", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": "t", "content": "done"})
        out = bob_core.fit_messages(msgs, 1200)
        self.assertEqual(out[:2], msgs[:2])
        self.assertIn(msgs[-4 + 1], out)                    # the last user message (the current ask)
        self.assertEqual(out[-2:], msgs[-2:])               # the newest tool turn, call and result
        self.assertLessEqual(sum(bob_core.message_tokens(m) for m in out), 1200)

    def test_a_lone_oversized_ask_is_cut_in_the_middle(self):
        big = "head " + "x " * 20000 + " tail"
        out = bob_core.fit_messages([{"role": "system", "content": "S"}, {"role": "user", "content": big}], 500)
        self.assertEqual(out[0]["content"], "S")
        self.assertTrue(out[1]["content"].startswith("head"))
        self.assertTrue(out[1]["content"].endswith("tail"))
        self.assertLessEqual(sum(bob_core.message_tokens(m) for m in out), 500)

    def test_tool_call_arguments_and_list_content_are_counted(self):
        call = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "t", "type": "function", "function": {"name": "w", "arguments": "z " * 2000}}]}
        self.assertGreater(bob_core.message_tokens(call), 2000)
        parts = {"role": "user", "content": [{"type": "text", "text": "q " * 500},
                                             {"type": "image_url", "image_url": {"url": "data:x"}}]}
        self.assertGreater(bob_core.message_tokens(parts), 500 + bob_core.IMAGE_BLOCK_TOKENS)


class TestPrecedence(unittest.TestCase):
    """User tuning in agent.* (and memory.maxInjectedTokens) beats the shipped mode blocks; the user's
    own mode block beats both."""

    def setUp(self):
        self._view = mock.patch.object(bob_core, "_models_view", return_value=_VIEW)
        self._view.start()
        self.addCleanup(self._view.stop)

    def _shipped(self, agent=None, memory=None, quick_local=None):
        import copy
        cfg = copy.deepcopy(bob_core.load_defaults()["runtime"])
        cfg["agent"].update(agent or {})
        cfg["memory"].update(memory or {})
        cfg["agent"]["contextModes"]["quick"]["local"].update(quick_local or {})
        return cfg

    def test_shipped_config_uses_the_mode_blocks(self):
        cfg = self._shipped()
        pol = bob_context.resolve(cfg, "chat", "quick")
        self.assertEqual((pol.max_context_tokens, pol.max_history_msgs), (16384, 40))
        self.assertEqual(bob_context.resolve(cfg, "chat", "deep").max_history_msgs, 200)
        self.assertEqual(pol.output_reserve_tokens, 1024)       # Quick leaves room for a thinking reply

    def test_user_agent_values_win_over_the_shipped_mode_blocks(self):
        cfg = self._shipped(agent={"maxHistoryMsgs": 77, "maxContextTokens": 12000, "stablePrefix": True,
                                   "outputReserveTokens": 3000, "compaction": "summarize",
                                   "maxToolResultTokens": 1500},
                            memory={"maxInjectedTokens": 999})
        for mode in ("quick", "deep"):
            pol = bob_context.resolve(cfg, "chat", mode)
            self.assertEqual((pol.max_history_msgs, pol.max_context_tokens, pol.stable_prefix,
                              pol.output_reserve_tokens, pol.compaction,
                              pol.max_tool_result_tokens, pol.memory_max_injected_tokens),
                             (77, 12000, True, 3000, "summarize", 1500, 999))

    def test_a_global_override_applies_even_when_it_matches_the_code_default(self):
        # Context budgets ship only in the mode blocks, so any agent.* value is the user's: switching
        # clearing off globally works even though false is also the fallback when nothing sets it.
        cfg = self._shipped(agent={"clearToolResults": False})
        self.assertFalse(bob_context.resolve(cfg, "chat", "quick").clear_tool_results)
        cfg = self._shipped(quick_local={"clearToolResults": False})
        self.assertFalse(bob_context.resolve(cfg, "chat", "quick").clear_tool_results)

    def test_the_users_mode_block_wins_over_everything(self):
        cfg = self._shipped(agent={"maxHistoryMsgs": 77}, quick_local={"maxHistoryMsgs": 12})
        self.assertEqual(bob_context.resolve(cfg, "chat", "quick").max_history_msgs, 12)
        self.assertEqual(bob_context.resolve(cfg, "chat", "deep").max_history_msgs, 77)

    def test_advertised_window_follows_the_same_precedence(self):
        cfg = self._shipped(agent={"maxContextTokens": 12000})
        self.assertEqual(bob_context.mode_window(40960, "local", "quick", cfg), 12000)
        self.assertEqual(bob_context.mode_window(40960, "local", "deep", cfg), 12000)

    def test_cap_window(self):
        self.assertEqual(bob_core.cap_window(40960, 16384), 16384)
        self.assertEqual(bob_core.cap_window(8192, 16384), 8192)
        self.assertEqual(bob_core.cap_window(0, 16384), 16384)
        self.assertEqual(bob_core.cap_window(40960, 0), 40960)
        self.assertEqual(bob_core.cap_window(40960, "auto"), 40960)

    def test_peer_role_value(self):
        peer = {"contextWindow": 1000, "maxOutputTokens": 50}
        self.assertEqual(bob_core.peer_role_value(peer, {"contextWindow": 10}, "contextWindow"), 10)
        self.assertEqual(bob_core.peer_role_value(peer, "model-id", "contextWindow"), 1000)
        self.assertEqual(bob_core.peer_role_value({}, {}, "maxOutputTokens"), 0)


class TestNormalizeMode(unittest.TestCase):
    def test_bad_configured_default_names_the_setting(self):
        with self.assertRaises(bob_context.ContextModeError) as ctx:
            bob_context.normalize_mode(None, _config(contextMode="turbo"))
        self.assertIn("agent.contextMode is 'turbo'", str(ctx.exception))
        self.assertNotIn("None", str(ctx.exception))

    def test_unknown_explicit_mode_names_the_value(self):
        with self.assertRaises(bob_context.ContextModeError) as ctx:
            bob_context.normalize_mode("turbo", _config())
        self.assertIn("'turbo'", str(ctx.exception))
        self.assertIn("quick, deep", str(ctx.exception))

    def test_a_user_mode_named_like_an_alias_wins(self):
        cfg = _config()
        cfg["agent"]["contextModes"]["fast"] = {"label": "Fast", "local": {"maxHistoryMsgs": 5}}
        self.assertEqual(bob_context.normalize_mode("fast", cfg), "fast")
        self.assertEqual(bob_context.normalize_mode("lean", cfg), "quick")


class TestCompleteMode(unittest.TestCase):
    """bob_core.complete: a caller's mode is honoured, no mode means the model's own window."""

    def _window_seen(self, cfg, mode=None):
        seen = {}

        class _C:
            def __init__(self):
                self.chat = mock.Mock(completions=self)

            def create(self, **kw):
                seen.update(kw)
                return mock.Mock(choices=[mock.Mock(message=mock.Mock(content="x"), finish_reason="stop")])

        big = [{"role": "user", "content": "w " * 30000}]
        with mock.patch.object(bob_core, "_models_view", return_value=_VIEW), \
                mock.patch.object(bob_core, "get_llm_client", return_value=_C()):
            bob_core.complete(cfg, "chat", big, 512, context_mode=mode)
        return sum(bob_core.message_tokens(m) for m in seen["messages"])

    def test_no_mode_uses_the_model_window(self):
        self.assertGreater(self._window_seen(_config()), 16384)

    def test_a_passed_mode_caps_the_window(self):
        self.assertLessEqual(self._window_seen(_config(), "quick"), 16384)
        self.assertGreater(self._window_seen(_config(), "deep"), 16384)

    def test_an_unknown_mode_is_a_completion_error(self):
        with self.assertRaises(bob_core.CompletionError):
            self._window_seen(_config(), "turbo")


class TestServerModes(unittest.TestCase):
    """The agent server stores canonical mode names and rejects unknown ones up front."""

    def setUp(self):
        import shutil
        import tempfile
        from pathlib import Path

        import bob_agent_server as srv
        from bob_session import SessionStore
        self.srv = srv
        d = Path(tempfile.mkdtemp(prefix="bob-modes-"))
        self.addCleanup(shutil.rmtree, d, True)
        srv._config = _config()
        srv._token_owner = {"sk-test": "alice"}
        srv._registry = _common.FakeRegistry()
        srv._sessions = SessionStore(d / "s.db")
        self.addCleanup(srv._sessions.close)

    def test_session_stores_the_canonical_name(self):
        out = self.srv.create_session(self.srv.SessionCreate(context_mode="slow"), authorization="Bearer sk-test")
        self.assertEqual(out["context_mode"], "deep")

    def test_unknown_mode_is_rejected_at_creation(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            self.srv.create_session(self.srv.SessionCreate(context_mode="turbo"), authorization="Bearer sk-test")
        self.assertEqual(ctx.exception.status_code, 422)


class TestCallbackShim(unittest.TestCase):
    """LiteLLM resolves `bob_context_callback.proxy_handler_instance` relative to the config file."""

    def test_litellm_loads_the_shim_beside_the_config(self):
        try:
            from litellm.proxy.types_utils.utils import get_instance_fn
        except Exception:  # pragma: no cover - LiteLLM missing in this interpreter
            self.skipTest("litellm not installed")
        cfg = os.path.join(_common.REPO, "config", "litellm.yaml")
        inst = get_instance_fn("bob_context_callback.proxy_handler_instance", config_file_path=cfg)
        self.assertTrue(hasattr(inst, "async_pre_call_hook"))


class TestDeepSummaryNormalization(unittest.TestCase):
    """Standard summarize compaction must fold prior notes, not accumulate system frames."""

    def test_prior_frames_are_replaced_with_one_note(self):
        import bob_loop
        seen = {}

        def fake(dropped, model, budget, config=None, context_mode=None, prior_note=None):
            seen["prior"] = prior_note
            return "new note"

        orig = bob_loop._compact_span
        bob_loop._compact_span = fake
        self.addCleanup(lambda: setattr(bob_loop, "_compact_span", orig))
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "system", "content": f"{bob_loop._COMPACT_FRAME}\nold one"},
            {"role": "system", "content": f"{bob_loop._COMPACT_FRAME}\nold two"},
        ]
        for i in range(10):
            msgs.append({"role": "user" if i % 2 == 0 else "assistant",
                         "content": f"turn {i} " + "x" * 400})
        out = bob_loop.truncate_history(msgs, max_msgs=5, compaction="summarize", keep_last=2)
        frames = [m for m in out if str(m.get("content", "")).startswith(bob_loop._COMPACT_FRAME)]
        self.assertEqual(len(frames), 1)
        self.assertIn("new note", frames[0]["content"])
        self.assertIn("old one", seen["prior"])
        self.assertIn("old two", seen["prior"])


class TestPriorNoteSurvives(unittest.TestCase):
    """An earlier compaction note stays in the window when a pass drops nothing new or the summarizer
    gives nothing back."""

    def _msgs(self, n):
        msgs = [{"role": "system", "content": "sys"},
                {"role": "system", "content": f"{bob_loop._COMPACT_FRAME}\nthe goal was X"}]
        for i in range(n):
            msgs.append({"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}"})
        return msgs

    def _patch(self, note):
        calls = []

        def fake(dropped, model, budget, config=None, context_mode=None, prior_note=None):
            calls.append(dropped)
            return note

        orig = bob_loop._compact_span
        bob_loop._compact_span = fake
        self.addCleanup(lambda: setattr(bob_loop, "_compact_span", orig))
        return calls

    def _notes(self, out):
        return [m for m in out if str(m.get("content", "")).startswith(bob_loop._COMPACT_FRAME)]

    def test_no_drop_keeps_the_note(self):
        calls = self._patch("unused")
        msgs = self._msgs(4)
        out = bob_loop.truncate_history(msgs, max_msgs=50, compaction="summarize", keep_last=2)
        self.assertEqual(calls, [])
        self.assertEqual(self._notes(out), [msgs[1]])
        self.assertEqual(out[-4:], msgs[-4:])

    def test_failed_summary_keeps_the_note(self):
        calls = self._patch("")
        out = bob_loop.truncate_history(self._msgs(20), max_msgs=6, compaction="summarize", keep_last=2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self._notes(out)), 1)
        self.assertIn("the goal was X", self._notes(out)[0]["content"])

    def test_truncate_mode_keeps_the_note(self):
        out = bob_loop.truncate_history(self._msgs(20), max_msgs=6, compaction="truncate")
        self.assertEqual(len(self._notes(out)), 1)


class TestOutputHint(_common.LLMStubMixin, unittest.TestCase):
    """A reply cut at the output cap names the setting that resolves the cap."""

    def setUp(self):
        self._view = mock.patch.object(bob_core, "_models_view", return_value=_VIEW)
        self._view.start()
        self.addCleanup(self._view.stop)

    def test_hint_names_the_mode_setting(self):
        from types import SimpleNamespace

        def chunk(content, finish):
            delta = SimpleNamespace(content=content, tool_calls=None)
            return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)], usage=None)

        class _Client:
            chat = completions = None

            def create(self, **kw):
                return iter([chunk("partial", "length")])

        client = _Client()
        client.chat = client
        client.completions = client
        self.stub_llm(client)
        evs = list(bob_loop.run_agent_events("go", _config(), role="chat", agency="silent",
                                             registry=_common.FakeRegistry(), no_tools=True))
        text = json.dumps(evs)
        self.assertIn("agent.contextModes.quick.local.outputReserveTokens", text)


class TestDynamicToolResultCap(unittest.TestCase):
    def test_mode_token_cap_is_respected(self):
        import sys
        sys.path.insert(0, "scripts/tools")
        from tool_registry import ToolRegistry
        from bob_core import est_tokens
        reg = ToolRegistry()
        long_text = "word " * 2000
        out = reg._truncate_and_retain(long_text, max_tokens=100)
        self.assertLessEqual(est_tokens(out), 100)
        self.assertIn("retained as", out)


if __name__ == "__main__":
    unittest.main()


class _RecordingClient:
    """Fake OpenAI client that records model/max_tokens kwargs for each turn."""

    def __init__(self, turns):
        self.calls = []
        self._base = _common.scripted_client(turns)

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, model, messages, tools, stream, timeout, **kwargs):
        self.calls.append({"model": model, **kwargs})
        return self._base.chat.completions.create(
            model=model, messages=messages, tools=tools, stream=stream, timeout=timeout, **kwargs)


class TestLoopModeBudgets(_common.LLMStubMixin, unittest.TestCase):
    """The loop consumes the resolved mode budget, not a single global agent scalar."""

    def setUp(self):
        self._view = mock.patch.object(bob_core, "_models_view", return_value=_VIEW)
        self._view.start()
        self.addCleanup(self._view.stop)

    def _run(self, cfg, role=None):
        client = _RecordingClient(["ok"])
        self.stub_llm(client)
        evs = list(bob_loop.run_agent_events(
            "go", cfg, role=role, agency="silent", registry=_common.FakeRegistry()))
        return evs, client.calls

    def test_quick_local_uses_the_quick_output_cap(self):
        evs, calls = self._run(_config(), role="chat")
        self.assertEqual(evs[-1]["type"], "final")
        self.assertEqual(calls[0]["max_tokens"], 512)

    def test_deep_local_uses_the_deep_output_cap(self):
        evs, calls = self._run(_config(contextMode="deep"), role="chat")
        self.assertEqual(evs[-1]["type"], "final")
        self.assertEqual(calls[0]["max_tokens"], 2048)

    def test_quick_api_uses_the_api_quick_cap_not_the_local_one(self):
        evs, calls = self._run(_config(), role="chat-pro")
        self.assertEqual(evs[-1]["type"], "final")
        self.assertEqual(calls[0]["max_tokens"], 4096)

    def test_deep_api_uses_the_peer_cap(self):
        evs, calls = self._run(_config(contextMode="deep"), role="chat-pro")
        self.assertEqual(evs[-1]["type"], "final")
        self.assertEqual(calls[0]["max_tokens"], 32768)


class TestCallback(unittest.IsolatedAsyncioTestCase):
    """The LiteLLM pre-call hook is the single external-harness enforcement seam."""

    async def test_alias_body_is_transformed(self):
        cfg = _config()
        data = {"model": "chat-pro-quick", "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 999999}
        with mock.patch.object(bob_core, "_models_view", return_value=_VIEW), \
             mock.patch.object(bob_core, "load_config", return_value=cfg):
            out = await bob_context_callback.proxy_handler_instance.async_pre_call_hook(
                None, None, data, "completion")
        self.assertEqual(out["max_tokens"], 32768)   # the peer's real maximum

    async def test_base_model_is_untouched(self):
        cfg = _config()
        data = {"model": "chat", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 99}
        with mock.patch.object(bob_core, "_models_view", return_value=_VIEW), \
             mock.patch.object(bob_core, "load_config", return_value=cfg):
            out = await bob_context_callback.proxy_handler_instance.async_pre_call_hook(
                None, None, data, "completion")
        self.assertEqual(out, data)


class TestDeepServer(unittest.TestCase):
    """A local role with a `deep` block is served by its own Deep server in Deep mode: Bob requests
    `<role>-deep` and the window is that server's."""

    def _view(self, deep=True):
        chat = {"gguf": "m.gguf", "ctx": 40960}
        if deep:
            chat["deep"] = {"ctx": 262144, "ngl": "auto"}
        return ({"defaults": {"parallel": 1}}, "16gb", {"chat": chat, "coder": dict(chat, _aliasOf="chat"),
                                                       "vision": {"gguf": "v.gguf", "ctx": 4096}})

    def test_deep_server_spec_layers_the_deep_block(self):
        spec = bob_core.deep_server_spec({"ctx": 40960, "flags": ["--jinja"], "deep": {"ctx": 262144, "ngl": "auto"}})
        self.assertEqual(spec, {"ctx": 262144, "flags": ["--jinja"], "ngl": "auto"})
        self.assertIsNone(bob_core.deep_server_spec({"ctx": 40960}))

    def test_deep_mode_requests_the_deep_server_with_its_window(self):
        cfg = bob_core.load_defaults()["runtime"]
        with mock.patch.object(bob_core, "_models_view", return_value=self._view()):
            self.assertEqual(bob_core.wire_model(cfg, "coder", "deep"), "coder-deep")
            self.assertEqual(bob_core.wire_model(cfg, "coder", "quick"), "coder")
            self.assertEqual(bob_core.wire_model(cfg, "vision", "deep"), "vision")
            self.assertEqual(bob_context.resolve(cfg, "coder", "deep").window(cfg, "coder"), 262144)
            self.assertEqual(bob_context.resolve(cfg, "coder", "quick").window(cfg, "coder"), 16384)

    def test_without_a_deep_block_deep_uses_the_role_server(self):
        cfg = bob_core.load_defaults()["runtime"]
        with mock.patch.object(bob_core, "_models_view", return_value=self._view(deep=False)):
            self.assertEqual(bob_core.wire_model(cfg, "coder", "deep"), "coder")
            self.assertEqual(bob_context.resolve(cfg, "coder", "deep").window(cfg, "coder"), 40960)

    def test_complete_sends_deep_runs_to_the_deep_server(self):
        cfg = bob_core.load_defaults()["runtime"]
        sent = {}

        class _Client:
            def __init__(self):
                self.chat = self
                self.completions = self

            def create(self, **kw):
                sent.update(kw)
                msg = mock.Mock(content="ok")
                return mock.Mock(choices=[mock.Mock(message=msg, finish_reason="stop")])

        with mock.patch.object(bob_core, "_models_view", return_value=self._view()), \
             mock.patch.object(bob_core, "get_llm_client", return_value=_Client()):
            bob_core.complete(cfg, "coder", [{"role": "user", "content": "hi"}], 64, context_mode="deep")
        self.assertEqual(sent["model"], "coder-deep")
