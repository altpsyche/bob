"""Token-aware context management."""
import unittest

import _common
import bob_loop


class TestTruncateHistory(unittest.TestCase):
    def test_count_window_keeps_system_and_recent(self):
        msgs = [{"role": "system", "content": "S"}]
        for i in range(10):
            msgs.append({"role": "user", "content": f"u{i}"})
        out = bob_loop.truncate_history(msgs, max_msgs=4, max_tokens=0)
        self.assertEqual(out[0]["role"], "system")
        self.assertLessEqual(len(out), 4)
        self.assertEqual(out[-1]["content"], "u9")  # most recent kept

    def test_token_budget_trims_oldest(self):
        msgs = [{"role": "system", "content": "S" * 40}]
        for _ in range(20):
            msgs.append({"role": "user", "content": "U" * 400})
        out = bob_loop.truncate_history(msgs, max_msgs=100, max_tokens=500)
        total = sum(bob_loop._message_tokens(m) for m in out)
        self.assertEqual(out[0]["role"], "system")
        self.assertLessEqual(total, 500 + bob_loop._message_tokens(msgs[-1]))
        self.assertGreater(len(out), 1)

    def test_orphan_leading_tool_dropped(self):
        msgs = [
            {"role": "system", "content": "s"},
            {"role": "tool", "content": "t", "tool_call_id": "x"},
            {"role": "user", "content": "u"},
        ]
        out = bob_loop.truncate_history(msgs, max_msgs=2, max_tokens=0)
        self.assertFalse(any(m["role"] == "tool" for m in out[1:2]))


class TestSchemaCompaction(unittest.TestCase):
    def _schemas(self, n):
        return [
            {"type": "function", "function": {
                "name": f"t{i}", "description": "d" * 200,
                "parameters": {"type": "object",
                               "properties": {"q": {"type": "string", "description": "x" * 100}},
                               "required": ["q"]}}}
            for i in range(n)
        ]

    def test_compact_drops_descriptions(self):
        compact = bob_loop._compact_schema(self._schemas(1)[0]["function"])
        self.assertNotIn("description", compact["parameters"]["properties"]["q"])
        self.assertEqual(compact["parameters"]["properties"]["q"]["type"], "string")

    def test_addendum_shrinks_past_threshold(self):
        full = bob_loop._hermes_tool_system_addendum(self._schemas(5), compact_after=12)
        compact = bob_loop._hermes_tool_system_addendum(self._schemas(20), compact_after=12)
        # 20 compact tools should not be 4x the size of 5 verbose ones.
        self.assertLess(len(compact), len(full) * 4)

    def test_estimate_tokens(self):
        self.assertEqual(bob_loop._estimate_tokens(""), 0)
        self.assertEqual(bob_loop._estimate_tokens("abcd"), 1)


if __name__ == "__main__":
    unittest.main()
