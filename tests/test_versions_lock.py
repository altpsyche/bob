"""versions.lock is the generated reproducibility pin — this exercises both sides of it.

Reader/validator: the committed lock parses + has the required shape; a model verifies against
its true SHA and FAILS against a wrong one (the 'wrong pinned version fails the gate' case); and
drift detection flags a moved model checksum.

Writer + sync-gate: submodule_commits/lock_model_manifest/build_lock_object/lock_text/check_sync/
write_lock produce the canonical lock text and the gate that keeps the committed file in sync.
Hermetic: git, the model registry, and the manifest are mocked or pointed at temp trees; nothing
touches the real lock."""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _common  # noqa: F401 — puts scripts/ on sys.path
from bob import versions

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
        "small": {  # 'big' < 'small' alphabetically; 'small' adds a new gguf
            "coder": {"gguf": "coder.gguf", "repo": "org/coder", "path": "c.gguf", "sizeGB": 8.0},
            "tiny": {"gguf": "tiny.gguf", "repo": "org/tiny", "path": "t.gguf", "sizeGB": 1.0},
        },
    },
}


class TestLockShape(unittest.TestCase):
    def test_committed_lock_parses_and_is_well_formed(self):
        lk = versions.load_lock()
        for key in ("lockVersion", "release", "submodules", "toolchain", "requirements", "models"):
            self.assertIn(key, lk, f"versions.lock missing '{key}'")
        self.assertIsInstance(lk["submodules"], dict)
        self.assertTrue(lk["submodules"], "no submodules pinned")
        # every submodule commit is a 40-hex sha (or None if unresolved)
        for sub, sha in lk["submodules"].items():
            if sha is not None:
                self.assertRegex(sha, r"^[0-9a-f]{40}$", sub)
        # every model entry carries repo/path/revision/sha256 (sha256 may be null until first fetch)
        self.assertTrue(lk["models"])
        for gguf, meta in lk["models"].items():
            for field in ("repo", "path", "revision", "sha256"):
                self.assertIn(field, meta, f"{gguf} missing {field}")
            if meta["sha256"] is not None:
                self.assertRegex(meta["sha256"], r"^[0-9a-f]{64}$", gguf)

    def test_cpu_tier_model_is_pinned_by_revision(self):
        # The CPU-tier GGUF served in CI must be present + revision-pinned (sha may be
        # null pre-first-fetch — TOFU-then-lock).
        lk = versions.load_lock()
        cpu = lk["models"].get("qwen2.5-0.5b-instruct-q8_0.gguf")
        self.assertIsNotNone(cpu, "CPU-tier GGUF missing from versions.lock")
        self.assertTrue(cpu["repo"] and cpu["path"] and cpu["revision"])


class TestVerifyModel(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bob-vlock-"))
        self.f = self.tmp / "m.gguf"
        self.f.write_bytes(b"hello-bob-model")
        self.good = hashlib.sha256(self.f.read_bytes()).hexdigest()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_correct_hash_passes(self):
        self.assertTrue(versions.verify_model(self.f, self.good))
        self.assertTrue(versions.verify_model(self.f, self.good.upper()))  # case-insensitive

    def test_wrong_hash_fails(self):
        # This is the deliberately-wrong-pin case the gate must catch.
        self.assertFalse(versions.verify_model(self.f, "0" * 64))

    def test_unpinned_is_a_pass(self):
        # sha256 null/empty == unpinned (e.g. CPU GGUF pre-first-fetch): nothing to verify against.
        self.assertTrue(versions.verify_model(self.f, None))
        self.assertTrue(versions.verify_model(self.f, ""))

    def test_missing_file_fails_when_pinned(self):
        self.assertFalse(versions.verify_model(self.tmp / "nope.gguf", self.good))


class TestDrift(unittest.TestCase):
    def test_model_checksum_drift_detected(self):
        tmp = Path(tempfile.mkdtemp(prefix="bob-vlock-repo-"))
        try:
            (tmp / "models").mkdir()
            (tmp / "models" / "m.gguf").write_bytes(b"actual-bytes")
            lock = {
                "submodules": {},  # skip git in this synthetic repo
                "models": {"m.gguf": {"sha256": "f" * 64}},  # pin a hash that won't match
            }
            drift = versions.check_reproducibility(repo=tmp, lock=lock)
            self.assertEqual(len(drift), 1)
            self.assertEqual(drift[0]["kind"], "model")
            self.assertEqual(drift[0]["name"], "m.gguf")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_drift_when_unpinned_or_absent(self):
        tmp = Path(tempfile.mkdtemp(prefix="bob-vlock-repo-"))
        try:
            (tmp / "models").mkdir()
            (tmp / "models" / "present.gguf").write_bytes(b"x")
            lock = {
                "submodules": {},
                "models": {
                    "present.gguf": {"sha256": None},       # unpinned -> skipped
                    "absent.gguf": {"sha256": "a" * 64},    # pinned but not on disk -> not drift
                },
            }
            self.assertEqual(versions.check_reproducibility(repo=tmp, lock=lock), [])
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class TestLockToolSurface(unittest.TestCase):
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
                                     "requirements", "tools", "models"])
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
