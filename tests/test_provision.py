"""Provisioning capability (scripts/tools/provision.py) plus its voice-setup path.

Covers the `fetch` capability (fetch loop, the versions.lock coupling: pinned -> loud-fail on
mismatch, unpinned -> TOFU; the manifest write; the tool surface) and voice provisioning
(setup_voice + _install_piper, and build_whisper in build.py). Hermetic: the model registry, curl,
SHA256 hashing, versions.lock, urllib downloads, pip, the whisper build, and the STT smoke are all
mocked, MODELS_DIR is redirected to a temp tree, and _install_piper runs against a real in-memory
tar.gz -- so nothing hits the network, curl, a GPU, or real state."""
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _common  # noqa: F401 — puts scripts/ on sys.path

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


class TestFetchToolSurface(unittest.TestCase):
    def test_tool_registered_and_mutating(self):
        self.assertIn("fetch_models", prov.DISPATCH)  # provision.py also carries the voice-setup tools
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

    def test_curl_retries_and_aborts_stalls(self):
        seen = []

        def fake_run(cmd, *a, **k):
            seen.extend(cmd)
            Path(cmd[cmd.index("-o") + 1]).write_text("bytes")
            return mock.Mock(returncode=0)
        with mock.patch("provision.shutil.which", return_value="/usr/bin/curl"), \
             mock.patch("provision.subprocess.run", side_effect=fake_run):
            prov._download("http://x/y", self.dest, [])
        for flag in ("--retry", "--speed-limit", "--speed-time", "-C"):
            self.assertIn(flag, seen)

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


