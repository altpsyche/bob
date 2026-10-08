"""Provider API keys: osenv.store_secret/delete_secret/secret_source, and bob.keys (the core `bob key` and
the shell's /key share). Runs against a temp data dir with the OS keychain disabled, and a stub registry,
so no real key or config is touched."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _common  # noqa: F401: puts scripts/ on sys.path
import osenv
from bob import keys

MCFG = {"peers": {
    "deepseek": {"enabled": True, "apiKeyEnv": "DEEPSEEK_API_KEY",
                 "pro": {"chat": {"model": "x"}, "coder": {"model": "y"}}},
    "zhipu": {"enabled": False, "apiKeyEnv": "ZHIPU_API_KEY", "pro": {"coder": {"model": "glm"}}},
}}


class _SecretsTmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / ".migrated").write_text("", encoding="utf-8")   # never copy the real data/ secrets
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {"BOB_DATA_DIR": str(self.tmp)})
        env.start()
        self.addCleanup(env.stop)
        for k in ("DEEPSEEK_API_KEY", "ZHIPU_API_KEY", "BOB_DEEPSEEK_API_KEY", "BOB_ZHIPU_API_KEY"):
            os.environ.pop(k, None)
        kr = mock.patch.dict(sys.modules, {"keyring": None})   # never touch the real OS keychain
        kr.start()
        self.addCleanup(kr.stop)


class TestStoreSecret(_SecretsTmp):
    def test_store_keeps_other_secrets_and_is_private(self):
        osenv.ensure_secret("other")
        osenv.store_secret("DEEPSEEK_API_KEY", "sk-1")
        osenv.store_secret("DEEPSEEK_API_KEY", "sk-2")
        data = json.loads(osenv.secrets_file().read_text())
        self.assertEqual(data["DEEPSEEK_API_KEY"], "sk-2")
        self.assertIn("other", data)
        if osenv.os_name() != "windows":
            self.assertEqual(osenv.secrets_file().stat().st_mode & 0o777, 0o600)

    def test_empty_value_refused(self):
        with self.assertRaises(ValueError):
            osenv.store_secret("DEEPSEEK_API_KEY", "")

    def test_delete(self):
        self.assertFalse(osenv.delete_secret("DEEPSEEK_API_KEY"))
        osenv.store_secret("DEEPSEEK_API_KEY", "sk-1")
        self.assertTrue(osenv.delete_secret("DEEPSEEK_API_KEY"))
        self.assertIsNone(osenv.secret("DEEPSEEK_API_KEY"))

    def test_source_follows_secret_precedence(self):
        self.assertIsNone(osenv.secret_source("DEEPSEEK_API_KEY"))
        osenv.store_secret("DEEPSEEK_API_KEY", "sk-file")
        self.assertEqual(osenv.secret_source("DEEPSEEK_API_KEY"), "secrets.json")
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-env"}):
            self.assertEqual(osenv.secret_source("DEEPSEEK_API_KEY"), "env")

    def test_corrupt_file_never_overwritten(self):
        osenv.secrets_file().write_text("{not json")
        with self.assertRaises(osenv.SecretsFileCorrupt):
            osenv.store_secret("DEEPSEEK_API_KEY", "sk-1")


class TestKeys(_SecretsTmp):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(keys, "_models_config", side_effect=lambda: json.loads(json.dumps(MCFG)))
        p.start()
        self.addCleanup(p.stop)
        r = mock.patch.object(keys, "_restart_litellm", return_value="")
        self.restart = r.start()
        self.addCleanup(r.stop)

    def test_entries_list_peers_then_search_keys(self):
        rows = keys.entries()
        self.assertEqual([r["name"] for r in rows][:2], ["deepseek", "zhipu"])
        self.assertIn("brave", [r["name"] for r in rows])
        ds = rows[0]
        self.assertEqual(ds["roles"], ["chat-pro", "coder-pro"])
        self.assertIsNone(ds["source"])

    def test_find_by_provider_or_env_name(self):
        self.assertEqual(keys.find("DeepSeek")["env"], "DEEPSEEK_API_KEY")
        self.assertEqual(keys.find("deepseek_api_key")["name"], "deepseek")
        self.assertIsNone(keys.find("nope"))

    def test_set_stores_and_restarts_proxy(self):
        lines = keys.set_key("deepseek", "  sk-abc\n", {})
        self.assertEqual(osenv.secret("DEEPSEEK_API_KEY"), "sk-abc")
        self.restart.assert_called_once()
        self.assertTrue(any("chat-pro" in ln for ln in lines))

    def test_set_disabled_peer_enables_it(self):
        with mock.patch.object(keys, "_enable_peer") as enable, \
                mock.patch("bob_models.regenerate_configs", return_value=True) as regen:
            lines = keys.set_key("zhipu", "zk", {})
        enable.assert_called_once_with("zhipu")
        regen.assert_called_once()
        self.assertTrue(any("Enabled peer 'zhipu'" in ln for ln in lines))
        self.assertTrue(any(ln.startswith("warning: deepseek also serves") for ln in lines))  # coder-pro

    def test_set_search_key_does_not_restart_proxy(self):
        keys.set_key("brave", "bk", {})
        self.assertEqual(osenv.secret("braveApiKey"), "bk")
        self.restart.assert_not_called()

    def test_set_warns_when_env_shadows(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-env"}):
            lines = keys.set_key("deepseek", "sk-file", {})
        self.assertTrue(any(ln.startswith("warning:") for ln in lines))

    def test_set_rejects_unknown_and_empty(self):
        with self.assertRaises(ValueError):
            keys.set_key("nope", "x", {})
        with self.assertRaises(ValueError):
            keys.set_key("deepseek", "   ", {})

    def test_remove(self):
        keys.set_key("deepseek", "sk-abc", {})
        self.restart.reset_mock()
        lines = keys.remove_key("deepseek", {})
        self.assertIsNone(osenv.secret("DEEPSEEK_API_KEY"))
        self.assertTrue(lines[0].startswith("Removed"))
        self.restart.assert_called_once()

    def test_peer_key_env_skips_disabled_and_falls_back_to_config(self):
        osenv.store_secret("ZHIPU_API_KEY", "zk")              # disabled peer: never passed
        mcfg = json.loads(json.dumps(MCFG))
        mcfg["peers"]["deepseek"]["apiKey"] = "sk-config"
        self.assertEqual(keys.peer_key_env(mcfg), {"DEEPSEEK_API_KEY": "sk-config"})
        osenv.store_secret("DEEPSEEK_API_KEY", "sk-stored")   # the secret store wins over config
        self.assertEqual(keys.peer_key_env(mcfg), {"DEEPSEEK_API_KEY": "sk-stored"})


class TestLiteLLMLaunchEnv(unittest.TestCase):
    def test_peer_keys_reach_the_proxy_environment(self):
        import stack
        with mock.patch.object(stack, "_osenv") as o, \
                mock.patch.object(stack, "_read_pid", return_value=None), \
                mock.patch.object(stack, "_litellm_key", return_value="mk"), \
                mock.patch.object(stack, "_langfuse_env", return_value={}), \
                mock.patch.object(stack, "_peer_key_env", return_value={"DEEPSEEK_API_KEY": "sk"}):
            o.return_value.venv_exe.return_value = mock.Mock(exists=lambda: True)
            o.return_value.start_detached.return_value = 1
            stack._start_litellm_bg({})
        env = o.return_value.start_detached.call_args.kwargs["env"]
        self.assertEqual(env["DEEPSEEK_API_KEY"], "sk")


if __name__ == "__main__":
    unittest.main()
