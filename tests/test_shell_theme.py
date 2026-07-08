"""Theme presets + accessibility (bob.theme): the named palettes selectable via `ui.theme`, the
preset/override precedence, the NO_COLOR monochrome path, and the ANSI (16-colour) preset used both
for colour-blind-legacy terminals and as the truecolor fallback."""
import io
import os
import unittest
from unittest import mock

import _common  # noqa: F401 — puts scripts/ on sys.path
from _common import fake_config

try:  # rich is the shell UI dep; skip (not error) where it isn't installed
    import rich  # noqa: F401
except ModuleNotFoundError as _e:  # pragma: no cover
    raise unittest.SkipTest(f"rich not installed: {_e}")

from bob import theme as th
from bob.theme import Theme

# The 16 ANSI colours rich can name (8 base + 8 bright); the `ansi` preset must stay within these.
_ANSI_16 = th._ANSI_NAMES | {f"bright_{n}" for n in th._ANSI_NAMES}


def _console():
    from rich.console import Console
    return Console(file=io.StringIO(), force_terminal=False, width=100, no_color=True)


def _load(**ui):
    return Theme.load({"ui": ui}, _console())


class TestPresets(unittest.TestCase):
    def setUp(self):
        # Presets only show through when NO_COLOR is not forcing monochrome.
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("NO_COLOR", None)

    def tearDown(self):
        self._env.stop()

    def test_every_preset_loads_a_full_palette(self):
        for name in th._PRESETS:
            t = _load(theme=name)
            for role in (t.accent, t.success, t.error, t.warn, t.tool, t.muted):
                self.assertTrue(role and isinstance(role, str), f"{name}: empty swatch")

    def test_named_preset_recolours(self):
        self.assertEqual(_load(theme="dark").accent, "#7AA2F7")
        self.assertEqual(_load(theme="light").accent, "#8250DF")
        self.assertEqual(_load(theme="mauve").accent, "#C48CD6")   # the built-in identity

    def test_explicit_swatch_overrides_the_preset(self):
        # config['ui'].colors is higher precedence than the preset it sits on.
        t = _load(theme="dark", colors={"accent": "#123456"})
        self.assertEqual(t.accent, "#123456")
        self.assertEqual(t.success, "#9ECE6A")     # untouched dark swatch remains

    def test_unknown_theme_falls_back_to_mauve(self):
        self.assertEqual(_load(theme="nonsense").accent, "#C48CD6")

    def test_ansi_preset_uses_only_ansi_names(self):
        t = _load(theme="ansi")
        for role in (t.accent, t.success, t.error, t.warn, t.tool, t.muted):
            self.assertIn(role, _ANSI_16)


class TestNoColor(unittest.TestCase):
    def _load_with(self, value):
        env = {"NO_COLOR": value} if value is not None else {}
        with mock.patch.dict(os.environ, env, clear=False):
            if value is None:
                os.environ.pop("NO_COLOR", None)
            return Theme.load(fake_config(), _console()), th.no_color_active()

    def test_set_nonempty_forces_monochrome(self):
        t, active = self._load_with("1")
        self.assertTrue(active)
        self.assertTrue(t.no_color)
        self.assertEqual(t.accent, "bold")     # weight, not hue
        self.assertEqual(t.muted, "dim")

    def test_empty_value_keeps_colour(self):
        # no-color.org: an EMPTY value does not disable colour.
        t, active = self._load_with("")
        self.assertFalse(active)
        self.assertFalse(t.no_color)
        self.assertEqual(t.accent, "#C48CD6")

    def test_unset_keeps_colour(self):
        t, active = self._load_with(None)
        self.assertFalse(active)
        self.assertFalse(t.no_color)


class TestPtkBrightNames(unittest.TestCase):
    def test_bright_name_maps_to_ptk_token(self):
        self.assertEqual(th.ptk_color("bright_black"), "ansibrightblack")
        self.assertEqual(th.ptk_color("magenta"), "ansimagenta")   # base names still work


if __name__ == "__main__":
    unittest.main()
