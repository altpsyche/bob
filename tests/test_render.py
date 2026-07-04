"""NE2 — rich catalog views (bob.render): commands grouped + hidden excluded, tools/skills with
failures surfaced, all themed. Rendered to a captured console and checked by substring."""
import io
import unittest

import _common  # noqa: F401 — puts scripts/ on sys.path
from _common import fake_config

try:  # rich is the shell UI dep; skip (not error) where it isn't installed (e.g. the minimal CI python)
    import rich  # noqa: F401
except ModuleNotFoundError as _e:  # pragma: no cover
    raise unittest.SkipTest(f"rich not installed: {_e}")

from bob import render
from bob.theme import Theme


def _theme():
    from rich.console import Console
    return Theme.load(fake_config(), Console(file=io.StringIO(), force_terminal=False))


def _render(renderable) -> str:
    from rich.console import Console
    con = Console(file=io.StringIO(), force_terminal=False, width=120, no_color=True)
    con.print(renderable)
    return con.file.getvalue()


class _ToolReg:
    _loaded_names = {"web", "git"}
    tool_schemas = [{"type": "function", "function": {"name": "git_status", "description": "status"}}]
    errors = [("broken_tool", "contract", "missing DISPATCH")]


class _SkillReg:
    def __init__(self, skills):
        self._s = skills
        self.errors = []

    def list(self):
        return self._s


class TestRender(unittest.TestCase):
    def test_commands_grouped_and_hidden_excluded(self):
        out = _render(render.commands_view(_theme()))
        self.assertIn("Talk", out)
        self.assertIn("chat", out)
        self.assertNotIn("verify-urls", out)      # hidden verb stays out of the catalog

    def test_tools_show_failed_not_hidden(self):
        out = _render(render.tools_view(_ToolReg(), _theme()))
        self.assertIn("git_status", out)
        self.assertIn("broken_tool", out)
        self.assertIn("FAILED", out)

    def test_skills_tag_sub_agent(self):
        reg = _SkillReg([
            {"name": "repo-brief", "description": "d", "steps": [{"tool": "x"}]},
            {"name": "web-research", "description": "d", "steps": []},
        ])
        out = _render(render.skills_view(reg, _theme()))
        self.assertIn("repo-brief", out)
        self.assertIn("sub-agent", out)      # no-steps skill tagged runnable (O11)


if __name__ == "__main__":
    unittest.main()
