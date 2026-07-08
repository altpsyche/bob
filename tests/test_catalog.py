"""Catalog rendering from the registries (commands grouped + hidden excluded, tools/skills with
failed loads surfaced, counts straight from the registries)."""
import unittest

import _common  # noqa: F401 — puts scripts/ on sys.path
from bob import catalog


class _FakeToolReg:
    _loaded_names = {"web", "git"}
    tool_schemas = [
        {"type": "function", "function": {"name": "git_status", "description": "status"}},
    ]
    errors = [("broken_tool", "contract", "missing DISPATCH")]


class _FakeSkillReg:
    def __init__(self, skills, errors=None):
        self._skills = skills
        self.errors = errors or []

    def list(self):
        return self._skills


class TestCatalog(unittest.TestCase):
    def test_commands_grouped_and_hidden_excluded(self):
        out = catalog.render_commands()
        self.assertIn("Talk:", out)
        self.assertIn("chat", out)
        self.assertNotIn("verify-urls", out)   # hidden dev utility
        self.assertNotIn("(alias of stop)", out)  # 'down' is hidden

    def test_tools_show_failed_not_hidden(self):
        out = catalog.render_tools(_FakeToolReg())
        self.assertIn("git_status", out)
        self.assertIn("[FAILED] broken_tool", out)

    def test_skills_mark_sub_agent(self):
        reg = _FakeSkillReg([
            {"name": "repo-brief", "description": "d", "steps": [{"tool": "git_status"}]},
            {"name": "web-research", "description": "d", "steps": []},
        ])
        out = catalog.render_skills(reg)
        self.assertIn("repo-brief", out)
        self.assertIn("[sub-agent]", out)   # web-research has no steps → runnable sub-agent skill

    def test_counts_from_registries(self):
        c = catalog.counts(
            tool_registry=_FakeToolReg(),
            skill_registry=_FakeSkillReg([{"name": "x", "description": "d", "steps": []}]),
        )
        self.assertIn("tools", c)
        self.assertIn("commands", c)
        self.assertIn("skills", c)


if __name__ == "__main__":
    unittest.main()
