"""The skills registry (discover/validate/list), simple tool-sequence execution, and
sub-agent execution: a no-`steps` skill runs as an isolated `run_agent_events` sub-run (its prompt is
the manifest description + user args), surfacing through the same event stream as any agent turn.
Malformed manifests stay hard contract errors; the tool-sequence path keeps its original byte-identical output."""
import tempfile
import unittest
from pathlib import Path

import _common  # noqa: F401 — puts scripts/ on sys.path

try:  # skill manifests are YAML; without PyYAML the registry degrades to empty, so skip (not fail)
    import yaml  # noqa: F401
except ModuleNotFoundError as _e:  # pragma: no cover
    raise unittest.SkipTest(f"PyYAML not installed: {_e}")

import bob_core
from bob_skills import SkillRegistry


class _FakeToolReg:
    def __init__(self):
        self.calls = []

    def dispatch_call(self, name, args_json, context=None):
        self.calls.append((name, args_json))
        return f"[{name} ran]"


def _cfg():
    cfg = _common.fake_config()
    cfg["agent"] = dict(cfg["agent"], agency="silent")
    return cfg


class TestSkillRegistry(unittest.TestCase):
    def test_seed_skills_register_cleanly(self):
        reg = SkillRegistry.build()  # the real skills/ dir
        names = {s["name"] for s in reg.list()}
        self.assertIn("repo-brief", names)
        self.assertIn("web-research", names)
        self.assertEqual(reg.errors, [], f"seed skills failed to load: {reg.errors}")

    def test_tool_sequence_skill_runs_its_steps(self):
        fake = _FakeToolReg()
        out = SkillRegistry.build().run("repo-brief", fake)
        self.assertEqual([c[0] for c in fake.calls], ["git_status", "git_log"])
        self.assertIn("git_status", out)

    def test_tool_sequence_run_matches_run_events_final(self):
        """`run` is now a thin consumer of `run_events`; the steps path's assembled text
        must be byte-identical to the original output (the `final` event carries it)."""
        reg = SkillRegistry.build()
        blocking = reg.run("repo-brief", _FakeToolReg())
        finals = [e["result"] for e in reg.run_events("repo-brief", _FakeToolReg())
                  if e["type"] == "final"]
        self.assertEqual(len(finals), 1)
        self.assertEqual(blocking, finals[0])
        self.assertTrue(blocking.startswith("# skill: repo-brief"))

    def test_unknown_skill(self):
        self.assertIn("Unknown skill", SkillRegistry.build().run("nope", _FakeToolReg()))

    def test_broken_manifest_is_contract_error(self):
        d = Path(tempfile.mkdtemp(prefix="bob-skills-"))
        (d / "bad").mkdir()
        (d / "bad" / "skill.yaml").write_text("group: X\n", encoding="utf-8")  # no description
        reg = SkillRegistry.build(d)
        self.assertNotIn("bad", reg.skills)
        self.assertTrue(any(phase == "contract" for _, phase, _ in reg.errors))

    def test_compose_task_folds_in_user_args(self):
        self.assertEqual(SkillRegistry._compose_task({"description": "Do X"}, ""), "Do X")
        self.assertEqual(SkillRegistry._compose_task({"description": "Do X"}, "  on Y "),
                         "Do X\n\nInput: on Y")


class TestSubAgentExecution(unittest.TestCase):
    """A no-`steps` skill (web-research/code-review) runs end-to-end as an isolated sub-run,
    NOT the old 'requires Module O' stub. Drives the real `run_agent_events` with a scripted client."""

    def setUp(self):
        self._orig_check = bob_core.check_litellm
        self._orig_client = bob_core.get_llm_client
        bob_core.check_litellm = lambda config=None: True

    def tearDown(self):
        bob_core.check_litellm = self._orig_check
        bob_core.get_llm_client = self._orig_client

    def test_sub_agent_skill_runs_not_requires_module_o(self):
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(["a cited brief"])
        out = SkillRegistry.build().run("web-research", _common.FakeRegistry(),
                                        config=_cfg(), args="quantum computing")
        self.assertEqual(out, "a cited brief")
        self.assertNotIn("Module O", out)

    def test_run_events_surfaces_sub_run_stream(self):
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(["a cited brief"])
        evs = list(SkillRegistry.build().run_events("web-research", _common.FakeRegistry(),
                                                    config=_cfg()))
        self.assertEqual(evs[0]["type"], "skill_start")
        self.assertEqual(evs[0]["mode"], "sub_agent")
        self.assertEqual(evs[-1]["type"], "final")
        self.assertEqual(evs[-1]["result"], "a cited brief")

    def test_sub_agent_skill_uses_tools(self):
        turns = ['<tool_call>{"name": "web_search", "arguments": {}}</tool_call>', "done"]
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(turns)
        reg_tools = _common.FakeRegistry({"web_search": "hits"})
        out = SkillRegistry.build().run("web-research", reg_tools, config=_cfg())
        self.assertEqual(out, "done")
        self.assertIn("web_search", reg_tools.dispatched)  # ran through the shared dispatch path

    def test_sub_agent_skill_without_config_reports_runtime_not_module_o(self):
        out = SkillRegistry.build().run("web-research", _common.FakeRegistry())  # no config
        self.assertNotIn("Module O", out)
        self.assertIn("sub-agent skill", out)


if __name__ == "__main__":
    unittest.main()
