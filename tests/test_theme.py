"""The theme abstraction (bob.theme): merge precedence, gradient math, unicode glyph fallback,
Theme.load field mapping, and safe header rendering."""
import io
import unittest

import _common  # noqa: F401 — puts scripts/ on sys.path
from _common import fake_config

try:  # rich is the shell UI dep; skip (not error) where it isn't installed (e.g. the minimal CI python)
    import rich  # noqa: F401
except ModuleNotFoundError as _e:  # pragma: no cover
    raise unittest.SkipTest(f"rich not installed: {_e}")

from bob import theme as th
from bob.theme import Theme


def _console():
    from rich.console import Console
    return Console(file=io.StringIO(), force_terminal=False, width=100, no_color=True)


class _Cp1252Console:
    """Stand-in console whose file reports a non-UTF-8 encoding (drives the ASCII glyph fallback)."""
    class file:
        encoding = "cp1252"


class TestMerge(unittest.TestCase):
    def test_deep_merge_recursive_and_pure(self):
        base = {"a": {"x": 1, "y": 2}, "b": 3}
        out = th._deep_merge(base, {"a": {"y": 9}, "c": 4})
        self.assertEqual(out, {"a": {"x": 1, "y": 9}, "b": 3, "c": 4})
        self.assertEqual(base["a"]["y"], 2)          # original not mutated

    def test_load_ui_sections_present(self):
        ui = th.load_ui(fake_config())
        for k in ("header", "colors", "glyphs", "spacing", "markdown"):
            self.assertIn(k, ui)

    def test_config_ui_is_highest_precedence(self):
        # config['ui'] wins over the shipped config/ui.json and the built-in defaults.
        ui = th.load_ui({"ui": {"prompt": "zzz", "colors": {"accent": "magenta"}}})
        self.assertEqual(ui["prompt"], "zzz")
        self.assertEqual(ui["colors"]["accent"], "magenta")


class TestGradient(unittest.TestCase):
    def test_lerp_endpoints_and_mid(self):
        self.assertEqual(th.lerp_color(["#000000", "#ffffff"], 0.0).lower(), "#000000")
        self.assertEqual(th.lerp_color(["#000000", "#ffffff"], 1.0).lower(), "#ffffff")
        self.assertEqual(th.lerp_color(["#000000", "#ffffff"], 0.5).lower(), "#808080")

    def test_gradient_n_and_single(self):
        self.assertEqual(th.gradient(["#000000", "#ffffff"], 3)[1].lower(), "#808080")
        self.assertEqual(th.gradient(["#abcdef"], 3), ["#abcdef"] * 3)

    def test_bad_hex_is_safe(self):
        self.assertTrue(th.lerp_color(["nonsense"], 0.5).startswith("#"))


class TestGlyphsAndColor(unittest.TestCase):
    def test_unicode_ok_true_on_utf8(self):
        self.assertTrue(th.unicode_ok(_console()))

    def test_ascii_fallback_on_cp1252(self):
        g = th.resolve_glyphs(th._DEFAULT_UI, _Cp1252Console())
        self.assertEqual(g["gear"], "*")
        self.assertEqual(g["spinner"], "line")

    def test_ptk_color_maps(self):
        self.assertEqual(th.ptk_color("cyan"), "ansicyan")
        self.assertEqual(th.ptk_color("#F2A63B"), "#F2A63B")


class TestThemeLoad(unittest.TestCase):
    def test_fields_and_override(self):
        t = Theme.load({"ui": {"colors": {"accent": "#123456"}, "prompt": "hey"}}, _console())
        self.assertEqual(t.accent, "#123456")
        self.assertEqual(t.prompt, "hey")
        self.assertTrue(t.header.text)
        self.assertIn(t.header.gradient_dir, ("vertical", "horizontal", "diagonal"))
        self.assertTrue(t.source.endswith("ui.json"))

    def test_prompt_style_map(self):
        s = Theme.load(fake_config(), _console()).prompt_style
        self.assertIn("prompt", s)
        self.assertIn("arrow", s)
        self.assertTrue(s["bottom-toolbar"].startswith("bg:"))   # toolbar auto-tinted from accent

    def test_prose_width_override(self):
        self.assertEqual(Theme.load({"ui": {"prose_width": 50}}, _console()).prose_width, 50)

    def test_tagline_override_and_default(self):
        self.assertEqual(Theme.load({"ui": {"tagline": "hi there"}}, _console()).tagline, "hi there")
        self.assertIsInstance(Theme.load(fake_config(), _console()).tagline, str)

    def test_frozen(self):
        t = Theme.load(fake_config(), _console())
        with self.assertRaises(Exception):
            t.accent = "red"          # frozen dataclass — parsed once, immutable


class TestRenderHeader(unittest.TestCase):
    def test_valid_font_renders(self):
        con = _console()
        th.render_header(Theme.load({"ui": {"header": {"font": "big", "text": "BOB"}}}, con), con)

    def test_bad_font_falls_back_to_text(self):
        con = _console()
        t = Theme.load({"ui": {"header": {"font": "definitely-not-a-font", "text": "BOB"}}}, con)
        th.render_header(t, con)
        self.assertIn("BOB", con.file.getvalue())

    def test_disabled_header_prints_nothing(self):
        con = _console()
        th.render_header(Theme.load({"ui": {"header": {"enabled": False}}}, con), con)
        self.assertEqual(con.file.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
