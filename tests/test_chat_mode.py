"""S (front-door unification) — the loop chat-mode seam (no_tools + max_tokens) and the unified
`bob chat|code|think` handler (role routing, flags, legacy syntax, one-shot vs shell REPL). Fake
client + monkeypatch; no model."""
import unittest
from types import SimpleNamespace

import _common
import bob_core
import bob_loop

ECHO_SCHEMA = {"type": "function", "function": {
    "name": "echo", "description": "", "parameters": {"type": "object", "properties": {}}}}


def _recording_client(turns):
    """A fake client that records every create() kwarg set and streams the scripted turns."""
    calls = []

    class _C:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)

        def create(self, model, messages, tools, stream, timeout, **kwargs):
            calls.append({"model": model, "tools": tools, **kwargs})
            content = turns[min(len(calls) - 1, len(turns) - 1)]
            return _common._FakeStream([_common._content_chunk(content)])

    return _C(), calls


class _PopulatedRegistry(_common.FakeRegistry):
    """A registry that advertises one tool, with a filtered() that honors allow=[] (→ empty)."""
    def __init__(self):
        super().__init__({"echo": "echoed"})
        self.tool_schemas = [ECHO_SCHEMA]

    def filtered(self, deny=None, allow=None):
        view = _common.FakeRegistry(self._results)
        view.tool_schemas = [] if allow == [] else self.tool_schemas
        return view


class TestLoopChatSeam(unittest.TestCase):
    def setUp(self):
        self._check = bob_core.check_litellm
        self._client = bob_core.get_llm_client
        bob_core.check_litellm = lambda config=None: True

    def tearDown(self):
        bob_core.check_litellm = self._check
        bob_core.get_llm_client = self._client

    def _openai_cfg(self):
        return _common.fake_config(agent={"toolFormat": "openai", "maxSteps": 5,
                                          "maxContextTokens": 0, "maxToolResultTokens": 1000})

    def test_max_tokens_threaded_when_set(self):
        client, calls = _recording_client(["hi"])
        bob_core.get_llm_client = lambda config=None: client
        list(bob_loop.run_agent_events("q", self._openai_cfg(), agency="silent",
                                       registry=_common.FakeRegistry(), max_tokens=128))
        self.assertEqual(calls[0].get("max_tokens"), 128)

    def test_max_tokens_absent_by_default(self):
        client, calls = _recording_client(["hi"])
        bob_core.get_llm_client = lambda config=None: client
        list(bob_loop.run_agent_events("q", self._openai_cfg(), agency="silent",
                                       registry=_common.FakeRegistry()))
        self.assertNotIn("max_tokens", calls[0])   # matches the prior path when unset

    def test_no_tools_suppresses_tools(self):
        # openai mode: tools kwarg is built from the registry schemas — no_tools must empty it.
        client, calls = _recording_client(["answer"])
        bob_core.get_llm_client = lambda config=None: client
        list(bob_loop.run_agent_events("q", self._openai_cfg(), agency="silent",
                                       registry=_PopulatedRegistry(), no_tools=True))
        self.assertIsNone(calls[0]["tools"])       # filtered to empty -> no tools sent

    def test_tools_present_without_no_tools(self):
        client, calls = _recording_client(["answer"])
        bob_core.get_llm_client = lambda config=None: client
        list(bob_loop.run_agent_events("q", self._openai_cfg(), agency="silent",
                                       registry=_PopulatedRegistry()))
        self.assertIsNotNone(calls[0]["tools"])    # default: tools ARE advertised

    def test_no_tools_still_answers(self):
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(["just chatting"])
        result, _ = bob_loop.run_agent("hi", self._openai_cfg(), agency="silent",
                                       registry=_common.FakeRegistry(), no_tools=True)
        self.assertEqual(result, "just chatting")


class TestChatHandler(unittest.TestCase):
    """cli._chat routing/flags — mock run_agent + shell.run to capture the resolved call."""

    def setUp(self):
        from bob import cli
        self.cli = cli
        self._orig_run_agent = bob_loop.run_agent
        self._orig_load = bob_core.load_config
        bob_core.load_config = lambda: _common.fake_config()
        # _chat now auto-starts inference (_ensure_endpoint) — neutralize it so the routing tests don't
        # probe/launch the real stack (which would regenerate configs + contaminate other tests).
        self._orig_ensure = self.cli._ensure_endpoint
        self.cli._ensure_endpoint = lambda config: None
        self.captured = {}

        def fake_run_agent(goal, config, role=None, agency=None, stream=False,
                           no_tools=False, max_tokens=None, **kw):
            self.captured = {"goal": goal, "role": role, "stream": stream,
                             "no_tools": no_tools, "max_tokens": max_tokens}
            return ("ok", False)

        bob_loop.run_agent = fake_run_agent

    def tearDown(self):
        bob_loop.run_agent = self._orig_run_agent
        bob_core.load_config = self._orig_load
        self.cli._ensure_endpoint = self._orig_ensure

    def test_oneshot_chat_routes_chat_role_no_tools(self):
        self.cli._chat("chat", ["hello", "world"])
        self.assertEqual(self.captured["goal"], "hello world")
        self.assertEqual(self.captured["role"], "chat")
        self.assertTrue(self.captured["no_tools"])

    def test_code_and_think_route_roles(self):
        self.cli._chat("code", ["x"])
        self.assertEqual(self.captured["role"], "coder")
        self.cli._chat("think", ["x"])
        self.assertEqual(self.captured["role"], "planner")

    def test_pro_flag(self):
        self.cli._chat("chat", ["--pro", "x"])
        self.assertEqual(self.captured["role"], "chat-pro")

    def test_think_code_flags_override_task(self):
        self.cli._chat("chat", ["--think", "x"])
        self.assertEqual(self.captured["role"], "planner")

    def test_max_and_raw(self):
        self.cli._chat("chat", ["--max", "50", "--raw", "hi"])
        self.assertEqual(self.captured["max_tokens"], 50)
        self.assertFalse(self.captured["stream"])      # --raw => no streaming
        self.assertEqual(self.captured["goal"], "hi")

    def test_legacy_known_role_syntax(self):
        self.cli._chat("chat", ["planner", "solve", "this"])
        self.assertEqual(self.captured["role"], "planner")
        self.assertEqual(self.captured["goal"], "solve this")

    def test_no_prompt_launches_shell_chat_mode(self):
        import bob.shell as shell
        orig = shell.run
        seen = {}
        shell.run = lambda config=None, role=None, no_tools=False: seen.update(role=role, no_tools=no_tools) or 0
        try:
            self.cli._chat("chat", [])
        finally:
            shell.run = orig
        self.assertEqual(seen, {"role": "chat", "no_tools": True})


if __name__ == "__main__":
    unittest.main()
