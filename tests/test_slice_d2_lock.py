"""ONE-D Slice D2 — the versions.lock WRITER + sync-gate (scripts/bob/versions.py). The reader
(load_lock/verify_model/check_reproducibility) landed with ND1; this slice adds write_lock/check_sync,
byte-identical to the pwsh writer. Hermetic: git, the model registry, and the manifest are mocked or
pointed at temp trees; nothing touches the real lock. Byte-parity vs pwsh is proven separately (a live
diff in the slice work); here we assert the port's data logic + the gate + the verb/tool wiring."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _common  # noqa: F401 — puts scripts/ on sys.path
from bob import cli, registry, versions

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "tools"))
import provision as prov  # noqa: E402

_MODELS_CFG = {
    "activeProfile": "big",
    "profiles": {
        "big": {
            "_targetVRAM": "24GB",
            "coder": {"gguf": "coder.gguf", "repo": "org/coder", "path": "c.gguf", "sizeGB": 8.0},
            "chat": {"gguf": "chat.gguf", "repo": "org/chat", "path": "h.gguf", "sizeGB": 4.0,
                     "mmproj": "mm.gguf", "revision": "v1"},
        },
        "small": {  # sorts before 'big'? no — 'big' < 'small' alphabetically; 'small' adds a new gguf
            "coder": {"gguf": "coder.gguf", "repo": "org/coder", "path": "c.gguf", "sizeGB": 8.0},
            "tiny": {"gguf": "tiny.gguf", "repo": "org/tiny", "path": "t.gguf", "sizeGB": 1.0},
        },
    },
}


class TestRegistryWiring(unittest.TestCase):
    def test_lock_flipped_to_python(self):
        entry = registry.by_name()["lock"]
        self.assertTrue(entry.get("handler"))
        self.assertEqual(entry["handler"], "lock")
        self.assertIn("lock", cli._HANDLERS)

    def test_lock_status_tool_registered_read_only(self):
        self.assertIn("lock_status", prov.DISPATCH)
        self.assertNotIn("lock_status", prov.MUTATING_TOOLS)  # read-only


class TestSubmoduleCommits(unittest.TestCase):
    def test_reads_gitlink_per_submodule(self):
        def fake_run(cmd, *a, **k):
            path = cmd[-1].split(":", 1)[1]
            return mock.Mock(returncode=0, stdout=f"sha-{path}\n")
        with mock.patch("bob.versions.subprocess.run", side_effect=fake_run):
            out = versions.submodule_commits()
        self.assertEqual(out["external/llama.cpp"], "sha-external/llama.cpp")
        self.assertEqual(set(out), set(versions.LOCK_SUBMODULES))

    def test_missing_gitlink_is_none(self):
        with mock.patch("bob.versions.subprocess.run", return_value=mock.Mock(returncode=128, stdout="")):
            self.assertIsNone(versions.submodule_commits()["external/fabric"])


class TestLockModelManifest(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.repo, True)
        (self.repo / "models").mkdir()

    def _run(self):
        with mock.patch("bob_models.load_models_config", return_value=_MODELS_CFG):
            return versions.lock_model_manifest(repo=self.repo)

    def test_union_dedup_order_and_fields(self):
        m = self._run()
        # profile order sorted (big, small); role order sorted; first gguf wins -> coder,chat (big), tiny (small)
        self.assertEqual(list(m), ["chat.gguf", "coder.gguf", "tiny.gguf"])
        self.assertEqual(m["chat.gguf"]["mmproj"], "mm.gguf")
        self.assertEqual(m["chat.gguf"]["revision"], "v1")
        self.assertEqual(m["coder.gguf"]["revision"], "main")  # default
        self.assertEqual(m["coder.gguf"]["sizeGB"], 8.0)
        self.assertIsNone(m["coder.gguf"]["sha256"])  # unfetched -> null

    def test_sha_from_manifest_then_locked_fallback(self):
        (self.repo / "models" / "manifest.json").write_text(json.dumps({"coder.gguf": {"sha256": "AAA"}}))
        (self.repo / "versions.lock").write_text(json.dumps({"models": {"tiny.gguf": {"sha256": "BBB"}}}))
        m = self._run()
        self.assertEqual(m["coder.gguf"]["sha256"], "aaa")   # manifest wins, lowercased
        self.assertEqual(m["tiny.gguf"]["sha256"], "bbb")    # falls back to the locked sha
        self.assertIsNone(m["chat.gguf"]["sha256"])          # neither -> null


class TestBuildAndText(unittest.TestCase):
    def test_object_key_order(self):
        with mock.patch("bob.versions.submodule_commits", return_value={"external/llama.cpp": "x"}), \
             mock.patch("bob.versions.lock_model_manifest", return_value={}), \
             mock.patch("bob.versions.bob_version", return_value="9.9.9"):
            obj = versions.build_lock_object()
        self.assertEqual(list(obj), ["lockVersion", "release", "submodules", "toolchain",
                                     "requirements", "models"])
        self.assertEqual(obj["release"], "9.9.9")
        self.assertEqual(obj["toolchain"], versions.LOCK_TOOLCHAIN)

    def test_text_is_2space_json_with_null_and_floats(self):
        with mock.patch("bob.versions.submodule_commits", return_value={"s": None}), \
             mock.patch("bob.versions.lock_model_manifest", return_value={"m.gguf": {"sizeGB": 5.0, "sha256": None}}), \
             mock.patch("bob.versions.bob_version", return_value="1.0.0"):
            txt = versions.lock_text()
        self.assertIn('  "release": "1.0.0"', txt)   # 2-space indent
        self.assertIn('"sha256": null', txt)         # None -> null
        self.assertIn('"sizeGB": 5.0', txt)          # float preserved
        self.assertNotIn("\t", txt)


class TestCheckSyncAndWrite(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.lock = self.tmp / "versions.lock"

    def test_in_sync_returns_zero(self):
        with mock.patch("bob.versions.lock_text", return_value='{"x": 1}'):
            self.lock.write_text('{"x": 1}\n')
            self.assertEqual(versions.check_sync(self.lock), 0)

    def test_stale_returns_one(self):
        with mock.patch("bob.versions.lock_text", return_value='{"x": 2}'):
            self.lock.write_text('{"x": 1}\n')
            self.assertEqual(versions.check_sync(self.lock), 1)

    def test_missing_returns_one(self):
        with mock.patch("bob.versions.lock_text", return_value='{}'):
            self.assertEqual(versions.check_sync(self.tmp / "nope.lock"), 1)

    def test_write_then_check_roundtrips(self):
        with mock.patch("bob.versions.lock_text", return_value='{"lockVersion": 1}'):
            p = versions.write_lock(self.lock)
            self.assertTrue(p.exists())
            self.assertTrue(self.lock.read_text().endswith("\n"))
            self.assertEqual(versions.check_sync(self.lock), 0)  # writer + gate share canonical text
            self.assertEqual(len(list(self.tmp.glob("*.tmp"))), 0)  # atomic — no leftover temp


if __name__ == "__main__":
    unittest.main()
