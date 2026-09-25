"""Plugin layout: every plugin's invoke.py exposes main(argv) (the `bob <plugin> ...` entry point) and
holds the logic its tool.py imports; draft's long-form types route to a role get_role understands."""
import importlib
import sys
import unittest
from pathlib import Path
from unittest import mock

import _common  # noqa: F401  (puts scripts/ + scripts/tools on sys.path)

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import bob_core  # noqa: E402


def _plugins():
    return sorted(d.name for d in (REPO / "plugins").iterdir()
                  if d.is_dir() and not d.name.startswith(("_", ".")))


class TestLayout(unittest.TestCase):
    def test_every_plugin_has_python_main_and_no_powershell(self):
        for name in _plugins():
            d = REPO / "plugins" / name
            self.assertTrue((d / "invoke.py").exists(), name)
            self.assertFalse(list(d.glob("*.ps1")), name)
            mod = importlib.import_module(f"plugins.{name}.invoke")
            self.assertTrue(callable(getattr(mod, "main", None)), name)

    def test_main_takes_argv_and_returns_exit_code(self):
        # --help through main(argv) proves argv is honored (not sys.argv) with the `bob <name>` prog.
        for name in _plugins():
            mod = importlib.import_module(f"plugins.{name}.invoke")
            with mock.patch("sys.stdout") as out, self.assertRaises(SystemExit) as ctx:
                mod.main(["--help"])
            self.assertEqual(ctx.exception.code, 0, name)
            printed = "".join(c.args[0] for c in out.write.call_args_list)
            self.assertIn(f"bob {name}", printed, name)


class TestDraftRouting(unittest.TestCase):
    def test_long_form_types_use_a_known_role_task(self):
        from plugins.draft import invoke as draft
        table = bob_core.load_defaults()["roleTable"]
        for task in draft.TYPE_TASK_MAP.values():
            self.assertIn(task, table)

    def test_pr_routes_to_reasoning_role_not_chat(self):
        from plugins.draft import invoke as draft
        cfg = _common.fake_config()
        seen = {}
        client = mock.Mock()
        client.chat.completions.create.side_effect = lambda **kw: seen.update(kw) or mock.Mock(
            choices=[mock.Mock(message=mock.Mock(content="d"), finish_reason="stop")])
        # The plugin goes through bob_core.complete, the one non-agent completion path.
        with mock.patch.object(bob_core, "get_llm_client", return_value=client):
            draft.draft("x", "pr", cfg)
            self.assertEqual(seen["model"], "ponder")
            draft.draft("x", "slack", cfg)
            self.assertEqual(seen["model"], "chat")


class TestSummariseCli(unittest.TestCase):
    def test_documented_invocation_parses(self):
        # tool.py's self-test suggests `bob summarise README.md --length short`: that argv must work.
        from plugins.summarise import invoke as summ
        seen = {}
        with mock.patch.object(summ, "load_config", return_value={}), \
             mock.patch.object(summ, "check_litellm", return_value=True), \
             mock.patch.object(summ, "summarise",
                               side_effect=lambda c, length, cfg: seen.update(length=length) or "s"), \
             mock.patch("sys.stdout"):
            rc = summ.main([str(REPO / "README.md"), "--length", "short"])
        self.assertEqual(rc, 0)
        self.assertEqual(seen["length"], "short")

    def test_missing_file_is_exit_1(self):
        from plugins.summarise import invoke as summ
        with mock.patch("sys.stderr"):
            self.assertEqual(summ.main(["/nonexistent/file.md"]), 1)


if __name__ == "__main__":
    unittest.main()
