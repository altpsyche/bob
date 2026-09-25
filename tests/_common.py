"""Shared test helpers. Import first — it puts scripts/ and scripts/tools/ on sys.path
so the tests run under both `python -m unittest discover -s tests` and `pytest`.

Importing it also makes the run hermetic: ambient knobs that change behaviour (BOB_PROFILE, secret env
vars, NO_COLOR, ...) are cleared, state (data dir, logs, the checkpoint/code DBs, the agent log) goes to a
per-run temp dir, the machine's config/user.json overlay is ignored, and the active profile is pinned to
TEST_PROFILE. A test that needs any of these sets it itself (mock.patch.dict(os.environ, ...))."""
import atexit
import contextlib
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(REPO, "scripts"), os.path.join(REPO, "scripts", "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# --- hermetic environment ------------------------------------------------------------------------

TEST_PROFILE = "16gb"   # the profile the suite's model/generator assertions are written against

# Env vars that steer behaviour off the machine: the profile override, every osenv.secret() lookup key
# (exact name + BOB_<UPPER>), colour, the OS test hook, and the voice-server knobs.
_AMBIENT_ENV = (
    "BOB_PROFILE", "BOB_FORCE_OS", "BOB_VIRTUAL_DISPLAY", "NO_COLOR", "HF_TOKEN",
    "litellmKey", "BOB_LITELLMKEY", "braveApiKey", "BOB_BRAVEAPIKEY", "tavilyApiKey", "BOB_TAVILYAPIKEY",
    "STT_PORT", "STT_MODEL", "STT_MODEL_DIR", "STT_DEVICE", "STT_COMPUTE_TYPE", "STT_PRELOAD",
    "STT_IDLE_SECONDS", "PIPER_PORT", "PIPER_VOICE", "PIPER_EXE",
)
for _k in _AMBIENT_ENV:
    os.environ.pop(_k, None)


def _test_root() -> Path:
    """The per-run temp root. A child process started by a test inherits BOB_TEST_ROOT and shares it;
    only the process that created it removes it."""
    inherited = os.environ.get("BOB_TEST_ROOT")
    if inherited and Path(inherited).is_dir():
        return Path(inherited)
    root = Path(tempfile.mkdtemp(prefix="bob-tests-"))
    os.environ["BOB_TEST_ROOT"] = str(root)
    atexit.register(shutil.rmtree, root, True)
    return root


TEST_ROOT = _test_root()
TEST_DATA = TEST_ROOT / "data"
TEST_LOGS = TEST_DATA / "logs"          # osenv.cache_dir() under BOB_DATA_DIR
TEST_LOGS.mkdir(parents=True, exist_ok=True)
# The stamp stops osenv._migrate_once copying the real data/ into the temp dir.
(TEST_DATA / ".migrated").write_text("", encoding="utf-8")
_apf = TEST_DATA / "active-profile.json"
if not _apf.exists():
    _apf.write_text(json.dumps({"activeProfile": TEST_PROFILE}) + "\n", encoding="utf-8")

os.environ["BOB_DATA_DIR"] = str(TEST_DATA)
os.environ["DSH_HOME"] = str(TEST_ROOT / "dsh")                 # absent: install_dsh skips
os.environ["XDG_CONFIG_HOME"] = str(TEST_ROOT / "xdg-config")
os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"   # no OS-keychain secrets
# The user-overlay path, for loaders that read it from the environment (child processes included).
_NO_USER_JSON = TEST_ROOT / "config" / "user.json"
os.environ["BOB_USER_CONFIG"] = str(_NO_USER_JSON)


def _neutralize_repo_state() -> None:
    """Point the module-level repo paths at the temp root: the user overlay (config/user.json + .toml),
    the checkpoint + code-index DBs, and the rotating agent log (a pre-seeded handler stops
    bob_loop._agent_logger attaching one to logs/bob-agent.log)."""
    import bob_checkpoint
    import bob_config
    import bob_models
    import bob_repomap

    bob_config._USER_JSON = _NO_USER_JSON
    bob_config._USER_TOML = _NO_USER_JSON.with_suffix(".toml")
    bob_models.USER_FILE = _NO_USER_JSON
    bob_checkpoint.DEFAULT_DB = TEST_DATA / "checkpoints.db"
    bob_repomap.CODE_DB = TEST_DATA / "code.db"

    # The runtime defaults carry REPO-relative state paths (REPO / "data/bob.db", ...). Absolute temp paths
    # resolve as-is (REPO / abs == abs), so a config built from the defaults never reaches the real files.
    import bob_core
    runtime = bob_core.load_defaults().setdefault("runtime", {})
    runtime.setdefault("memory", {})["dbPath"] = str(TEST_DATA / "bob.db")
    agent = runtime.setdefault("agent", {})
    agent["sessionDbPath"] = str(TEST_DATA / "sessions.db")
    agent["scheduleFile"] = str(TEST_DATA / "schedules.json")
    agent["logFile"] = str(TEST_LOGS / "bob-agent.log")
    bob_core._MEM_DEFAULTS = runtime["memory"]

    log = logging.getLogger("bob.agent")
    if not log.handlers:
        h = logging.FileHandler(TEST_LOGS / "bob-agent.log", encoding="utf-8", delay=True)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(h)
        log.setLevel(logging.INFO)
        log.propagate = False


_neutralize_repo_state()


# --- shared stubs --------------------------------------------------------------------------------

@contextlib.contextmanager
def stubbed_llm(client=None, up=True, factory=None):
    """Stub bob_core.check_litellm (returns `up`), bob_core.litellm_key_rejected (False) and
    bob_core.get_llm_client, restoring all three on exit.
    get_llm_client returns `client` on every call, or a fresh factory() per call when `factory` is given
    (e.g. factory=lambda: scripted_client(turns)); with neither it is left real. Any test driving
    run_agent(_events) needs this or it passes only while the real stack happens to be up."""
    import bob_core

    saved = (bob_core.check_litellm, bob_core.get_llm_client, bob_core.litellm_key_rejected)
    bob_core.check_litellm = lambda config=None: up
    bob_core.litellm_key_rejected = lambda config=None: False
    if factory is not None:
        bob_core.get_llm_client = lambda config=None: factory()
    elif client is not None:
        bob_core.get_llm_client = lambda config=None: client
    try:
        yield client
    finally:
        bob_core.check_litellm, bob_core.get_llm_client, bob_core.litellm_key_rejected = saved


class LLMStubMixin:
    """unittest.TestCase mixin: self.stub_llm(client) applies stubbed_llm for the rest of the test."""

    def stub_llm(self, client=None, up=True, factory=None):
        cm = stubbed_llm(client, up, factory)
        cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)
        return client


