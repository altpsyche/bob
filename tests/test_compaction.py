"""Context compaction: `compaction='summarize'` replaces the dropped oldest span with one
structured note (reusing the summarizer core), keeps the last K verbatim, reserves budget so it can't
re-overflow, and falls back to plain truncation when no summary is produced. `truncate` is the plain-truncation baseline."""
import unittest

import _common  # noqa: F401 — sys.path
import bob_loop
from bob_loop import truncate_history, _COMPACT_FRAME, _message_tokens


def _history(n=10):
    """system + n alternating user/assistant turns with distinguishable content."""
    msgs = [{"role": "system", "content": "SYS-PROMPT"}]
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        msgs.append({"role": role, "content": f"turn-{i} " + "x" * 200})   # ~51 tokens each
    return msgs


class TestTruncateModeUnchanged(unittest.TestCase):
    def test_default_is_truncate_drop_oldest(self):
        msgs = _history(10)
        out = truncate_history(msgs, max_msgs=5)          # default compaction='truncate'
        self.assertEqual(out[0], {"role": "system", "content": "SYS-PROMPT"})
        self.assertNotIn(_COMPACT_FRAME, out[1].get("content", ""))   # no note inserted
        self.assertEqual(out[-4:], msgs[-4:])              # newest kept, oldest dropped

    def test_system_preserved_both_modes(self):
        msgs = _history(8)
        for mode in ("truncate", "summarize"):
            out = truncate_history(msgs, max_msgs=4, compaction=mode)
            self.assertTrue(any(m["role"] == "system" and m["content"] == "SYS-PROMPT" for m in out))


class TestSummarizeMode(unittest.TestCase):
    def setUp(self):
        self._orig = bob_loop._compact_span

    def tearDown(self):
        bob_loop._compact_span = self._orig

    def test_summarize_inserts_note_and_keeps_last_k(self):
        seen = {}

        def fake_compact(dropped, model, max_tokens):
            seen["dropped"] = dropped
            return "GOAL: do X\n- decided Y"

        bob_loop._compact_span = fake_compact
        msgs = _history(10)
        out = truncate_history(msgs, max_msgs=5, compaction="summarize", keep_last=2)
        # system prompt first, compaction note second, recent tail verbatim after.
        self.assertEqual(out[0]["content"], "SYS-PROMPT")
        self.assertIn(_COMPACT_FRAME, out[1]["content"])
        self.assertIn("GOAL: do X", out[1]["content"])
        self.assertEqual(out[-4:], msgs[-4:])              # window kept 4; note replaced the rest
        # the dropped span (older turns) was handed to the summarizer, not silently lost.
        self.assertTrue(any("turn-0" in m["content"] for m in seen["dropped"]))

    def test_empty_summary_falls_back_to_truncate(self):
        bob_loop._compact_span = lambda dropped, model, max_tokens: ""   # summarizer failed / no LLM
        msgs = _history(10)
        out = truncate_history(msgs, max_msgs=5, compaction="summarize", keep_last=2)
        self.assertFalse(any(_COMPACT_FRAME in m.get("content", "") for m in out))  # no note
        self.assertEqual(out[0]["content"], "SYS-PROMPT")
        self.assertEqual(out[-4:], msgs[-4:])              # behaves like plain truncation

    def test_nothing_to_drop_returns_unchanged(self):
        bob_loop._compact_span = lambda *a: self.fail("must not summarize when nothing is dropped")
        msgs = _history(3)                                  # well under the window
        out = truncate_history(msgs, max_msgs=40, compaction="summarize", keep_last=6)
        self.assertEqual(out, msgs)

    def test_summary_reserves_budget_no_reoverflow(self):
        bob_loop._compact_span = lambda dropped, model, max_tokens: "NOTE " + "y" * 80
        msgs = _history(20)
        budget = 400
        out = truncate_history(msgs, max_msgs=100, max_tokens=budget, compaction="summarize",
                               keep_last=2, summary_max_tokens=60)
        total = sum(_message_tokens(m) for m in out)
        self.assertLessEqual(total, budget, f"compacted total {total} exceeded budget {budget}")
        self.assertTrue(any(_COMPACT_FRAME in m.get("content", "") for m in out))


if __name__ == "__main__":
    unittest.main()
