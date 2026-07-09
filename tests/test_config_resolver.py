"""The Python runtime-config resolver produces every runtime key the core
reads, from neutral sources; and bob_core.load_config falls back to it when
data/config.json is absent. Rule: the runtime receives every runtime key it needs, correctly."""
import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import _common  # noqa: F401 — puts scripts/ on sys.path
import bob_config
import bob_core

# Provisioner keys the runtime must never need — the resolver must not emit these.
_PROVISIONER_KEYS = {"profiles", "peers", "activeProfile", "toastAppId", "defaults"}


class TestResolver(unittest.TestCase):
    def test_all_runtime_keys_present(self):
        cfg = bob_config.resolve_runtime_config()
        for key in ("port", "litellmPort", "searxngPort", "litellmKey", "routing",
                    "persona", "agent", "memory", "vision"):
            self.assertIn(key, cfg, f"missing runtime key: {key}")
        # routing resolves to real role values (derived from the shared roleTable)
        self.assertEqual(cfg["routing"]["defaultRole"], "chat")
        self.assertEqual(cfg["routing"]["proCodeRole"], "coder-pro")
        self.assertEqual(cfg["routing"]["agentRole"], "agent")
        # persona.systemPrompt is a non-empty string
        self.assertTrue(cfg["persona"]["systemPrompt"])
        # ownership keys the agent server reads
        self.assertIn("apiTokens", cfg["agent"])
        self.assertEqual(cfg["agent"]["defaultOwner"], "local")
        # agentPort lives under agent (where bob_agent_server._port reads it)
        self.assertEqual(bob_core._port(cfg["agent"], "agentPort"),
                         bob_core.load_defaults()["ports"]["agentPort"])
        # get_role works against the resolved config exactly as against config.json
        self.assertEqual(bob_core.get_role(cfg, "code", pro=True), "coder-pro")

    def test_no_provisioner_keys(self):
        cfg = bob_config.resolve_runtime_config()
        self.assertEqual(_PROVISIONER_KEYS & set(cfg), set())
        self.assertNotIn("toastAppId", cfg["agent"])  # retired from runtime

    def test_allowed_read_paths_defaults_to_repo(self):
        cfg = bob_config.resolve_runtime_config()
        self.assertEqual(cfg["agent"]["allowedReadPaths"], [str(bob_core.REPO)])

    def test_user_override_deep_merges(self):
        d = Path(tempfile.mkdtemp(prefix="bob-user-"))
        user = d / "user.json"
        user.write_text(json.dumps({
            "agent": {"maxSteps": 3, "serveHost": "0.0.0.0"},
            "routing": {"defaultRole": "myrole"},
            "litellmKey": "sk-override",
        }), encoding="utf-8")
        cfg = bob_config.resolve_runtime_config(user_path=user)
        self.assertEqual(cfg["agent"]["maxSteps"], 3)             # overridden
        self.assertEqual(cfg["agent"]["serveHost"], "0.0.0.0")    # overridden
        self.assertEqual(cfg["agent"]["defaultOwner"], "local")   # base kept (deep merge)
        self.assertEqual(cfg["routing"]["defaultRole"], "myrole")
        self.assertEqual(cfg["routing"]["proRole"], "chat-pro")   # base kept
        self.assertEqual(cfg["litellmKey"], "sk-override")

    def test_user_overlay_catalog_sections_do_not_leak_into_runtime(self):
        # user.json is shared with the model-registry resolver, so it may carry catalog-only sections
        # (profiles/peers/defaults/...). Those must NOT appear in the runtime config; the runtime keys
        # alongside them still apply.
        d = Path(tempfile.mkdtemp(prefix="bob-user-cat-"))
        user = d / "user.json"
        user.write_text(json.dumps({
            "memory": {"rerank": True},
            "profiles": {"16gb": {"rerank": {"reranking": True, "gguf": "x.gguf"}}},
            "peers": {"deepseek": {}}, "defaults": {"ngl": 80}, "prompts": {"chat": "hi"},
        }), encoding="utf-8")
        cfg = bob_config.resolve_runtime_config(user_path=user)
        for k in ("profiles", "peers", "defaults", "prompts"):
            self.assertNotIn(k, cfg, f"catalog section {k} leaked into runtime cfg")
        self.assertTrue(cfg["memory"]["rerank"])                  # runtime key alongside them still applied


class TestUserOverlayLoader(unittest.TestCase):
    """The ONE user-overlay loader (bob_config.load_user_overlay), shared by the runtime resolver
    and the model registry: one parse, one error policy (a bad overlay is ignored, never fatal)."""

    def test_absent_returns_empty(self):
        self.assertEqual(bob_config.load_user_overlay(Path("/no/such/user.json")), {})

    def test_malformed_json_is_tolerated(self):
        d = Path(tempfile.mkdtemp(prefix="bob-baduser-"))
        bad = d / "user.json"
        bad.write_text("{ not valid json", encoding="utf-8")
        # The loader swallows it (returns {}) rather than raising...
        self.assertEqual(bob_config.load_user_overlay(bad), {})
        # ...so a malformed overlay never breaks the whole runtime resolve (previously this raised).
        cfg = bob_config.resolve_runtime_config(user_path=bad)
        self.assertIn("routing", cfg)

    def test_reads_json_overlay(self):
        d = Path(tempfile.mkdtemp(prefix="bob-user-"))
        f = d / "user.json"
        f.write_text(json.dumps({"litellmKey": "sk-x"}), encoding="utf-8")
        self.assertEqual(bob_config.load_user_overlay(f), {"litellmKey": "sk-x"})

    def test_models_registry_uses_the_shared_loader(self):
        # bob_models.load_models_config must merge the overlay via the SAME loader — assert it delegates
        # (not a private re-read) so the parse/error policy can't drift. bob_models binds the name at
        # import, so patch it there. `_s1_marker` is a top-level key nothing else touches.
        import bob_models
        with unittest.mock.patch.object(bob_models, "load_user_overlay",
                                        return_value={"_s1_marker": "sentinel"}) as m:
            cfg = bob_models.load_models_config()
        self.assertTrue(m.called)
        self.assertEqual(cfg["_s1_marker"], "sentinel")


class TestCapabilityProbe(unittest.TestCase):
    """The provisioner readiness probe: degrades with a clear message, never assumes setup ran."""

    def test_endpoint_down_reports_clearly(self):
        cfg = bob_config.resolve_runtime_config()
        with unittest.mock.patch.object(bob_core, "check_litellm", return_value=False):
            ok, msg = bob_core.capability_probe(cfg)
        self.assertFalse(ok)
        self.assertIn("not reachable", msg)
        self.assertIn(str(cfg["litellmPort"]), msg)

    def test_endpoint_up_reports_ok(self):
        cfg = bob_config.resolve_runtime_config()
        with unittest.mock.patch.object(bob_core, "check_litellm", return_value=True):
            ok, msg = bob_core.capability_probe(cfg)
        self.assertTrue(ok)


class TestLoadConfigFallback(unittest.TestCase):
    def test_falls_back_to_resolver_when_config_absent(self):
        orig = bob_core.REPO
        try:
            # Point REPO at an empty dir so data/config.json doesn't exist; the resolver still
            # reads the real config/defaults.json via its own module-level path.
            bob_core.REPO = Path(tempfile.mkdtemp(prefix="bob-noconf-"))
            cfg = bob_core.load_config()
            self.assertIn("routing", cfg)
            self.assertIn("agent", cfg)
            self.assertTrue(cfg["persona"]["systemPrompt"])
        finally:
            bob_core.REPO = orig


if __name__ == "__main__":
    unittest.main()
