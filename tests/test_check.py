"""scripts/check.py's own checks: release metadata (VERSION / CHANGELOG / tag), the n8n portability scan,
and the unittest skip guard. Hermetic: each runs against a temp tree, never the repo's files."""
import json
import tempfile
import unittest
from pathlib import Path

import _common  # noqa: F401 — puts scripts/ on sys.path
import check

_CHANGELOG = "# Changelog\n\n## [Unreleased]\n\n- next\n\n## [1.3.0] (2026-09-08)\n\n- shipped\n\n## [1.2.3] (x)\n"


class _Tree(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.addCleanup(self._d.cleanup)
        self.root = Path(self._d.name)

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


class TestReleaseMetadata(_Tree):
    def _problems(self, version="1.3.0", changelog=_CHANGELOG, ref=""):
        self.write("VERSION", version + "\n")
        self.write("CHANGELOG.md", changelog)
        return check.release_metadata_problems(self.root, env={"GITHUB_REF": ref} if ref else {})

    def test_consistent_tree_passes(self):
        self.assertEqual(self._problems(), [])
        self.assertEqual(self._problems(ref="refs/tags/v1.3.0"), [])
        self.assertEqual(self._problems(ref="refs/heads/main"), [])

    def test_version_without_a_section_fails(self):
        self.assertTrue(any("no '## [1.4.0]'" in p for p in self._problems(version="1.4.0")))

    def test_version_behind_the_newest_section_fails(self):
        self.assertTrue(any("newest release is [1.3.0]" in p for p in self._problems(version="1.2.3")))

    def test_missing_unreleased_fails(self):
        cl = _CHANGELOG.replace("## [Unreleased]\n", "")
        self.assertTrue(any("Unreleased" in p for p in self._problems(changelog=cl)))

    def test_tag_must_equal_v_version(self):
        self.assertTrue(any("v1.3.0" in p for p in self._problems(ref="refs/tags/v1.3.1")))

    def test_repo_is_consistent(self):
        # The live tree: VERSION and CHANGELOG.md must agree on every commit.
        self.assertEqual(check.release_metadata_problems(env={}), [])


class TestN8nScan(_Tree):
    def test_flags_docker_host_and_default_key(self):
        self.write("tools/n8n-workflows/a.json",
                   json.dumps({"url": "http://host.docker.internal:8081", "key": "Bearer sk-local"}))
        self.write("tools/n8n-workflows/b.json", json.dumps({"url": "http://{{$env.BOB_HOST}}:8081"}))
        got = check.n8n_problems(self.root)
        self.assertEqual(len(got), 2)
        self.assertTrue(all("a.json" in p for p in got))

    def test_invalid_json_is_reported(self):
        self.write("tools/n8n-workflows/c.json", "{not json")
        self.assertIn("invalid JSON", check.n8n_problems(self.root)[0])


class TestSkipGuard(unittest.TestCase):
    def test_allowed_skips_pass(self):
        skipped = [("test_release_manifest.TestPublishedManifestLive.test_real_published_rows_resolve", "opt-in")]
        self.assertEqual(check.unexpected_skips(skipped, "linux"), [])

    def test_platform_scoped_allowance(self):
        s = [("test_sandbox.TestLinuxConfinement.test_x", "bwrap not present")]
        self.assertEqual(check.unexpected_skips(s, "win32"), [])
        self.assertEqual(check.unexpected_skips(s, "linux"), s)

    def test_missing_dep_skip_fails(self):
        s = [("test_mcp.TestStreamableHttp.test_x", "mcp/starlette not installed"),
             ("unittest.loader.ModuleSkipped.test_theme", "rich not installed")]
        self.assertEqual(check.unexpected_skips(s, "linux"), s)


if __name__ == "__main__":
    unittest.main()
