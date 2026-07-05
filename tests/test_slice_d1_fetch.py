"""ONE-D Slice D1 — the `fetch` capability (scripts/tools/provision.py). Hermetic: the model registry,
curl, SHA256 hashing, and versions.lock are all mocked, and MODELS_DIR is redirected to a temp tree, so
nothing hits the network, curl, a GPU, or real state. Verifies the fetch loop, the versions.lock coupling
(pinned -> loud-fail on mismatch; unpinned -> TOFU), the manifest write, and the registry/tool wiring."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _common  # noqa: F401 — puts scripts/ on sys.path
from bob import cli, registry

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "tools"))
import provision as prov  # noqa: E402

CFG = {"port": 8080}

_ROLES = {
    "coder": {"gguf": "coder.gguf", "repo": "org/coder", "path": "q4.gguf", "sizeGB": 8},
    "chat": {"gguf": "chat.gguf", "repo": "org/chat", "path": "q4.gguf", "sizeGB": 4, "mmproj": "mm.gguf"},
    "alias": {"gguf": "coder.gguf", "repo": "org/coder", "path": "q4.gguf", "sizeGB": 8},  # dup gguf
}


def _patch_registry(roles=None):
    roles = roles if roles is not None else _ROLES
    return mock.patch.multiple(
        "bob_models",
        load_models_config=mock.Mock(return_value={"profiles": {"p": roles}, "activeProfile": "p"}),
        resolve_profile_name=mock.Mock(return_value="p"),
        profile_roles=mock.Mock(return_value=roles),
    )


class TestRegistryWiring(unittest.TestCase):
    def test_fetch_flipped_to_python(self):
        entry = registry.by_name()["fetch"]
        self.assertTrue(entry.get("handler"))
        self.assertEqual(entry["handler"], "fetch")
        self.assertIn("fetch", cli._HANDLERS)

    def test_tool_registered_and_mutating(self):
        self.assertIn("fetch_models", prov.DISPATCH)  # provision.py grows with later ONE-D slices
        self.assertEqual(prov.MUTATING_TOOLS, {"fetch_models"})  # fetch is the only mutating one


class TestResolveFetchSet(unittest.TestCase):
    def test_dedupes_by_gguf_and_carries_mmproj(self):
        with _patch_registry():
            name, models = prov.resolve_fetch_set()
        self.assertEqual(name, "p")
        ggufs = [m["gguf"] for m in models]
        self.assertEqual(ggufs, ["coder.gguf", "chat.gguf"])  # 'alias' dropped (dup gguf)
        chat = next(m for m in models if m["gguf"] == "chat.gguf")
        self.assertEqual(chat["mmproj"], "mm.gguf")


class TestModelRevision(unittest.TestCase):
    def test_pinned_and_fallback(self):
        lock = {"models": {"coder.gguf": {"revision": "abc123"}}}
        self.assertEqual(prov._model_revision("coder.gguf", lock), "abc123")
        self.assertEqual(prov._model_revision("chat.gguf", lock), "main")
        self.assertEqual(prov._model_revision("coder.gguf", None), "main")


class TestVerifyDownload(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.f = self.tmp / "coder.gguf"
        self.f.write_text("data")

    def test_pinned_match_returns_sha(self):
        lock = {"models": {"coder.gguf": {"sha256": "ABCDEF"}}}
        with mock.patch("bob.versions.sha256_file", return_value="abcdef"):
            self.assertEqual(prov._verify_download(self.f, "coder.gguf", lock), "abcdef")
        self.assertTrue(self.f.exists())

    def test_pinned_mismatch_deletes_and_raises(self):
        lock = {"models": {"coder.gguf": {"sha256": "expected"}}}
        with mock.patch("bob.versions.sha256_file", return_value="actual"):
            with self.assertRaises(RuntimeError):
                prov._verify_download(self.f, "coder.gguf", lock)
        self.assertFalse(self.f.exists())  # bad file deleted

    def test_unpinned_is_tofu(self):
        with mock.patch("bob.versions.sha256_file", return_value="cafe"):
            self.assertEqual(prov._verify_download(self.f, "coder.gguf", None), "cafe")
        self.assertTrue(self.f.exists())


class TestUpdateManifest(unittest.TestCase):
    def test_atomic_write_and_content(self):
        import json
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        with mock.patch.object(prov, "MODELS_DIR", tmp):
            prov._update_manifest("coder.gguf", "http://x/y", 8, "deadbeef")
            prov._update_manifest("chat.gguf", "http://x/z", 4, "feedface")
        manifest = json.loads((tmp / "manifest.json").read_text())
        self.assertEqual(manifest["coder.gguf"]["sha256"], "deadbeef")
        self.assertEqual(manifest["coder.gguf"]["sizeGB"], 8)
        self.assertIn("verifiedAt", manifest["chat.gguf"])
        self.assertEqual(len(list(tmp.glob("*.tmp"))), 0)  # no leftover temp


class TestDownload(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.dest = self.tmp / "coder.gguf"

    def _run(self, returncode):
        def fake_run(cmd, *a, **k):
            Path(cmd[cmd.index("-o") + 1]).write_text("bytes")  # curl writes the .part
            return mock.Mock(returncode=returncode)
        with mock.patch("provision.shutil.which", return_value="/usr/bin/curl"), \
             mock.patch("provision.subprocess.run", side_effect=fake_run):
            prov._download("http://x/y", self.dest, [])

    def test_success_moves_part_to_dest(self):
        self._run(0)
        self.assertTrue(self.dest.exists())
        self.assertFalse(Path(f"{self.dest}.part").exists())

    def test_curl22_deletes_part_and_raises(self):
        with self.assertRaises(RuntimeError):
            self._run(22)
        self.assertFalse(Path(f"{self.dest}.part").exists())  # poisoned prefix removed

    def test_other_exit_keeps_part_for_resume(self):
        with self.assertRaises(RuntimeError):
            self._run(7)
        self.assertTrue(Path(f"{self.dest}.part").exists())  # valid partial kept

    def test_missing_curl_raises(self):
        with mock.patch("provision.shutil.which", return_value=None):
            with self.assertRaises(RuntimeError):
                prov._download("http://x", self.dest, [])


class TestFetchModelsFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)

    def test_list_only_reports_present_and_missing(self):
        (self.tmp / "coder.gguf").write_text("x")  # coder present, chat missing
        with _patch_registry(), mock.patch.object(prov, "MODELS_DIR", self.tmp):
            out = prov.fetch_models(list_only=True)
        self.assertIn("Profile 'p': 2 models", out)
        self.assertIn("coder.gguf", out)
        self.assertIn("present", out)
        self.assertIn("MISSING", out)
        self.assertIn("nothing downloaded", out)

    def test_fetch_downloads_missing_and_records(self):
        calls = []
        with _patch_registry(), \
             mock.patch.object(prov, "MODELS_DIR", self.tmp), \
             mock.patch.object(prov, "_load_lock", return_value=None), \
             mock.patch.object(prov, "_download", side_effect=lambda url, dest, h: dest.write_text("m")), \
             mock.patch.object(prov, "_verify_download", side_effect=lambda f, g, lk: "sha_" + g), \
             mock.patch.object(prov, "_update_manifest", side_effect=lambda *a: calls.append(a[0])):
            out = prov.fetch_models()
        # coder.gguf, chat.gguf, and chat's mmproj (mm.gguf) all downloaded + manifested
        self.assertIn("done    coder.gguf", out)
        self.assertIn("done    mm.gguf", out)
        self.assertEqual(set(calls), {"coder.gguf", "chat.gguf", "mm.gguf"})

    def test_existing_files_are_skipped(self):
        (self.tmp / "coder.gguf").write_text("x")
        (self.tmp / "chat.gguf").write_text("x")
        (self.tmp / "mm.gguf").write_text("x")
        with _patch_registry(), mock.patch.object(prov, "MODELS_DIR", self.tmp), \
             mock.patch.object(prov, "_load_lock", return_value=None), \
             mock.patch.object(prov, "_download", side_effect=AssertionError("should not download")):
            out = prov.fetch_models()
        self.assertEqual(out.count("exists"), 3)


if __name__ == "__main__":
    unittest.main()
