"""Shell input wiring (scripts/bob/shell.py `_session_kwargs` + theme.ptk_color_depth): the fuzzy
slash completer, history ghost-text, colour-depth alignment, and the opt-in multiline flag. Driven
without a TTY by inspecting the session kwargs and calling the completer directly with a Document."""
import io
import unittest

import _common  # noqa: F401 — puts scripts/ on sys.path
from _common import FakeRegistry, fake_config

try:  # rich + prompt_toolkit are the shell input deps; skip where either isn't installed
    import rich  # noqa: F401
    import prompt_toolkit  # noqa: F401
except ModuleNotFoundError as _e:  # pragma: no cover
    raise unittest.SkipTest(f"shell input deps not installed: {_e}")

from bob import shell as shellmod
from bob import theme as th
from bob.shell import BobShell


class _FakeSkillReg:
    def __init__(self):
        self.skills = {}
        self.errors = []

    def list(self):
        return []


def _make_shell(**cfg_over):
    from rich.console import Console
    console = Console(file=io.StringIO(), force_terminal=False, width=100, no_color=True)
    return BobShell(fake_config(**cfg_over), FakeRegistry(), _FakeSkillReg(), console=console)


def _completions(completer, text):
    """The completions a completer yields for `text` (cursor at end), headlessly."""
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document
    doc = Document(text, cursor_position=len(text))
    return list(completer.get_completions(doc, CompleteEvent()))


class TestSessionInput(unittest.TestCase):
    def test_completer_is_fuzzy_and_covers_the_slash_tree(self):
        from prompt_toolkit.completion import FuzzyCompleter
        comp = _make_shell()._session_kwargs()["completer"]
        self.assertIsInstance(comp, FuzzyCompleter)
        self.assertEqual({c.text for c in _completions(comp, "/")}, set(shellmod._SLASH))

    def test_history_autosuggest_attached(self):
        from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
        kw = _make_shell()._session_kwargs()
        self.assertIsInstance(kw["auto_suggest"], AutoSuggestFromHistory)

    def test_menu_opens_while_typing(self):
        self.assertTrue(_make_shell()._session_kwargs()["complete_while_typing"])

    def test_fuzzy_matches_slash_commands(self):
        comp = _make_shell()._session_kwargs()["completer"]
        texts = {c.text for c in _completions(comp, "/ag")}
        self.assertIn("/agent", texts)
        self.assertIn("/agency", texts)   # both surface under the same fuzzy prefix

    def test_plain_message_yields_no_menu(self):
        comp = _make_shell()._session_kwargs()["completer"]
        self.assertEqual(_completions(comp, "what is 2+2"), [])   # no '/' → nothing to complete

    def test_color_depth_is_set(self):
        from prompt_toolkit.output import ColorDepth
        self.assertIsInstance(_make_shell()._session_kwargs()["color_depth"], ColorDepth)


class TestSlashDiscovery(unittest.TestCase):
    """The '/' menu is self-documenting: each command carries its one-line description from the single
    _COMMANDS list; sub-commands still complete; typos are tolerated with the meta intact."""

    def _comp(self):
        return _make_shell()._session_kwargs()["completer"]

    def test_every_command_shows_its_descriptor_description(self):
        by_text = {c.text: c.display_meta_text for c in _completions(self._comp(), "/")}
        for cmd in shellmod._COMMANDS:
            for nm in cmd.names():
                self.assertEqual(by_text.get(nm), cmd.desc)   # meta == descriptor, one source

    def test_meta_covers_every_command(self):
        self.assertEqual({c.text for c in _completions(self._comp(), "/")}, set(shellmod._SLASH))

    def test_typo_still_matches_with_meta(self):
        got = [(c.text, c.display_meta_text) for c in _completions(self._comp(), "/agcy")]
        self.assertEqual(got, [("/agency", "tool-approval mode: show | confirm | silent")])

    def test_subcommands_still_complete(self):
        self.assertEqual({c.text for c in _completions(self._comp(), "/agency ")},
                         {"show", "confirm", "silent"})
        self.assertEqual({c.text for c in _completions(self._comp(), "/services ")},
                         {"start", "stop"})

    def test_unknown_slash_yields_nothing(self):
        self.assertEqual(_completions(self._comp(), "/zzq"), [])


class TestMultilineFlag(unittest.TestCase):
    def test_default_off_leaves_enter_gesture_alone(self):
        kw = _make_shell()._session_kwargs()
        self.assertNotIn("multiline", kw)          # single-line: Enter submits, as before
        self.assertNotIn("prompt_continuation", kw)

    def test_opt_in_enables_multiline_with_continuation(self):
        kw = _make_shell(ui={"input": {"multiline": True}})._session_kwargs()
        self.assertIs(kw["multiline"], True)
        self.assertTrue(callable(kw["prompt_continuation"]))

    def test_continuation_renders_a_gutter(self):
        from prompt_toolkit.formatted_text import fragment_list_to_text, to_formatted_text
        sh = _make_shell(ui={"input": {"multiline": True}})
        cont = sh._continuation(6, 1, False)
        self.assertIn("…", fragment_list_to_text(to_formatted_text(cont)))


class TestColorDepth(unittest.TestCase):
    def test_maps_each_rich_system(self):
        from prompt_toolkit.output import ColorDepth

        def depth(system):
            return th.ptk_color_depth(type("C", (), {"color_system": system})())

        self.assertEqual(depth("truecolor"), ColorDepth.DEPTH_24_BIT)
        self.assertEqual(depth("256"), ColorDepth.DEPTH_8_BIT)
        self.assertEqual(depth("standard"), ColorDepth.DEPTH_4_BIT)
        self.assertEqual(depth("windows"), ColorDepth.DEPTH_4_BIT)

    def test_unknown_or_none_falls_back_to_256(self):
        from prompt_toolkit.output import ColorDepth
        self.assertEqual(th.ptk_color_depth(type("C", (), {"color_system": None})()),
                         ColorDepth.DEPTH_8_BIT)


if __name__ == "__main__":
    unittest.main()
