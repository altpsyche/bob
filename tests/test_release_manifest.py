"""The engine-manifest resolution contract: the resolver must select every prebuilt row the release
publishes, or a shipped engine is silently declined for a slow source build (the incident that motivated
1.2.2 — the published tier is 'cuda' while the internal decision is 'gpu', and equality matching failed).

Two layers:
- TestManifestContract (hermetic, runs every gate): a fixture manifest built by the SAME row emitter the
  publish jobs call (.github/scripts/pack_engine.py engine_row). It proves the resolver selects each row,
  that the internal 'gpu' query matches the published 'cuda' row, and (the non-vacuous part) that a wrong
  tier value or a renamed key yields no match. TestPublishedManifestLive is the backstop against the real
  published asset.
- TestPublishedManifestLive (network, opt-in via BOB_LIVE_MANIFEST_TEST=1, run by the dedicated CI job): fetches
  the ACTUAL published engines.json for the current/latest release tag and asserts each real row resolves with
  the commit guard passing. Skips cleanly offline / no tag / no manifest so it is never a false red."""
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import _common  # noqa: F401 — puts scripts/ on sys.path
import osenv

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "tools"))
from bob import lifecycle  # noqa: E402

# Published tier value -> the internal tier resolve_build_tier emits (the synonym bridge under test).
_QUERY = {"cuda": "gpu", "cpu": "cpu"}


def _load_pack_engine():
    """The publish jobs' row emitter (.github/scripts/pack_engine.py), loaded by path (not a package)."""
    import importlib.util
    path = Path(__file__).resolve().parent.parent / ".github" / "scripts" / "pack_engine.py"
    spec = importlib.util.spec_from_file_location("pack_engine", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pack_engine = _load_pack_engine()


def _published_row(os_name, arch, tier, commit):
    """The row the publish jobs emit, produced by the same pack_engine.engine_row they call."""
    (key, row), = pack_engine.engine_row(os_name, tier, "o/r", "v9.9.9", commit, cpu_arch=arch,
                                         sha256="b" * 64, nbytes=512345678).items()
    return key, row


class TestManifestContract(unittest.TestCase):
    COMMIT = "a" * 40

    def _manifest(self):
        rows = [_published_row("linux", "x86_64", "cuda", self.COMMIT),
                _published_row("linux", "x86_64", "cpu", self.COMMIT),
                _published_row("windows", "x86_64", "cuda", self.COMMIT),
                _published_row("windows", "x86_64", "cpu", self.COMMIT)]
        return dict(rows)

    def test_row_shape_from_the_packed_asset(self):
        # engine_row reads sha256 + bytes off the archive itself and pins the published tier fields.
        import hashlib
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            asset = Path(d) / "a.tar.xz"
            asset.write_bytes(b"engine")
            (key, row), = pack_engine.engine_row("windows", "cuda", "o/r", "v1.2.3", "c" * 40,
                                                 asset=asset).items()
        self.assertEqual(key, "llama-server-windows-x86_64-cuda")
        self.assertEqual(row["sha256"], hashlib.sha256(b"engine").hexdigest())
        self.assertEqual(row["bytes"], 6)
        self.assertEqual(row["url"], "https://github.com/o/r/releases/download/v1.2.3/"
                                     "llama-server-windows-x86_64-cuda.tar.xz")
        self.assertEqual((row["cudaArchs"], row["cudaMajor"]), (pack_engine.CUDA_ARCHS, 12))
        (_, cpu), = pack_engine.engine_row("linux", "cpu", "o/r", "v1", "c", sha256="x", nbytes=1).items()
        self.assertEqual((cpu["cudaArchs"], cpu["cudaMajor"]), ("", None))

    def _patched(self, man, pinned=None):
        return (mock.patch.object(lifecycle, "_load_engine_manifest", return_value=man),
                mock.patch.object(lifecycle, "_pinned_submodule_commit",
                                  return_value=self.COMMIT if pinned is None else pinned))

    def test_every_published_row_is_selected(self):
        man = self._manifest()
        m1, m2 = self._patched(man)
        with m1, m2:
            for key, r in man.items():
                got = lifecycle._select_engine_row(r["component"], r["os"], r["cpuArch"], _QUERY[r["tier"]])
                self.assertIsNotNone(got, f"resolver did not select published row {key}")
                self.assertEqual(got["url"], r["url"])

    def test_gpu_query_matches_the_cuda_row(self):
        # The exact regression: the internal 'gpu' tier must match the published 'cuda' row.
        man = self._manifest()
        m1, m2 = self._patched(man)
        with m1, m2:
            got = lifecycle._select_engine_row("llama-server", "linux", "x86_64", "gpu")
        self.assertIsNotNone(got)
        self.assertEqual(got["tier"], "cuda")

    def test_wrong_tier_value_yields_none(self):
        # Not vacuous: a tier that is not a synonym of the query must not match.
        man = self._manifest()
        man["llama-server-linux-x86_64-cuda"]["tier"] = "rocm"
        m1, m2 = self._patched(man)
        with m1, m2:
            self.assertIsNone(lifecycle._select_engine_row("llama-server", "linux", "x86_64", "gpu"))

    def test_renamed_key_yields_none(self):
        # If publish renamed cpuArch -> arch, the resolver would stop matching (caught here).
        man = self._manifest()
        r = man["llama-server-linux-x86_64-cuda"]
        r["arch"] = r.pop("cpuArch")
        m1, m2 = self._patched(man)
        with m1, m2:
            self.assertIsNone(lifecycle._select_engine_row("llama-server", "linux", "x86_64", "gpu"))

    def test_commit_mismatch_falls_back_to_source(self):
        # A prebuilt built from a different llama.cpp commit than the checkout pins must be declined.
        man = self._manifest()
        m1, m2 = self._patched(man, pinned="c" * 40)
        with m1, m2:
            self.assertIsNone(lifecycle._select_engine_row("llama-server", "linux", "x86_64", "gpu"))


@unittest.skipUnless(os.environ.get("BOB_LIVE_MANIFEST_TEST") == "1",
                     "live manifest test — set BOB_LIVE_MANIFEST_TEST=1 (the dedicated CI job does)")
class TestPublishedManifestLive(unittest.TestCase):
    def _pin_at_tag(self, tag, component):
        sub = lifecycle._COMPONENT_SUBMODULE.get(component)
        if not sub:
            return None
        r = subprocess.run(["git", "-C", str(osenv.REPO), "rev-parse", f"{tag}:{sub}"],
                           capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None

    def test_real_published_rows_resolve(self):
        tag = lifecycle._current_release_tag() or lifecycle._latest_release_tag()
        if not tag:
            self.skipTest("no release tag resolvable (shallow clone / no tags)")
        man = lifecycle._load_engine_manifest()
        if not man:
            self.skipTest(f"no published engines.json for {tag} (offline, 404, or none uploaded)")
        for key, r in man.items():
            internal = _QUERY.get(r.get("tier"), r.get("tier"))
            # Guard against main being ahead of the tag: compare against the pin recorded AT the tag.
            pin = self._pin_at_tag(tag, r.get("component"))
            with mock.patch.object(lifecycle, "_pinned_submodule_commit", return_value=pin):
                got = lifecycle._select_engine_row(r["component"], r["os"], r["cpuArch"], internal)
            self.assertIsNotNone(got, f"resolver failed to select published row {key} (tag {tag})")
            self.assertEqual(got["url"], r["url"])


if __name__ == "__main__":
    unittest.main()
