"""NE4 — the skills registry: discover/validate/list + simple tool-sequence execution, with sub-agent
skills reporting they require Module O and malformed manifests as hard contract errors."""
import tempfile
import unittest
from pathlib import Path

import _common  # noqa: F401 — puts scripts/ on sys.path

try:  # skill manifests are YAML; without PyYAML the registry degrades to empty, so skip (not fail)
    import yaml  # noqa: F401
except ModuleNotFoundError as _e:  # pragma: no cover
    raise unittest.SkipTest(f"PyYAML not installed: {_e}")

from bob_skills import SkillRegistry


class _FakeToolReg:
    def __init__(self):
        self.calls = []

    def dispatch_call(self, name, args_json, context=None):
        self.calls.append((name, args_json))
        return f"[{name} ran]"


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

    def test_sub_agent_skill_requires_module_o(self):
        out = SkillRegistry.build().run("web-research", _FakeToolReg())
        self.assertIn("Module O", out)

    def test_unknown_skill(self):
        self.assertIn("Unknown skill", SkillRegistry.build().run("nope", _FakeToolReg()))

    def test_broken_manifest_is_contract_error(self):
        d = Path(tempfile.mkdtemp(prefix="bob-skills-"))
        (d / "bad").mkdir()
        (d / "bad" / "skill.yaml").write_text("group: X\n", encoding="utf-8")  # no description
        reg = SkillRegistry.build(d)
        self.assertNotIn("bad", reg.skills)
        self.assertTrue(any(phase == "contract" for _, phase, _ in reg.errors))


if __name__ == "__main__":
    unittest.main()
