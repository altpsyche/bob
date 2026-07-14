"""Grammar-constrained tool calls. When agent.constrainedToolCalls is on, the loop attaches the
structured `tools` payload + tool_choice='auto' so a grammar-capable backend (llama.cpp) can only emit
a well-formed tool call, killing the malformed-JSON (__parse_error__) class — while still allowing text
answers. A backend that rejects the kwargs is detected once and the run falls back to today's hermes
parse. Default off keeps the request kwargs matching the prior path. Decode QUALITY needs a live model
and is validated on the GPU tier — these assert wiring + fallback only."""
import unittest
from types import SimpleNamespace

import _common
import bob_core
import bob_loop
from bob_loop import _is_unsupported_constraint

_SCHEMA = [{"type": "function", "function": {
    "name": "echo", "description": "echo", "parameters": {"type": "object", "properties": {}}}}]


def _stream(content="done"):
    return _common._FakeStream([_common._content_chunk(content)])


def _recording_client(calls, content="done"):
    class _C:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)

        def create(self, **kwargs):
            calls.append(dict(kwargs))
            return _stream(content)

    return _C()


class _BadRequest(Exception):
    def __init__(self, msg):
        super().__init__(msg)
        self.status_code = 400


def _reject_constraint_client(calls, content="done"):
    """Rejects any request carrying tool_choice (like a non-grammar OpenAI endpoint), else succeeds."""
    class _C:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)

        def create(self, **kwargs):
            calls.append(dict(kwargs))
            if "tool_choice" in kwargs:
                raise _BadRequest("tool_choice is not supported by this backend")
            return _stream(content)

    return _C()


class TestUnsupportedConstraintPredicate(unittest.TestCase):
    def test_badrequest_naming_the_param(self):
        self.assertTrue(_is_unsupported_constraint(_BadRequest("tool_choice not supported")))
        self.assertTrue(_is_unsupported_constraint(_BadRequest("unknown field: response_format")))

    def test_unexpected_keyword_typeerror(self):
        self.assertTrue(_is_unsupported_constraint(
            TypeError("create() got an unexpected keyword argument 'tool_choice'")))

    def test_not_a_plain_400_without_keyword(self):
        self.assertFalse(_is_unsupported_constraint(_BadRequest("your prompt was too long")))

    def test_not_transient_or_generic(self):
        to = type("APITimeoutError", (Exception,), {})()
        self.assertFalse(_is_unsupported_constraint(to))
        self.assertFalse(_is_unsupported_constraint(Exception("boom")))


class _LoopBase(unittest.TestCase):
    def setUp(self):
        self._orig_check = bob_core.check_litellm
        self._orig_client = bob_core.get_llm_client
        bob_core.check_litellm = lambda config=None: True

    def tearDown(self):
        bob_core.check_litellm = self._orig_check
        bob_core.get_llm_client = self._orig_client

    def _cfg(self, **agent_over):
        cfg = _common.fake_config()
        cfg["agent"] = dict(cfg["agent"], **agent_over)
        return cfg

    def _reg(self, schemas=None):
        r = _common.FakeRegistry()
        r.tool_schemas = schemas if schemas is not None else list(_SCHEMA)
        return r