def fake_config(**over):
    """A minimal but complete config dict for tests — no config.json / network needed."""
    cfg = {
        "litellmPort": 8081,
        "litellmKey": "sk-test",
        "searxngPort": 8888,
        "routing": {
            "defaultRole": "chat", "proRole": "chat-pro",
            "codeRole": "coder", "proCodeRole": "coder-pro",
            "ponderRole": "ponder", "proPonderRole": "ponder-pro",
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


class FakeSkillRegistry:
    """Stand-in for bob_skills.SkillRegistry. `skills` is names, a list of skill dicts, or a
    {name: dict} map; list() returns dicts carrying their name, run() records the call."""

    def __init__(self, skills=None, errors=None):
        if isinstance(skills, dict):
            self.skills = {n: {"name": n, **s} for n, s in skills.items()}
        else:
            self.skills = {}
            for s in skills or ():
                s = {"name": s} if isinstance(s, str) else dict(s)
                self.skills[s["name"]] = s
        self.errors = list(errors or [])
        self.ran = []

    def list(self):
        return [dict(s) for s in self.skills.values()]

    def run(self, name, registry=None, config=None, context=None, args=""):
        self.ran.append(name)
        return f"[ran skill {name}]"

    def run_events(self, name, registry=None, config=None, context=None, args="", **kwargs):
        yield {"type": "final", "result": self.run(name, registry, config, context, args), "skill": name}


class FakeRegistry:
    """Stand-in for ToolRegistry with scripted dispatch results.

    Carries `mutating_tools` / `approval_required_tools` (empty by default) so permission tests
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
        self._delay = delay   # per-call sleep so a test can measure parallel vs sequential wall-clock

    def dispatch_call(self, name, arguments_json, context=None):
        if self._delay:
            import time
            time.sleep(self._delay)
        self.dispatched.append(name)   # list.append is atomic under the GIL — safe from pool threads
        return self._results.get(name, f"[{name} ran]")


def _content_chunk(text):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text, tool_calls=None))])


class _FakeStream:
    """An iterable streaming response with a .close() the loop can call on cancel."""

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
    stream (the loop always consumes streaming internally now). Each turn is the assistant
    content string (Hermes tool calls inline as <tool_call>…)."""
    state = {"i": 0}

    class _C:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)

        def create(self, model, messages, tools, stream, timeout, **kwargs):
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

        def create(self, model, messages, tools, stream, timeout, **kwargs):
            return _FakeStream([_content_chunk(d) for d in deltas])

    return _C()


def multi_turn_stream_client(turns):
    """Fake client: each create() streams the next turn; a turn is a list of content-delta strings,
    so a test can split a <tool_call> marker across chunks."""
    state = {"i": 0}

    class _C:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)

        def create(self, model, messages, tools, stream, timeout, **kwargs):
            i = state["i"]
            state["i"] += 1
            deltas = turns[min(i, len(turns) - 1)]
            return _FakeStream([_content_chunk(d) for d in deltas])

    return _C()


def slow_stream_client(deltas, sleep_s=0.02, on_chunk=None):
    """A streaming fake that sleeps between chunks so a test can trip a cancel token mid-stream
    on_chunk(i) runs before yielding chunk i — use it to set the token. The returned stream
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

        def create(self, model, messages, tools, stream, timeout, **kwargs):
            return the_stream

    return _C()
