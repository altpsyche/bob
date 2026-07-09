"""Structured edit engine (bob_edit) + the file_edit tool: precise search/replace with strict matching,
correctable rejections, multi-file atomicity, and the allowlist/secrets guard."""
import shutil
import tempfile
import unittest
from pathlib import Path

import _common  # noqa: F401 — puts scripts/ + scripts/tools on sys.path
import bob_edit


class TestEditEngine(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bob-edit-"))
        self.allowed = [self.dir]

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, name, text):
        p = self.dir / name
        p.write_text(text, encoding="utf-8")
        return p

    def test_exact_search_replace_applies(self):
        p = self._write("a.py", "def f():\n    return 1\n")
        res = bob_edit.apply_edits(
            {"path": str(p), "edits": [{"search": "    return 1", "replace": "    return 2"}]},
            self.allowed)
        self.assertTrue(res.ok, res.message)
        self.assertEqual(p.read_text(), "def f():\n    return 2\n")

    def test_whitespace_tolerant_match_reindents(self):
        p = self._write("a.py", "class C:\n    def f(self):\n        return 1\n")
        # Model supplies the body under-indented; normalized match + reindent should land it correctly.
        res = bob_edit.apply_edits(
            {"path": str(p), "edits": [{"search": "return 1", "replace": "return 42"}]},
            self.allowed)
        self.assertTrue(res.ok, res.message)
        self.assertEqual(p.read_text(), "class C:\n    def f(self):\n        return 42\n")

    def test_ambiguous_match_is_rejected_and_nothing_written(self):
        p = self._write("a.py", "x = 1\nx = 1\n")
        res = bob_edit.apply_edits(
            {"path": str(p), "edits": [{"search": "x = 1", "replace": "x = 2"}]},
            self.allowed)
        self.assertFalse(res.ok)
        self.assertIn("ambiguous", res.message)
        self.assertEqual(p.read_text(), "x = 1\nx = 1\n")  # unchanged

    def test_rejection_message_prefix_and_not_self_repair_trigger(self):
        p = self._write("a.py", "a = 1\n")
        res = bob_edit.apply_edits(
            {"path": str(p), "edits": [{"search": "no such line", "replace": "z = 9"}]},
            self.allowed)
        self.assertFalse(res.ok)
        self.assertTrue(res.message.startswith("EDIT REJECTED"))
        for prefix in ("Tool error", "Unknown tool", "Bad arguments"):
            self.assertFalse(res.message.startswith(prefix))

    def test_rejection_shows_closest_lines(self):
        p = self._write("a.py", "def compute_total(items):\n    return sum(items)\n")
        res = bob_edit.apply_edits(
            {"path": str(p), "edits": [{"search": "def compute_total(item):", "replace": "def compute_total(xs):"}]},
            self.allowed)
        self.assertFalse(res.ok)
        self.assertIn("compute_total", res.message)  # near-miss echoed

    def test_multi_file_one_bad_hunk_lands_nothing(self):
        p1 = self._write("a.py", "a = 1\n")
        p2 = self._write("b.py", "b = 1\n")
        res = bob_edit.apply_edits({"files": [
            {"path": str(p1), "edits": [{"search": "a = 1", "replace": "a = 2"}]},
            {"path": str(p2), "edits": [{"search": "MISSING", "replace": "b = 2"}]},
        ]}, self.allowed)
        self.assertFalse(res.ok)
        self.assertEqual(p1.read_text(), "a = 1\n")  # good hunk did NOT land
        self.assertEqual(p2.read_text(), "b = 1\n")

    def test_multi_file_all_good_applies_all(self):
        p1 = self._write("a.py", "a = 1\n")
        p2 = self._write("b.py", "b = 1\n")
        res = bob_edit.apply_edits({"files": [
            {"path": str(p1), "edits": [{"search": "a = 1", "replace": "a = 2"}]},
            {"path": str(p2), "edits": [{"search": "b = 1", "replace": "b = 2"}]},
        ]}, self.allowed)
        self.assertTrue(res.ok, res.message)
        self.assertEqual(p1.read_text(), "a = 2\n")
        self.assertEqual(p2.read_text(), "b = 2\n")

    def test_secret_path_refused(self):
        p = self._write("config.json", '{"litellmKey":"SECRET"}')
        res = bob_edit.apply_edits(
            {"path": str(p), "edits": [{"search": "SECRET", "replace": "X"}]},
            self.allowed)
        self.assertFalse(res.ok)
        self.assertIn("sensitive", res.message)
        self.assertIn("SECRET", p.read_text())  # untouched

    def test_outside_allowed_write_refused(self):
        other = Path(tempfile.mkdtemp(prefix="bob-edit-other-"))
        try:
            p = other / "f.py"
            p.write_text("a = 1\n", encoding="utf-8")
            res = bob_edit.apply_edits(
                {"path": str(p), "edits": [{"search": "a = 1", "replace": "a = 2"}]},
                self.allowed)
            self.assertFalse(res.ok)
            self.assertIn("allowedWritePaths", res.message)
            self.assertEqual(p.read_text(), "a = 1\n")
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_disabled_when_no_allowed_write(self):
        p = self._write("a.py", "a = 1\n")
        res = bob_edit.apply_edits(
            {"path": str(p), "edits": [{"search": "a = 1", "replace": "a = 2"}]},
            [])
        self.assertFalse(res.ok)
        self.assertIn("disabled", res.message)

    def test_whole_file_content_creates_file(self):
        p = self.dir / "new.py"
        res = bob_edit.apply_edits({"path": str(p), "content": "created = True\n"}, self.allowed)
        self.assertTrue(res.ok, res.message)
        self.assertEqual(p.read_text(), "created = True\n")

    def test_search_replace_on_missing_file_rejected(self):
        res = bob_edit.apply_edits(
            {"path": str(self.dir / "nope.py"), "edits": [{"search": "x", "replace": "y"}]},
            self.allowed)
        self.assertFalse(res.ok)
        self.assertIn("does not exist", res.message)

    def test_full_file_read_not_truncated(self):
        # A file larger than file_read's 6000-char cap must still edit correctly (the engine reads full).
        big = "# pad\n" * 2000  # ~12000 chars
        p = self._write("big.py", big + "TARGET = 1\n")
        res = bob_edit.apply_edits(
            {"path": str(p), "edits": [{"search": "TARGET = 1", "replace": "TARGET = 2"}]},
            self.allowed)
        self.assertTrue(res.ok, res.message)
        self.assertTrue(p.read_text().endswith("TARGET = 2\n"))
        self.assertIn("# pad", p.read_text())

    def test_preview_does_not_write_and_matches_apply(self):
        p = self._write("a.py", "a = 1\n")
        args = {"path": str(p), "edits": [{"search": "a = 1", "replace": "a = 2"}]}
        prev = bob_edit.preview_edits(args, self.allowed)
        self.assertTrue(prev.ok, prev.message)
        self.assertEqual(p.read_text(), "a = 1\n")  # preview wrote nothing
        self.assertIn("-a = 1", prev.diff)
        self.assertIn("+a = 2", prev.diff)
        # applying yields the content preview computed
        bob_edit.apply_edits(args, self.allowed)
        self.assertEqual(p.read_text(), prev.files[str(p)])


