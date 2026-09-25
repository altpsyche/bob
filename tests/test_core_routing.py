"""bob_core routing + port defaults."""
import unittest
from unittest import mock

import _common
import bob_core


class TestGetRole(unittest.TestCase):
    def setUp(self):
        self.cfg = _common.fake_config()

    def test_chat_and_pro(self):
        self.assertEqual(bob_core.get_role(self.cfg, "chat"), "chat")
        self.assertEqual(bob_core.get_role(self.cfg, "chat", pro=True), "chat-pro")

    def test_code_think_agent(self):
        self.assertEqual(bob_core.get_role(self.cfg, "code"), "coder")
        self.assertEqual(bob_core.get_role(self.cfg, "code", pro=True), "coder-pro")
        self.assertEqual(bob_core.get_role(self.cfg, "ponder"), "ponder")
        self.assertEqual(bob_core.get_role(self.cfg, "ponder", pro=True), "ponder-pro")
        self.assertEqual(bob_core.get_role(self.cfg, "agent"), "agent")

    def test_vision_uses_vision_section(self):
        self.assertEqual(bob_core.get_role(self.cfg, "vision"), "vision")
        self.assertEqual(bob_core.get_role(self.cfg, "vision", pro=True), "vision-pro")

    def test_unknown_task_raises(self):
        # A typo'd task must not silently route to the chat model.
        with self.assertRaises(ValueError):
            bob_core.get_role(self.cfg, "nonsense")

    def test_missing_routing_uses_hard_default(self):
        self.assertEqual(bob_core.get_role({}, "chat"), "chat")


class TestPortDefaults(unittest.TestCase):
    def test_default_used_when_absent(self):
        self.assertEqual(bob_core._port({}, "litellmPort"), 8081)

    def test_config_value_overrides(self):
        self.assertEqual(bob_core._port({"litellmPort": 9999}, "litellmPort"), 9999)

    def test_unknown_key_raises(self):
        with self.assertRaises(KeyError):
            bob_core._port({}, "nope")


def _view(profile, roles, peers=None, parallel=1):
    return mock.patch.object(bob_core, "_models_view", return_value=(
        {"defaults": {"parallel": parallel}, "peers": peers or {}}, profile, roles))


class TestRoleWindow(unittest.TestCase):
    def test_slot_split_without_kv_unified(self):
        spec = {"ctx": 393216, "flags": ["--parallel", "2", "--no-kv-unified"]}
        self.assertEqual(bob_core.slot_ctx(spec, {"parallel": 1}), 196608)

    def test_kv_unified_keeps_the_whole_ctx(self):
        spec = {"ctx": 8192, "flags": ["--parallel", "2", "--kv-unified"]}
        self.assertEqual(bob_core.slot_ctx(spec, {}), 8192)

    def test_local_role_window(self):
        with _view("cpu", {"chat": {"ctx": 4096}}):
            self.assertEqual(bob_core.role_window({}, "chat"), 4096)
            self.assertEqual(bob_core.role_window({}, "nope"), 0)

    def test_pro_role_window_from_peer(self):
        peers = {"d": {"contextWindow": 1000000, "pro": {"chat": {"model": "x"}}}}
        with _view("16gb", {"chat": {"ctx": 40960}}, peers):
            self.assertEqual(bob_core.role_window({}, "chat-pro"), 1000000)

    def test_real_profiles(self):
        # The registry's 32gb profile runs --parallel 2 without a unified KV cache: half the ctx per request.
        import bob_models
        mcfg = bob_models.load_models_config()
        spec = bob_models.profile_roles("32gb", config=mcfg)["chat"]
        self.assertEqual(bob_core.slot_ctx(spec, mcfg.get("defaults") or {}), 196608)


class TestServedRole(unittest.TestCase):
    def test_cpu_falls_back_to_chat_with_notice(self):
        seen = []
        with _view("cpu", {"chat": {}, "writer": {}, "agent": {}}):
            self.assertEqual(bob_core.served_role({}, "coder", notice=seen.append), "chat")
            self.assertEqual(bob_core.served_role({}, "ponder", notice=seen.append), "chat")
            self.assertEqual(bob_core.served_role({}, "agent", notice=seen.append), "agent")
        self.assertEqual(len(seen), 2)
        self.assertIn("cpu", seen[0])

    def test_pro_roles_pass_through(self):
        with _view("cpu", {"chat": {}}):
            self.assertEqual(bob_core.served_role({}, "coder-pro", notice=lambda m: None), "coder-pro")


class TestImageRefusal(unittest.TestCase):
    def test_disabled(self):
        self.assertIn("vision.enabled", bob_core.image_refusal({"vision": {"enabled": False}}, "vision"))

    def test_profile_without_vision(self):
        with _view("12gb", {"chat": {}}):
            self.assertIn("12gb", bob_core.image_refusal({}, "vision"))

    def test_pro_peer_without_images(self):
        peers = {"d": {"pro": {"vision": {"model": "flash"}}}}
        with _view("16gb", {"vision": {"supportsVision": True}}, peers):
            self.assertIn("takes no images", bob_core.image_refusal({}, "vision-pro"))
            self.assertIsNone(bob_core.image_refusal({}, "vision"))

    def test_a_pinned_text_only_role_is_refused(self):
        roles = {"vision": {"supportsVision": True}, "coder": {}, "chat": {"mmproj": "mm.gguf"}}
        with _view("16gb", roles):
            self.assertIn("text-only", bob_core.image_refusal({}, "coder"))
            self.assertIsNone(bob_core.image_refusal({}, "chat"))     # an mmproj makes it vision-capable
            self.assertIsNone(bob_core.image_refusal({}, "vision"))


