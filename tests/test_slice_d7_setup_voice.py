"""ONE-D Slice D7 — setup-voice (provision.py:setup_voice + _install_piper) and build_whisper (build.py).
Hermetic: urllib downloads, pip, the whisper build, and the STT smoke are mocked; _install_piper is tested
against a real in-memory tar.gz. This is the last verb — after it, zero pwsh verbs remain."""
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _common  # noqa: F401 — puts scripts/ on sys.path
from bob import cli, registry

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "tools"))
import build as build_mod  # noqa: E402
import provision as prov  # noqa: E402


class TestAllVerbsPython(unittest.TestCase):
    def test_setup_voice_flipped(self):
        entry = registry.by_name()["setup-voice"]
        self.assertEqual(entry["handler"], "setup-voice")
        self.assertIn("setup-voice", cli._HANDLERS)

    def test_every_verb_maps_to_a_handler(self):
        # ONE-D/E milestone: every verb is Python (no pwsh runtime, no verbs.json table).
        for c in registry.commands():
            self.assertIn(c["handler"], cli._HANDLERS, c["name"])


class TestInstallPiper(unittest.TestCase):
    def _make_archive(self, tmp: Path) -> Path:
        stage = tmp / "piper"
        stage.mkdir()
        (stage / "piper").write_text("#!/bin/sh\n")
        (stage / "libpiper.so").write_text("lib")
        (stage / "espeak-ng-data").mkdir()
        (stage / "espeak-ng-data" / "phontab").write_text("data")
        arc = tmp / "piper.tar.gz"
        with tarfile.open(arc, "w:gz") as t:
            t.add(stage, arcname="piper")
        return arc

    def test_extracts_binary_libs_and_espeak(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        arc = self._make_archive(tmp)
        bindir = tmp / "bin"
        out = []
        with mock.patch("urllib.request.urlretrieve", side_effect=lambda url, dest: __import__("shutil").copy2(arc, dest)):
            prov._install_piper("http://x/piper.tar.gz", win=False, bindir=bindir, out=out)
        self.assertTrue((bindir / "piper").exists())
        self.assertTrue((bindir / "libpiper.so").exists())
        self.assertTrue((bindir / "espeak-ng-data" / "phontab").exists())
        self.assertTrue(any("espeak-ng-data" in line for line in out))


class TestSetupVoice(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        prov.configure({"voice": {"sttModel": "base.en", "ttsVoice": "en_GB-alan-medium"},
                        "ports": {"sttPort": 8082}, "sttPort": 8082})

    def _run(self, force=False, server_exists=False, piper_exists=False, pip_exists=True, dls=None):
        pip = self.tmp / "pip"
        if pip_exists:
            pip.write_text("x")
        server = self.tmp / "whisper-server"
        if server_exists:
            server.write_text("x")
        piper = self.tmp / "piper"
        if piper_exists:
            piper.write_text("x")

        def bin_exe(name):
            return {"whisper-server": server, "piper": piper}.get(name, self.tmp / name)

        dls = dls if dls is not None else []
        with mock.patch("osenv.bin_exe", side_effect=bin_exe), \
             mock.patch("osenv.venv_exe", return_value=pip), \
             mock.patch("osenv.os_name", return_value="linux"), \
             mock.patch.object(build_mod, "build_whisper", return_value="whisper built") as bw, \
             mock.patch.object(prov, "_dl_file", side_effect=lambda url, dest, label, force, out: dls.append((label, url))), \
             mock.patch.object(prov, "_install_piper") as ip, \
             mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)) as sub, \
             mock.patch.object(prov, "_voice_smoke", return_value="  smoke ok"):
            out = prov.setup_voice(force=force, smoke=True)
        return out, {"build_whisper": bw, "install_piper": ip, "pip": sub, "dls": dls}

    def test_builds_whisper_when_absent(self):
        out, m = self._run(server_exists=False)
        m["build_whisper"].assert_called_once()
        self.assertIn("whisper built", out)

    def test_skips_whisper_build_when_present(self):
        out, m = self._run(server_exists=True)
        m["build_whisper"].assert_not_called()

    def test_downloads_model_and_voice_with_derived_urls(self):
        _, m = self._run()
        labels = {label for label, _ in m["dls"]}
        self.assertIn("ggml-base.en.bin", labels)
        self.assertIn("en_GB-alan-medium.onnx", labels)
        # piper voice URL is derived from the voice name (lang/region/name/quality)
        voice_url = next(u for label, u in m["dls"] if label == "en_GB-alan-medium.onnx")
        self.assertIn("rhasspy/piper-voices/resolve/v1.0.0/en/en_GB/alan/medium/", voice_url)

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


class TestBuildWhisper(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.repo, True)
        self.src = self.repo / "external" / "whisper.cpp"
        self.src.mkdir(parents=True)
        (self.src / "CMakeLists.txt").write_text("# fake")
        self.bin = self.repo / "bin"
        for name, val in (("SRC_WHISPER", self.src), ("BIN", self.bin), ("REPO", self.repo)):
            p = mock.patch.object(build_mod, name, val)
            p.start()
            self.addCleanup(p.stop)

    def _fake_run(self, cap):
        def run(argv, **kw):
            cap.append([str(a) for a in argv])
            if "--build" in argv:
                out = self.src / "build" / "bin"
                out.mkdir(parents=True, exist_ok=True)
                (out / "whisper-server").write_text("ELF")
                (out / "whisper-cli").write_text("ELF")
        return run

    def test_cpu_only_configures_whisper_cuda_off_and_swaps(self):
        cap = []
        with mock.patch("osenv.os_name", return_value="linux"), \
             mock.patch("osenv.bin_exe", side_effect=lambda n: self.bin / n), \
             mock.patch.object(build_mod, "_resolve_cmake", return_value="cmake"), \
             mock.patch.object(build_mod, "_run", side_effect=self._fake_run(cap)):
            out = build_mod.build_whisper(force=True, cpu_only=True)
        configure = next(c for c in cap if any("WHISPER_CUDA" in a for a in c))
        self.assertIn("-DWHISPER_CUDA=OFF", configure)
        self.assertTrue((self.bin / "whisper-server").exists())
        self.assertIn("Built", out)

    def test_already_built_short_circuits(self):
        self.bin.mkdir()
        (self.bin / "whisper-server").write_text("x")
        (self.bin / "whisper-cli").write_text("x")
        with mock.patch("osenv.bin_exe", side_effect=lambda n: self.bin / n):
            self.assertIn("already built", build_mod.build_whisper())


if __name__ == "__main__":
    unittest.main()
