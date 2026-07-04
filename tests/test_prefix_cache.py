"""O13 — prefix-cache-aware context (`stablePrefix`, default off). truncate_history freezes a head
(system + append-only summary block + pinned goal) so llama.cpp's KV prefix cache is reused across
turns instead of being busted every turn. Default `stable_prefix=False` reproduces pre-O13 exactly.
Also confirms the loop never disables llama.cpp's cache_prompt on a request."""
import json
import unittest
from types import SimpleNamespace

import _common  # noqa: F401 — sys.path
import bob_core
import bob_loop
from bob_loop import truncate_history, _COMPACT_FRAME, _message_tokens


def _serialize(msgs):
    """Canonical bytes of a message list — what the shared-prefix comparison is really about."""
    return json.dumps(msgs, sort_keys=True, ensure_ascii=False)


def _history(n=10, big=200):
    msgs = [{"role": "system", "content": "SYS-PROMPT"}]
    goal = {"role": "user", "content": "GOAL: do the big task"}
    msgs.append(goal)
    for i in range(n):
        role = "assistant" if i % 2 == 0 else "user"
        msgs.append({"role": role, "content": f"turn-{i} " + "x" * big})
    return msgs, goal


class TestStablePrefixByteStability(unittest.TestCase):
    """The core O13 guarantee: given NO compaction event, the serialized prefix is byte-identical
    across two consecutive turns — only the tail grows."""

    def test_prefix_identical_across_turns_no_compaction(self):
        msgs, goal = _history(4)
        t1 = truncate_history(msgs, max_msgs=40, max_tokens=0, compaction="summarize",
                              keep_last=6, stable_prefix=True, pin_goal=goal)
        # A normal step appends an assistant + user exchange; still well under any window -> no drop.
        msgs2 = list(t1) + [{"role": "assistant", "content": "step reply"},
                            {"role": "user", "content": "next"}]
        t2 = truncate_history(msgs2, max_msgs=40, max_tokens=0, compaction="summarize",
                              keep_last=6, stable_prefix=True, pin_goal=goal)
        # No compaction fired.
        self.assertFalse(any(_COMPACT_FRAME in m.get("content", "") for m in t2))
        # The whole of turn-1's list is an unchanged PREFIX of turn-2's -> KV cache reused.
        self.assertEqual(t2[:len(t1)], t1)
        self.assertTrue(_serialize(t2).startswith(_serialize(t1)[:-1]))  # -1 drops the closing ']'

    def test_goal_and_system_are_the_frozen_head(self):
        msgs, goal = _history(2)
        out = truncate_history(msgs, max_msgs=40, compaction="summarize", stable_prefix=True,
                               pin_goal=goal)
        self.assertEqual(out[0]["content"], "SYS-PROMPT")
        self.assertIs(out[1], goal)                      # goal pinned right after system


class TestGoalPinnedThroughCompaction(unittest.TestCase):
    def setUp(self):
        self._orig = bob_loop._compact_span

    def tearDown(self):
        bob_loop._compact_span = self._orig

    def test_goal_survives_a_compaction_event(self):
        bob_loop._compact_span = lambda dropped, model, max_tokens: "did A; decided B"
        msgs, goal = _history(12)
        out = truncate_history(msgs, max_msgs=6, compaction="summarize", keep_last=2,
                               stable_prefix=True, pin_goal=goal)
        # System first, then the compaction block, then the pinned goal — head frozen, goal never lost.
        self.assertEqual(out[0]["content"], "SYS-PROMPT")
        self.assertTrue(out[1]["content"].startswith(_COMPACT_FRAME))
        self.assertIs(out[2], goal)
        # The goal was never handed to the summarizer (it's pinned, not part of the dropped span).
        self.assertTrue(any(_message_tokens(m) for m in out))

    def test_goal_never_folded_into_the_summary(self):
        captured = {}

        def fake(dropped, model, max_tokens):
            captured["dropped"] = dropped
            return "summary"

        bob_loop._compact_span = fake
        msgs, goal = _history(12)
        truncate_history(msgs, max_msgs=6, compaction="summarize", keep_last=2,
                         stable_prefix=True, pin_goal=goal)
        self.assertNotIn(goal, captured["dropped"])
        self.assertFalse(any("GOAL:" in m.get("content", "") for m in captured["dropped"]))


