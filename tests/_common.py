"""Shared test helpers (M13). Import first — it puts scripts/ and scripts/tools/ on sys.path
so the tests run under both `python -m unittest discover -s tests` and `pytest`."""
import os
import sys
from types import SimpleNamespace

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(REPO, "scripts"), os.path.join(REPO, "scripts", "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def fake_config(**over):
    """A minimal but complete config dict for tests — no config.json / network needed."""
    cfg = {
        "litellmPort": 8081,
        "litellmKey": "sk-test",
        "searxngPort": 8888,
        "routing": {
            "defaultRole": "chat", "proRole": "chat-pro",
            "codeRole": "coder", "proCodeRole": "coder-pro",
            "thinkRole": "planner", "proThinkRole": "planner-pro",
            "agentRole": "agent",
        },
        "vision": {"visionRole": "vision", "visionProRole": "vision-pro"},
        "persona": {"systemPrompt": "You are Bob."},
        "memory": {"enabled": False},
        "agent": {
            "toolFormat": "hermes", "maxSteps": 5,
            "maxContextTokens": 0, "maxToolResultTokens": 1000,
        },
    }
    for k, v in over.items():
        cfg[k] = v
    return cfg


class FakeRegistry:
    """Stand-in for ToolRegistry with scripted dispatch results.

    O6: carries `mutating_tools` / `approval_required_tools` (empty by default) so permission tests
    can mark a fake tool mutating or approval-gated. `dispatched` records what actually ran, so a test
    can assert a denied tool never reached dispatch_call."""

    def __init__(self, results=None, mutating_tools=None, approval_required_tools=None, delay=0.0):
        self.tool_schemas = []
        self.exit_voice_tools = set()
        self._loaded_names = set()   # /health reads these
        self.errors = []
        self._results = results or {}
        self.mutating_tools = set(mutating_tools or ())
        self.approval_required_tools = set(approval_required_tools or ())
        self.dispatched = []
        self._delay = delay   # O2 — per-call sleep so a test can measure parallel vs sequential wall-clock

    def dispatch_call(self, name, arguments_json, context=None):
        if self._delay:
            import time
            time.sleep(self._delay)
        self.dispatched.append(name)   # list.append is atomic under the GIL — safe from pool threads
        return self._results.get(name, f"[{name} ran]")


def _content_chunk(text):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text, tool_calls=None))])


class _FakeStream:
    """An iterable streaming response with a .close() the loop can call on cancel (N3)."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False

    def __iter__(self):
        for c in self._chunks:
            yield c

    def close(self):
        self.closed = True


def scripted_client(turns):
    """A fake OpenAI client whose create() returns each item of `turns` in order, as a one-chunk
    stream (the loop always consumes streaming internally now, N3). Each turn is the assistant
    content string (Hermes tool calls inline as <tool_call>…)."""
    state = {"i": 0}

    class _C:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)

        def create(self, model, messages, tools, stream, timeout):
            i = state["i"]
            state["i"] += 1
            content = turns[min(i, len(turns) - 1)]
            return _FakeStream([_content_chunk(content)])

    return _C()


def stream_client(deltas):
    """A fake client whose streaming create() yields one chunk per string in `deltas`."""
    class _C:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)

        def create(self, model, messages, tools, stream, timeout):
            return _FakeStream([_content_chunk(d) for d in deltas])

    return _C()


def multi_turn_stream_client(turns):
    """Fake client: each create() streams the next turn; a turn is a list of content-delta strings,
    so a test can split a <tool_call> marker across chunks (N6)."""
    state = {"i": 0}

    class _C:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)

        def create(self, model, messages, tools, stream, timeout):
            i = state["i"]
            state["i"] += 1
            deltas = turns[min(i, len(turns) - 1)]
            return _FakeStream([_content_chunk(d) for d in deltas])

    return _C()


def slow_stream_client(deltas, sleep_s=0.02, on_chunk=None):
    """A streaming fake that sleeps between chunks so a test can trip a cancel token mid-stream
    (N3). on_chunk(i) runs before yielding chunk i — use it to set the token. The returned stream
    exposes .close() and records .closed so the test can assert the abort path ran."""
    import time

    class _SlowStream:
        def __init__(self):
            self.closed = False

        def __iter__(self):
            for i, d in enumerate(deltas):
                if on_chunk:
                    on_chunk(i)
                time.sleep(sleep_s)
                yield _content_chunk(d)

        def close(self):
            self.closed = True

    the_stream = _SlowStream()

    class _C:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)
            self.last_stream = the_stream

        def create(self, model, messages, tools, stream, timeout):
            return the_stream

    return _C()