class _Resp:
    def __init__(self, content, finish="stop"):
        from types import SimpleNamespace
        self.choices = [SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish)]


class TestComplete(unittest.TestCase):
    def _client(self, calls, resp=None, exc=None):
        from types import SimpleNamespace

        class _C:
            chat = SimpleNamespace(completions=SimpleNamespace())

        def create(**kw):
            calls.append(kw)
            if exc:
                raise exc
            return resp or _Resp("ok")
        _C.chat.completions.create = create
        return _C()

    def test_fits_input_and_disables_thinking(self):
        calls = []
        big = "x" * 40000
        with _view("cpu", {"chat": {"ctx": 4096}}), \
                mock.patch.object(bob_core, "get_llm_client", return_value=self._client(calls)):
            text, finish = bob_core.complete({}, "chat", [{"role": "system", "content": "S"},
                                                          {"role": "user", "content": big}], 512)
        self.assertEqual((text, finish), ("ok", "stop"))
        sent = calls[0]
        self.assertEqual(sent["max_tokens"], 512)
        self.assertEqual(sent["extra_body"], {"chat_template_kwargs": {"enable_thinking": False}})
        total = sum(bob_core.est_tokens(m["content"]) + 4 for m in sent["messages"])
        self.assertLessEqual(total + 512, 4096)
        self.assertEqual(sent["messages"][0]["content"], "S")

    def test_failure_raises_clearly(self):
        with _view("16gb", {"chat": {"ctx": 40960}}), \
                mock.patch.object(bob_core, "get_llm_client",
                                  return_value=self._client([], exc=RuntimeError("down"))):
            with self.assertRaises(bob_core.CompletionError) as ctx:
                bob_core.complete({}, "chat", [{"role": "user", "content": "hi"}], 64)
        self.assertIn("chat", str(ctx.exception))

    def test_role_falls_back_on_cpu(self):
        calls = []
        with _view("cpu", {"chat": {"ctx": 4096}}), \
                mock.patch.object(bob_core, "get_llm_client", return_value=self._client(calls)), \
                mock.patch("sys.stderr"):
            bob_core.complete({}, "ponder", [{"role": "user", "content": "hi"}], 64)
        self.assertEqual(calls[0]["model"], "chat")


class TestLitellmKey(unittest.TestCase):
    def test_generated_when_unset_and_stable(self):
        import osenv
        with mock.patch.object(osenv, "ensure_secret", return_value="sk-bob-gen") as ens:
            self.assertEqual(bob_core._litellm_key({"litellmKey": ""}), "sk-bob-gen")
        ens.assert_called_once()
        # Really generated and persisted: two resolutions agree and it is not the old fixed key.
        a, b = bob_core._litellm_key({}), bob_core._litellm_key({})
        self.assertEqual(a, b)
        self.assertNotEqual(a, "sk-local")

    def test_explicit_config_wins_over_a_stored_key(self):
        bob_core._litellm_key({})                            # a generated key is now stored
        self.assertEqual(bob_core._litellm_key({"litellmKey": "sk-mine"}), "sk-mine")

    def test_env_wins(self):
        with mock.patch.dict("os.environ", {"BOB_LITELLMKEY": "sk-env"}):
            self.assertEqual(bob_core._litellm_key({"litellmKey": "sk-mine"}), "sk-env")

    def test_accepted_tokens_drop_the_key_on_request(self):
        import bob_authstore
        cfg = {"litellmKey": "sk-mine", "agent": {"acceptLitellmKey": False, "apiTokens": ["t1"]}}
        with mock.patch.dict("os.environ", {}, clear=False):
            self.assertEqual(set(bob_authstore.config_token_owners(cfg)), {"t1"})


class TestSharedHelpers(unittest.TestCase):
    def test_est_tokens(self):
        self.assertEqual(bob_core.est_tokens(""), 0)
        self.assertEqual(bob_core.est_tokens("abcd"), 1)
        self.assertEqual(bob_core.est_tokens("abcde"), 2)
        self.assertEqual(bob_core.tokens_to_chars(10), 40)

    def test_service_port_sections(self):
        self.assertEqual(bob_core.service_port({"agent": {"agentPort": 9999}}, "agentPort"), 9999)
        self.assertEqual(bob_core.service_port({"litellmPort": 9000}, "litellmPort"), 9000)

    def test_stack_reuses_the_accessor(self):
        import stack
        self.assertIs(stack.service_port, bob_core.service_port)
        # Every SERVICES port_section agrees with the accessor's section map.
        for svc in stack.SERVICES:
            if svc.get("port_section"):
                self.assertEqual(bob_core._SECTION_PORTS.get(svc["port"]), svc["port_section"], svc["name"])

    def test_state_path_under_data_dir(self):
        import osenv
        self.assertEqual(bob_core.state_path("data/sessions.db"), osenv.data_dir() / "sessions.db")
        self.assertEqual(bob_core.state_path("logs/bob-agent.log"), osenv.cache_dir() / "bob-agent.log")
        self.assertEqual(str(bob_core.state_path("/abs/x.db")), "/abs/x.db")

    def test_check_litellm_uses_osenv(self):
        import osenv
        with mock.patch.object(osenv, "is_port_in_use", return_value=True) as p:
            self.assertTrue(bob_core.check_litellm({"litellmPort": 8123}))
        p.assert_called_once_with(8123)

    def test_voice_disabled(self):
        self.assertIsNone(bob_core.voice_disabled({}))
        self.assertIn("voice.enabled", bob_core.voice_disabled({"voice": {"enabled": False}}))


if __name__ == "__main__":
    unittest.main()