class TestDiffFormat(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bob-edit-diff-"))
        self.allowed = [self.dir]

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_unified_diff_applies_ignoring_line_numbers(self):
        p = self.dir / "a.py"
        p.write_text("def f():\n    x = 1\n    return x\n", encoding="utf-8")
        # Deliberately wrong line numbers in the @@ header -- must still land by content.
        diff = ("@@ -999,3 +999,3 @@\n"
                " def f():\n"
                "-    x = 1\n"
                "+    x = 2\n"
                "     return x\n")
        res = bob_edit.apply_edits({"path": str(p), "diff": diff}, self.allowed)
        self.assertTrue(res.ok, res.message)
        self.assertEqual(p.read_text(), "def f():\n    x = 2\n    return x\n")

    def test_parse_unified_diff_hunks(self):
        hunks = bob_edit.parse_unified_diff("@@ -1 +1 @@\n-old\n+new\n")
        self.assertEqual(hunks, [("old", "new")])

    def test_diff_with_no_hunks_rejected(self):
        p = self.dir / "a.py"
        p.write_text("a = 1\n", encoding="utf-8")
        res = bob_edit.apply_edits({"path": str(p), "diff": "not a diff at all"}, self.allowed)
        self.assertFalse(res.ok)
        self.assertTrue(res.message.startswith("EDIT REJECTED"))


class TestRenderDiff(unittest.TestCase):
    def test_render_diff_is_unified(self):
        d = bob_edit.render_diff("a.py", "a = 1\n", "a = 2\n")
        self.assertIn("-a = 1", d)
        self.assertIn("+a = 2", d)
        self.assertIn("a/a.py", d)
        self.assertIn("b/a.py", d)


if __name__ == "__main__":
    unittest.main()