class TestSummaryBlockAppendsNotRegenerates(unittest.TestCase):
    """A second compaction event must APPEND to the existing block — its prior bytes are frozen so the
    KV prefix is reused up to the previous divergence rather than rewritten."""

    def setUp(self):
        self._orig = bob_loop._compact_span

    def tearDown(self):
        bob_loop._compact_span = self._orig

    def test_second_event_appends_and_keeps_prior_bytes(self):
        notes = iter(["NOTE-ONE", "NOTE-TWO"])
        bob_loop._compact_span = lambda dropped, model, max_tokens: next(notes)

        msgs, goal = _history(12)
        out1 = truncate_history(msgs, max_msgs=6, compaction="summarize", keep_last=2,
                                stable_prefix=True, pin_goal=goal)
        block1 = next(m for m in out1 if m.get("content", "").startswith(_COMPACT_FRAME))
        self.assertIn("NOTE-ONE", block1["content"])

        # Grow the tail so a second window pass drops more turns -> a second compaction event.
        grown = list(out1) + [{"role": "assistant", "content": "r " + "z" * 300},
                              {"role": "user", "content": "u " + "z" * 300},
                              {"role": "assistant", "content": "r2 " + "z" * 300}]
        out2 = truncate_history(grown, max_msgs=6, compaction="summarize", keep_last=2,
                                stable_prefix=True, pin_goal=goal)
        block2 = next(m for m in out2 if m.get("content", "").startswith(_COMPACT_FRAME))
        # Exactly one block (single canonical summary, not accumulation), and NOTE-ONE's bytes are the
        # unchanged prefix of the appended block.
        self.assertEqual(sum(1 for m in out2 if m.get("content", "").startswith(_COMPACT_FRAME)), 1)
        self.assertTrue(block2["content"].startswith(block1["content"]))
        self.assertIn("NOTE-TWO", block2["content"])


class TestDefaultOffReproducesToday(unittest.TestCase):
    """stable_prefix=False must be byte-identical to the pre-O13 path (both modes)."""

    def setUp(self):
        self._orig = bob_loop._compact_span

    def tearDown(self):
        bob_loop._compact_span = self._orig

    def test_truncate_mode_default_off_unchanged(self):
        msgs, goal = _history(10)
        base = truncate_history(list(msgs), max_msgs=5)
        with_flag_off = truncate_history(list(msgs), max_msgs=5, stable_prefix=False, pin_goal=goal)
        self.assertEqual(base, with_flag_off)

    def test_summarize_mode_default_off_unchanged(self):
        bob_loop._compact_span = lambda dropped, model, max_tokens: "N"
        msgs, goal = _history(10)
        base = truncate_history(list(msgs), max_msgs=5, compaction="summarize", keep_last=2)
        off = truncate_history(list(msgs), max_msgs=5, compaction="summarize", keep_last=2,
                               stable_prefix=False, pin_goal=goal)
        self.assertEqual(base, off)


class TestCachePromptNotDisabled(unittest.TestCase):
    """The loop must never send cache_prompt=False — llama.cpp's default (caching on) must stand."""

    def setUp(self):
        self.cfg = _common.fake_config()
        self._orig_check = bob_core.check_litellm
        self._orig_client = bob_core.get_llm_client
        bob_core.check_litellm = lambda config=None: True

    def tearDown(self):
        bob_core.check_litellm = self._orig_check
        bob_core.get_llm_client = self._orig_client

    def _recording_client(self, calls):
        def _content_chunk(text):
            return SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content=text, tool_calls=None))])

        class _Stream:
            def __iter__(self):
                yield _content_chunk("done")

            def close(self):
                pass

        class _C:
            def __init__(self):
                self.chat = SimpleNamespace(completions=self)

            def create(self, **kwargs):
                calls.append(kwargs)
                return _Stream()

        return _C()

    def _assert_no_cache_disable(self, cfg):
        calls = []
        bob_core.get_llm_client = lambda config=None: self._recording_client(calls)
        list(bob_loop.run_agent_events("go", cfg, agency="silent",
                                       registry=_common.FakeRegistry()))
        self.assertTrue(calls, "loop never called create()")
        for kw in calls:
            self.assertNotEqual(kw.get("cache_prompt", None), False)
            body = kw.get("extra_body") or {}
            self.assertNotEqual(body.get("cache_prompt", None), False)

    def test_cache_prompt_not_disabled_default(self):
        self._assert_no_cache_disable(self.cfg)

    def test_cache_prompt_not_disabled_with_stable_prefix(self):
        cfg = _common.fake_config()
        cfg["agent"] = dict(cfg["agent"], stablePrefix=True, compaction="summarize")
        self._assert_no_cache_disable(cfg)


if __name__ == "__main__":
    unittest.main()
