"""O6 — permission / approval policy: PermissionPolicy resolution + enforcement at the dispatch
choke point (_dispatch_with_approval) through run_agent_events. Fake client + FakeRegistry; no model."""
import unittest

import _common
import bob_core
import bob_loop
from bob_permissions import ALLOW, ASK, DENY, PermissionPolicy


def _agent_cfg(permissions):
    """A fake_config whose agent block carries the O6 permissions plus the keys the loop reads."""
    return _common.fake_config(agent={
        "toolFormat": "hermes", "maxSteps": 5,
        "maxContextTokens": 0, "maxToolResultTokens": 1000,
        "permissions": permissions,
    })


ECHO_CALL = '<tool_call>{"name": "echo", "arguments": {"x": "hi"}}</tool_call>'


class TestPermissionPolicyUnit(unittest.TestCase):
    """PermissionPolicy is pure — resolve() without a registry or model."""

    def test_empty_config_allows_everything(self):
        p = PermissionPolicy({})
        self.assertFalse(p.configured)
        self.assertEqual(p.resolve("shell_run", mutating=True), ALLOW)
        self.assertEqual(p.resolve("anything"), ALLOW)

    def test_per_tool_beats_class_default(self):
        p = PermissionPolicy({"agent": {"permissions": {
            "mutating": ASK, "tools": {"file_write": DENY}}}})
        self.assertEqual(p.resolve("file_write", mutating=True), DENY)   # per-tool wins
        self.assertEqual(p.resolve("memory_store", mutating=True), ASK)  # class default
        self.assertEqual(p.resolve("file_read", mutating=False), ALLOW)  # no read rule -> allow

    def test_read_and_mutating_class_defaults(self):
        p = PermissionPolicy({"agent": {"permissions": {"read": ASK, "mutating": DENY}}})
        self.assertEqual(p.resolve("web_fetch", mutating=False), ASK)
        self.assertEqual(p.resolve("shell_run", mutating=True), DENY)

    def test_precedence_depth_over_owner_over_top(self):
        perms = {
            "tools": {"echo": ALLOW},
            "perOwner": {"guest": {"tools": {"echo": ASK}}},
            "perDepth": {"1": {"tools": {"echo": DENY}}},
        }
        p = PermissionPolicy({"agent": {"permissions": perms}})
        self.assertEqual(p.resolve("echo", owner="local", agent_depth=0), ALLOW)  # top
        self.assertEqual(p.resolve("echo", owner="guest", agent_depth=0), ASK)    # owner override
        self.assertEqual(p.resolve("echo", owner="guest", agent_depth=1), DENY)   # depth wins over owner

    def test_unknown_mode_falls_through_to_allow(self):
        p = PermissionPolicy({"agent": {"permissions": {"tools": {"echo": "bogus"}}}})
        self.assertEqual(p.resolve("echo"), ALLOW)


class TestEnforcement(unittest.TestCase):
    """Enforcement through the real loop (run_agent_events), fake client + FakeRegistry."""

    def setUp(self):
        self._orig_check = bob_core.check_litellm
        self._orig_client = bob_core.get_llm_client
        bob_core.check_litellm = lambda config=None: True
        # step 1 asks for the echo tool, step 2 is a final answer.
        bob_core.get_llm_client = lambda config=None: _common.scripted_client([ECHO_CALL, "done."])

    def tearDown(self):
        bob_core.check_litellm = self._orig_check
        bob_core.get_llm_client = self._orig_client

    def _run(self, permissions, *, approve=None, owner="local", agent_depth=0,
             mutating=None, results=None):
        reg = _common.FakeRegistry(results or {"echo": "REAL RESULT"}, mutating_tools=mutating)
        events = list(bob_loop.run_agent_events(
            "go", _agent_cfg(permissions), agency="silent", registry=reg,
            approve=approve, owner=owner, agent_depth=agent_depth))
        return reg, events

    def _tool_result(self, events):
        return [e for e in events if e["type"] == "tool_result"][0]["result"]

    def test_deny_never_dispatches(self):
        reg, events = self._run({"tools": {"echo": DENY}})
        self.assertNotIn("echo", reg.dispatched)                      # never ran
        self.assertIn("denied by policy", self._tool_result(events))
        self.assertNotIn("REAL RESULT", self._tool_result(events))

    def test_ask_runs_only_on_approval(self):
        approved, events = self._run({"tools": {"echo": ASK}}, approve=lambda a: True)
        self.assertIn("echo", approved.dispatched)
        self.assertEqual(self._tool_result(events), "REAL RESULT")
        self.assertTrue(any(e["type"] == "approval_required" for e in events))

    def test_ask_denied_when_approver_refuses(self):
        reg, events = self._run({"tools": {"echo": ASK}}, approve=lambda a: False)
        self.assertNotIn("echo", reg.dispatched)
        self.assertIn("denied by the user", self._tool_result(events))

    def test_ask_fail_closed_without_approver(self):
        # No approve callback (server/scheduler/non-TTY) -> ask fails closed to deny.
        reg, events = self._run({"tools": {"echo": ASK}}, approve=None)
        self.assertNotIn("echo", reg.dispatched)
        self.assertIn("denied by the user", self._tool_result(events))

    def test_per_owner_override(self):
        perms = {"perOwner": {"guest": {"tools": {"echo": DENY}}}}
        guest_reg, guest_ev = self._run(perms, owner="guest")
        self.assertNotIn("echo", guest_reg.dispatched)
        self.assertIn("denied by policy", self._tool_result(guest_ev))
        local_reg, local_ev = self._run(perms, owner="local")
        self.assertIn("echo", local_reg.dispatched)                    # no rule for local -> allow
        self.assertEqual(self._tool_result(local_ev), "REAL RESULT")

    def test_per_depth_override_on_mutating(self):
        perms = {"perDepth": {"1": {"mutating": DENY}}}
        sub_reg, sub_ev = self._run(perms, agent_depth=1, mutating={"echo"})
        self.assertNotIn("echo", sub_reg.dispatched)                   # sub-agent (depth 1) denied
        root_reg, root_ev = self._run(perms, agent_depth=0, mutating={"echo"})
        self.assertIn("echo", root_reg.dispatched)                     # root unaffected
        self.assertEqual(self._tool_result(root_ev), "REAL RESULT")

    def test_empty_policy_reproduces_today(self):
        # No permissions + silent agency + not approval-required -> runs with no approval event.
        reg, events = self._run({})
        self.assertIn("echo", reg.dispatched)
        self.assertEqual(self._tool_result(events), "REAL RESULT")
        self.assertFalse(any(e["type"] == "approval_required" for e in events))

    def test_ne0_floor_preserved_under_empty_policy(self):
        # A tool that self-declares REQUIRES_APPROVAL still asks even with an empty policy.
        reg = _common.FakeRegistry({"echo": "REAL RESULT"}, approval_required_tools={"echo"})
        events = list(bob_loop.run_agent_events(
            "go", _agent_cfg({}), agency="silent", registry=reg, approve=lambda a: True))
        self.assertTrue(any(e["type"] == "approval_required" for e in events))
        self.assertIn("echo", reg.dispatched)

    def test_audit_line_records_decision(self):
        with self.assertLogs("bob.agent", level="INFO") as cm:
            self._run({"tools": {"echo": DENY}})
        audit = [ln for ln in cm.output if "AUDIT" in ln and "tool=echo" in ln]
        self.assertTrue(audit, f"no AUDIT line emitted: {cm.output}")
        self.assertIn("decision=deny", audit[0])
        self.assertIn("owner=local", audit[0])


if __name__ == "__main__":
    unittest.main()