class TestInstallPiper(unittest.TestCase):
    def _make_archive(self, tmp: Path) -> Path:
        stage = tmp / "piper"
        stage.mkdir()
        (stage / "piper").write_text("#!/bin/sh\n")
        (stage / "libpiper.so.1.2.0").write_text("lib")
        if sys.platform != "win32":
            import os
            os.symlink("libpiper.so.1.2.0", stage / "libpiper.so.1")
        (stage / "espeak-ng-data").mkdir()
        (stage / "espeak-ng-data" / "phontab").write_text("data")
        arc = tmp / "piper.tar.gz"
        with tarfile.open(arc, "w:gz") as t:
            t.add(stage, arcname="piper")
        return arc

    def test_extracts_binary_libs_and_espeak(self):
        import hashlib
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        arc = self._make_archive(tmp)
        sha = hashlib.sha256(arc.read_bytes()).hexdigest()
        bindir = tmp / "bin"
        out = []
        prov._install_piper(arc.as_uri(), win=False, bindir=bindir, out=out, sha256=sha)
        self.assertTrue((bindir / "piper").exists())
        self.assertTrue((bindir / "libpiper.so.1.2.0").exists())
        if sys.platform != "win32":
            self.assertTrue((bindir / "libpiper.so.1").is_symlink())   # SONAME link kept, not a copy
        self.assertTrue((bindir / "espeak-ng-data" / "phontab").exists())
        self.assertTrue(any("espeak-ng-data" in line for line in out))

    def test_unverified_archive_is_refused(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        arc = self._make_archive(tmp)
        with self.assertRaises(RuntimeError):
            prov._install_piper(arc.as_uri(), win=False, bindir=tmp / "bin", out=[], sha256="0" * 64)
        self.assertFalse((tmp / "bin" / "piper").exists())


class TestDlFile(unittest.TestCase):
    def test_partial_download_never_counts_as_present(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        dest = tmp / "model.bin"
        with mock.patch("urllib.request.urlopen", side_effect=OSError("reset mid-transfer")):
            with self.assertRaises(OSError):
                prov._dl_file("https://example.invalid/model.bin", dest, "model.bin", False, [])
        self.assertFalse(dest.exists())   # the next run downloads again instead of skipping


class TestSetupVoice(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        prov.configure({"voice": {"sttModel": "base.en", "ttsVoice": "en_GB-alan-medium"},
                        "ports": {"sttPort": 8082}, "sttPort": 8082})

    def _run(self, force=False, piper_exists=False, pip_exists=True, dls=None, gpu=None):
        prov.configure({"voice": {"sttModel": "base.en", "ttsVoice": "en_GB-alan-medium"},
                        "ports": {"sttPort": 8082}, "sttPort": 8082})
        pip = self.tmp / "pip"
        if pip_exists:
            pip.write_text("x")
        piper = self.tmp / "piper"
        if piper_exists:
            piper.write_text("x")

        def bin_exe(name):
            return {"piper": piper}.get(name, self.tmp / name)

        dls = dls if dls is not None else []
        with mock.patch("osenv.bin_exe", side_effect=bin_exe), \
             mock.patch("osenv.venv_exe", return_value=pip), \
             mock.patch("osenv.os_name", return_value="linux"), \
             mock.patch("osenv.gpu_info", return_value=gpu), \
             mock.patch.object(prov, "_dl_file",
                               side_effect=lambda url, dest, label, force, out, sha256=None: dls.append((label, url))), \
             mock.patch.object(prov, "_install_piper") as ip, \
             mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)) as sub, \
             mock.patch.object(prov, "_voice_smoke", return_value="  smoke ok"):
            out = prov.setup_voice(force=force, smoke=True)
        return out, {"install_piper": ip, "pip": sub, "dls": dls}

    def test_downloads_voice_with_derived_urls(self):
        _, m = self._run()
        labels = {label for label, _ in m["dls"]}
        self.assertFalse(any(label.startswith("ggml-") for label in labels))   # no whisper.cpp model
        self.assertIn("en_GB-alan-medium.onnx", labels)
        # piper voice URL is derived from the voice name (lang/region/name/quality)
        voice_url = next(u for label, u in m["dls"] if label == "en_GB-alan-medium.onnx")
        self.assertIn("rhasspy/piper-voices/resolve/v1.0.0/en/en_GB/alan/medium/", voice_url)

    def test_faster_whisper_fetches_ct2_model(self):
        # faster-whisper is the only STT engine: fetch the CT2 model dir from Systran.
        _, m = self._run()
        labels = {label for label, _ in m["dls"]}
        self.assertIn("faster-whisper/base.en/model.bin", labels)
        self.assertIn("faster-whisper/base.en/config.json", labels)
        self.assertIn("faster-whisper/base.en/tokenizer.json", labels)
        model_url = next(u for label, u in m["dls"] if label == "faster-whisper/base.en/model.bin")
        self.assertIn("Systran/faster-whisper-base.en/resolve/main/model.bin", model_url)

    def test_faster_whisper_installs_dep(self):
        _, m = self._run()
        pip_calls = [c.args[0] for c in m["pip"].call_args_list]
        self.assertTrue(any("faster-whisper" in c for c in pip_calls))
        # resolved against the pinned lock, so a voice install never moves a pinned package
        fw = next(c for c in pip_calls if "faster-whisper" in c)
        self.assertIn("-c", fw)

    def test_gpu_stt_libs_installed_when_gpu_present(self):
        # optional GPU upgrade: with an NVIDIA GPU detected, install the cu12 cuBLAS/cuDNN wheels.
        _, m = self._run(gpu={"name": "RTX 5080"})
        pip_calls = [c.args[0] for c in m["pip"].call_args_list]
        self.assertTrue(any("nvidia-cublas-cu12" in c for c in pip_calls))

    def test_gpu_stt_libs_skipped_without_gpu(self):
        _, m = self._run(gpu=None)   # CPU tier: no cu12 wheels, CPU int8 is used
        pip_calls = [c.args[0] for c in m["pip"].call_args_list]
        self.assertFalse(any("nvidia-cublas-cu12" in c for c in pip_calls))

    def test_installs_piper_when_absent(self):
        _, m = self._run(piper_exists=False)
        m["install_piper"].assert_called_once()

    def test_installs_audio_deps(self):
        _, m = self._run()
        pip_calls = [c.args[0] for c in m["pip"].call_args_list]
        self.assertTrue(any("sounddevice" in c and "numpy" in c for c in pip_calls))

    def test_missing_venv_raises(self):
        with self.assertRaises(RuntimeError):
            self._run(pip_exists=False)


class TestVoiceSmokeStdlib(unittest.TestCase):
    def test_multipart_is_stdlib_and_well_formed(self):
        # the voice smoke posts via stdlib urllib (no requests) so it runs under the bare kernel too.
        body, ctype = prov._multipart({"temperature": "0.0"}, "probe.wav", b"\x00\x01\x02")
        self.assertTrue(ctype.startswith("multipart/form-data; boundary=----bob"))
        self.assertIn(b'name="temperature"', body)
        self.assertIn(b'filename="probe.wav"', body)
        self.assertIn(b"\x00\x01\x02", body)
        self.assertTrue(body.rstrip().endswith(b"--"))


if __name__ == "__main__":
    unittest.main()
