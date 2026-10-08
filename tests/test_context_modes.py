"""Context modes: Quick/Deep policies resolve separately for local and API roles."""
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

    def test_quick_alias_trims_history_and_caps_output(self):
        cfg = _config()
        msgs = [{"role": "system", "content": "sys"}]
        for i in range(40):
            msgs.append({"role": "user" if i % 2 == 0 else "assistant", "content": "h" * 4000})
        body = {"model": "chat-quick", "messages": msgs, "max_tokens": 9999}
        out = bob_context.apply_openai_request(cfg, "chat-quick", body)
        self.assertEqual(out["max_tokens"], 512)
        self.assertLess(len(out["messages"]), len(msgs))
        self.assertEqual(out["messages"][-1], msgs[-1])

    def test_api_deep_alias_uses_peer_output_cap(self):
        cfg = _config()
        body = {"model": "chat-pro-deep", "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 999999}
        out = bob_context.apply_openai_request(cfg, "chat-pro-deep", body)
        self.assertEqual(out["max_tokens"], 32768)


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
        data = {"model": "chat-quick", "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 9999}
        with mock.patch.object(bob_core, "_models_view", return_value=_VIEW), \
             mock.patch.object(bob_core, "load_config", return_value=cfg):
            out = await bob_context_callback.proxy_handler_instance.async_pre_call_hook(
                None, None, data, "completion")
        self.assertEqual(out["max_tokens"], 512)

    async def test_base_model_is_untouched(self):
        cfg = _config()
        data = {"model": "chat", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 99}
        with mock.patch.object(bob_core, "_models_view", return_value=_VIEW), \
             mock.patch.object(bob_core, "load_config", return_value=cfg):
            out = await bob_context_callback.proxy_handler_instance.async_pre_call_hook(
                None, None, data, "completion")
        self.assertEqual(out, data)