class TestDefaultOffByteIdentical(_LoopBase):
    def test_no_constraint_kwargs_when_off(self):
        calls = []
        bob_core.get_llm_client = lambda config=None: _recording_client(calls)
        list(bob_loop.run_agent_events("go", self._cfg(), agency="silent", registry=self._reg()))
        self.assertEqual(len(calls), 1)
        # Base request + the reasoning-mode carrier (extra_body). No constraint kwargs when off.
        self.assertEqual(set(calls[0]), {"model", "messages", "tools", "stream", "timeout", "extra_body"})
        self.assertNotIn("tool_choice", calls[0])
        self.assertIsNone(calls[0]["tools"])          # hermes mode passes tools=None today
        # think defaults off -> enable_thinking False rides extra_body (stable per request; the prompt
        # tokens are unchanged, so prefix/KV caching is unaffected).
        self.assertEqual(calls[0]["extra_body"], {"chat_template_kwargs": {"enable_thinking": False}})

    def test_think_param_enables_reasoning(self):
        calls = []
        bob_core.get_llm_client = lambda config=None: _recording_client(calls)
        list(bob_loop.run_agent_events("go", self._cfg(), agency="silent", registry=self._reg(), think=True))
        self.assertEqual(calls[0]["extra_body"], {"chat_template_kwargs": {"enable_thinking": True}})

    def test_think_falls_back_to_config_default(self):
        # think=None (the caller didn't pass one) uses agent.think from config.
        calls = []
        bob_core.get_llm_client = lambda config=None: _recording_client(calls)
        list(bob_loop.run_agent_events("go", self._cfg(think=True), agency="silent", registry=self._reg()))
        self.assertEqual(calls[0]["extra_body"], {"chat_template_kwargs": {"enable_thinking": True}})

    def test_extra_body_scoped_to_local_models(self):
        # A cloud peer (chat-pro is not a locally served role) must NOT receive the chat-template
        # kwarg: llama-server consumes it; DeepSeek/GLM don't take it.
        calls = []
        bob_core.get_llm_client = lambda config=None: _recording_client(calls)
        cfg = self._cfg()
        cfg["routing"] = dict(cfg["routing"], agentRole="chat-pro")
        list(bob_loop.run_agent_events("go", cfg, agency="silent", registry=self._reg()))
        self.assertNotIn("extra_body", calls[0])

    def test_reasoning_content_never_enters_the_transcript(self):
        # llama-server (--reasoning-format deepseek) streams reasoning in a separate reasoning_content
        # field; the content-only stream reader must drop it so it never reaches the answer or memory.
        def _reasoning_client(calls):
            class _C:
                def __init__(self):
                    self.chat = SimpleNamespace(completions=self)

                def create(self, **kwargs):
                    calls.append(dict(kwargs))
                    reasoning = SimpleNamespace(choices=[SimpleNamespace(
                        delta=SimpleNamespace(content=None, reasoning_content="secret chain of thought",
                                              tool_calls=None))])
                    answer = _common._content_chunk("42 is the answer")
                    return _common._FakeStream([reasoning, answer])
            return _C()

        calls = []
        bob_core.get_llm_client = lambda config=None: _reasoning_client(calls)
        events = list(bob_loop.run_agent_events("go", self._cfg(), agency="silent",
                                                registry=self._reg(), think=True))
        final = next(e["result"] for e in events if e["type"] == "final")
        self.assertIn("42 is the answer", final)
        self.assertNotIn("secret chain of thought", final)


class TestConstraintAttached(_LoopBase):
    def test_tools_and_tool_choice_attached_in_hermes_mode(self):
        calls = []
        bob_core.get_llm_client = lambda config=None: _recording_client(calls)
        list(bob_loop.run_agent_events("go", self._cfg(constrainedToolCalls=True),
                                       agency="silent", registry=self._reg()))
        self.assertEqual(calls[0]["tool_choice"], "auto")
        self.assertEqual(calls[0]["tools"], _SCHEMA)   # structured payload built even in hermes mode

    def test_no_attach_when_no_tool_schemas(self):
        calls = []
        bob_core.get_llm_client = lambda config=None: _recording_client(calls)
        list(bob_loop.run_agent_events("go", self._cfg(constrainedToolCalls=True),
                                       agency="silent", registry=self._reg(schemas=[])))
        self.assertNotIn("tool_choice", calls[0])       # nothing to constrain -> byte-identical


class TestBackendFallback(_LoopBase):
    def test_rejection_falls_back_and_latches_off(self):
        calls = []
        bob_core.get_llm_client = lambda config=None: _reject_constraint_client(calls)
        # Two steps: a tool call then a final answer, so we can see the constraint stays off after drop.
        turns = ['<tool_call>{"name": "echo", "arguments": {}}</tool_call>', "final answer"]

        # Multi-turn: reject_constraint_client always rejects tool_choice; the loop must retry without.
        state = {"i": 0}

        class _C:
            def __init__(self):
                self.chat = SimpleNamespace(completions=self)

            def create(self, **kwargs):
                calls.append(dict(kwargs))
                if "tool_choice" in kwargs:
                    raise _BadRequest("tool_choice is not supported")
                content = turns[min(state["i"], len(turns) - 1)]
                state["i"] += 1
                return _stream(content)

        bob_core.get_llm_client = lambda config=None: _C()
        events = list(bob_loop.run_agent_events("go", self._cfg(constrainedToolCalls=True),
                                                agency="silent", registry=self._reg()))
        # The run still completed via the fallback path.
        self.assertEqual(events[-1]["type"], "final")
        self.assertEqual(events[-1]["result"], "final answer")
        # First call tried tool_choice (and was rejected); after the latch, later calls never send it again.
        self.assertIn("tool_choice", calls[0])
        self.assertFalse(any("tool_choice" in c for c in calls[2:]),
                         "constraint must stay latched off after a rejection")


class TestParseErrorFallbackStillWorks(_LoopBase):
    def test_malformed_hermes_call_recovers_when_off(self):
        # Malformed JSON inside <tool_call> -> __parse_error__ recovery guidance, then a clean answer.
        turns = ['<tool_call>{"name": "echo", "arguments": {bad}}</tool_call>', "recovered answer"]
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(turns)
        events = list(bob_loop.run_agent_events("go", self._cfg(), agency="silent", registry=self._reg()))
        types = [e["type"] for e in events]
        self.assertIn("tool_result", types)             # the __parse_error__ pseudo-call ran
        self.assertEqual(events[-1]["result"], "recovered answer")


if __name__ == "__main__":
    unittest.main()
