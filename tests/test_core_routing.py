"""bob_core routing + port defaults."""
import unittest

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

    def test_unknown_task_falls_back_to_chat(self):
        self.assertEqual(bob_core.get_role(self.cfg, "nonsense"), "chat")

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


if __name__ == "__main__":
    unittest.main()
