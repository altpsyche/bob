"""/voice shell mode + bob_voice capability core: speech-safe formatting, STT client, TTS synth,
and the shell round-trip (mic→STT→_run_turn→TTS). Everything network/audio is faked; no model call, no
whisper/piper server, no real mic. The point is that /voice wraps the SAME _run_turn as text, so the
turn path (memory/tools/persona/retry) is exercised by the existing shell tests — here we prove the loop
glue and the STT/TTS edges."""
import unittest
from unittest import mock

import _common
import bob_voice

try:
    import fastapi          # noqa: F401
    import httpx            # noqa: F401 — TestClient transport
    # python-multipart: FastAPI needs it to BUILD the File/Form routes (at import), or the module raises.
    # Import name changed at 0.0.12 (multipart -> python_multipart); accept either so the pin (0.0.32) counts.
    try:
        import python_multipart  # noqa: F401
    except ImportError:
        import multipart         # noqa: F401
    _HAS_FASTAPI = True
except ImportError:                       # runtime-venv deps; present in CI core-suite, maybe not locally
    _HAS_FASTAPI = False


class TestFormatForSpeech(unittest.TestCase):
    def test_strips_markdown_and_typography(self):
        out = bob_voice.format_for_speech(
            "**Bold** and *italic*, `code`, [link](http://x), — dash, “quote”, …")
        self.assertNotIn("*", out)
        self.assertNotIn("`", out)
        self.assertNotIn("](", out)
        self.assertIn("link", out)          # link text kept, url dropped
        self.assertIn("Bold", out)
        self.assertIn(", dash", out)         # em dash → ", "
        self.assertIn('"quote"', out)        # smart quotes normalized
        self.assertIn("...", out)            # ellipsis normalized

    def test_headings_bullets_and_fences(self):
        out = bob_voice.format_for_speech("## Title\n- one\n- two\n```py\ncode\n```\ntext")
        self.assertNotIn("#", out)
        self.assertNotIn("```", out)
        self.assertNotIn("- one", out)
        self.assertIn("one", out)
        self.assertIn("text", out)

    def test_empty_and_none_safe(self):
        self.assertEqual(bob_voice.format_for_speech(""), "")
        self.assertEqual(bob_voice.format_for_speech(None), "")


class TestSttClient(unittest.TestCase):
    def test_stt_port_from_defaults(self):
        # neutral config has no top-level sttPort → falls to config/defaults.json ports (8082)
        self.assertEqual(bob_voice.stt_port(_common.fake_config()), 8082)

    def test_stt_ready_true_when_port_open(self):
        with mock.patch("socket.create_connection", return_value=mock.MagicMock()):
            self.assertTrue(bob_voice.stt_ready(_common.fake_config()))

    def test_stt_ready_false_when_refused(self):
        with mock.patch("socket.create_connection", side_effect=OSError):
            self.assertFalse(bob_voice.stt_ready(_common.fake_config()))

    def test_transcribe_connection_error_raises_actionable(self):
        import requests
        with mock.patch("requests.post", side_effect=requests.exceptions.ConnectionError), \
             mock.patch("builtins.open", mock.mock_open(read_data=b"RIFF")):
            with self.assertRaises(RuntimeError) as cm:
                bob_voice.transcribe("/tmp/x.wav", 8082)
        self.assertIn("bob whisper", str(cm.exception))

    def test_transcribe_timeout_wrapped(self):
        import requests
        with mock.patch("requests.post", side_effect=requests.exceptions.Timeout), \
             mock.patch("builtins.open", mock.mock_open(read_data=b"RIFF")):
            with self.assertRaises(RuntimeError) as cm:
                bob_voice.transcribe("/tmp/x.wav", 8082)
        self.assertIn("timed out", str(cm.exception))

    def test_transcribe_http_500_wrapped(self):
        # engine crashed mid-request -> raise_for_status HTTPError -> friendly RuntimeError, not a traceback
        import requests
        resp = mock.Mock(status_code=500)
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=resp)
        with mock.patch("requests.post", return_value=resp), \
             mock.patch("builtins.open", mock.mock_open(read_data=b"RIFF")):
            with self.assertRaises(RuntimeError) as cm:
                bob_voice.transcribe("/tmp/x.wav", 8082)
        self.assertIn("500", str(cm.exception))

    def test_transcribe_bad_json_wrapped(self):
        resp = mock.Mock(status_code=200)
        resp.raise_for_status.return_value = None
        resp.json.side_effect = ValueError("no json")   # json.JSONDecodeError is a ValueError
        with mock.patch("requests.post", return_value=resp), \
             mock.patch("builtins.open", mock.mock_open(read_data=b"RIFF")):
            with self.assertRaises(RuntimeError) as cm:
                bob_voice.transcribe("/tmp/x.wav", 8082)
        self.assertIn("unreadable", str(cm.exception))

    def test_listen_empty_capture_returns_blank(self):
        with mock.patch.object(bob_voice.osenv, "record_audio", return_value=b""):
            self.assertEqual(bob_voice.listen(_common.fake_config()), "")

    def test_listen_records_then_transcribes(self):
        with mock.patch.object(bob_voice.osenv, "record_audio", return_value=b"RIFFxxxx"), \
             mock.patch.object(bob_voice, "transcribe", return_value="hello world") as tr:
            out = bob_voice.listen(_common.fake_config())
        self.assertEqual(out, "hello world")
        self.assertEqual(tr.call_args.args[1], 8082)   # transcribed against the STT port


class TestSpeak(unittest.TestCase):
    def test_speak_empty_text_noop(self):
        self.assertFalse(bob_voice.speak("   ", _common.fake_config()))

    def test_speak_missing_piper_returns_false(self):
        with mock.patch.object(bob_voice, "_piper_exe", return_value=_FakePath(exists=False)):
            self.assertFalse(bob_voice.speak("hi", _common.fake_config()))

    def test_speak_synthesizes_and_plays(self):
        played = {}
        proc = mock.MagicMock(returncode=0, stderr=b"")
        with mock.patch.object(bob_voice, "_piper_exe", return_value=_FakePath(exists=True)), \
             mock.patch.object(bob_voice, "_voice_model", return_value=_FakePath(exists=True)), \
             mock.patch.object(bob_voice.subprocess, "run", return_value=proc) as run, \
             mock.patch.object(bob_voice.osenv, "play_audio",
                               side_effect=lambda p: played.setdefault("path", p) or True):
            ok = bob_voice.speak("hello", _common.fake_config())
        self.assertTrue(ok)
        self.assertEqual(run.call_args.kwargs["input"], b"hello")   # text piped to piper stdin
        self.assertIn("path", played)                                # WAV handed to the player

    def test_speak_piper_failure_returns_false(self):
        proc = mock.MagicMock(returncode=1, stderr=b"boom")
        with mock.patch.object(bob_voice, "_piper_exe", return_value=_FakePath(exists=True)), \
             mock.patch.object(bob_voice, "_voice_model", return_value=_FakePath(exists=True)), \
             mock.patch.object(bob_voice.subprocess, "run", return_value=proc):
            self.assertFalse(bob_voice.speak("hello", _common.fake_config()))


class TestCliHandlers(unittest.TestCase):
    """The bob voice/listen/transcribe/speak CLI verbs route to Python handlers.
    Faked bob_voice; no server, no mic, no piper."""

    def setUp(self):
        import bob_core
        from bob import cli
        self.cli = cli
        self._orig_load = bob_core.load_config
        bob_core.load_config = lambda: _common.fake_config()

    def tearDown(self):
        import bob_core
        bob_core.load_config = self._orig_load

    def test_listen_prints_transcript(self):
        with mock.patch.object(bob_voice, "listen", return_value="hello there"), \
             mock.patch("builtins.print") as pr:
            rc = self.cli._handle_listen([])
        self.assertEqual(rc, 0)
        self.assertIn(mock.call("hello there"), pr.call_args_list)   # to stdout

    def test_listen_no_speech_returns_1(self):
        with mock.patch.object(bob_voice, "listen", return_value=""):
            self.assertEqual(self.cli._handle_listen([]), 1)

    def test_listen_server_down_returns_1(self):
        with mock.patch.object(bob_voice, "listen", side_effect=RuntimeError("whisper down")):
            self.assertEqual(self.cli._handle_listen([]), 1)

    def test_transcribe_requires_a_file(self):
        self.assertEqual(self.cli._handle_transcribe([]), 1)

    def test_transcribe_missing_file_returns_1(self):
        self.assertEqual(self.cli._handle_transcribe(["/no/such/audio.wav"]), 1)

    def test_transcribe_prints_transcript(self):
        with mock.patch("os.path.exists", return_value=True), \
             mock.patch.object(bob_voice, "transcribe", return_value="the text"), \
             mock.patch("builtins.print") as pr:
            rc = self.cli._handle_transcribe(["clip.wav"])
        self.assertEqual(rc, 0)
        self.assertIn(mock.call("the text"), pr.call_args_list)

    def test_speak_empty_stdin_is_noop_ok(self):
        with mock.patch("sys.stdin") as stdin:
            stdin.read.return_value = "   "
            self.assertEqual(self.cli._handle_speak([]), 0)

    def test_speak_success_and_failure(self):
        with mock.patch.object(bob_voice, "speak", return_value=True):
            self.assertEqual(self.cli._handle_speak(["hi"]), 0)
        with mock.patch.object(bob_voice, "speak", return_value=False):
            self.assertEqual(self.cli._handle_speak(["hi"]), 1)

    def test_voice_default_is_chat_role_no_tools(self):
        from bob import shell
        with mock.patch.object(shell, "run_voice", return_value=0) as rv:
            self.cli._handle_voice([])
        # default (no --agent): voice role resolved + tools off
        self.assertEqual(rv.call_args.kwargs["role"], "chat")   # roleTable voice → chat fallback
        self.assertTrue(rv.call_args.kwargs["no_tools"])

    def test_voice_pro_uses_pro_role(self):
        from bob import shell
        with mock.patch.object(shell, "run_voice", return_value=0) as rv:
            self.cli._handle_voice(["--pro"])
        self.assertEqual(rv.call_args.kwargs["role"], "chat-pro")

    def test_voice_agent_keeps_tools(self):
        from bob import shell
        with mock.patch.object(shell, "run_voice", return_value=0) as rv:
            self.cli._handle_voice(["--agent"])
        # --agent: shell default (agent role + tools) — no role/no_tools override passed
        self.assertNotIn("no_tools", rv.call_args.kwargs)
        self.assertNotIn("role", rv.call_args.kwargs)


class TestRecordAudioMicHardening(unittest.TestCase):
    """A present-but-unusable capture device raises sd.PortAudioError (not ImportError); osenv.record_audio
    must wrap it as a friendly RuntimeError so the /voice loop reports 'check your mic', not a traceback."""

    def test_portaudio_error_wrapped_as_runtime_error(self):
        import sys
        import types
        fake_sd = types.ModuleType("sounddevice")

        class PortAudioError(Exception):
            pass

        fake_sd.PortAudioError = PortAudioError

        def _input_stream(*a, **k):
            raise PortAudioError("Error opening InputStream: no default input device")

        fake_sd.InputStream = _input_stream
        fake_np = types.ModuleType("numpy")   # imported alongside sd; unused before the raise
        with mock.patch.dict(sys.modules, {"sounddevice": fake_sd, "numpy": fake_np}):
            import osenv
            with self.assertRaises(RuntimeError) as cm:
                osenv.record_audio()
        self.assertIn("microphone", str(cm.exception))


class TestSttEngineDispatch(unittest.TestCase):
    """stack._start_stt_bg picks the STT backend from voice.sttEngine (faster-whisper default)."""

    def test_dispatch_selects_backend(self):
        import stack
        with mock.patch.object(stack, "_start_whisper_bg", return_value="wc"), \
             mock.patch.object(stack, "_start_faster_whisper_bg", return_value="fw"):
            self.assertEqual(stack._start_stt_bg({"voice": {"sttEngine": "whisper.cpp"}}), "wc")
            self.assertEqual(stack._start_stt_bg({"voice": {"sttEngine": "faster-whisper"}}), "fw")
            self.assertEqual(stack._start_stt_bg({"voice": {}}), "fw")   # default is faster-whisper


@unittest.skipUnless(_HAS_FASTAPI, "fastapi/httpx not installed (runtime venv only)")
class TestFasterWhisperServer(unittest.TestCase):
    """The faster-whisper STT server matches the whisper.cpp /inference contract (multipart file ->
    {'text': ...}) so the client/lifecycle stay engine-agnostic. The CT2 model is faked (no wheel needed)."""

    def _client(self):
        import faster_whisper_server as fws
        from fastapi.testclient import TestClient
        return fws, TestClient(fws.app)

    def test_inference_returns_text_from_segments(self):
        fws, client = self._client()
        seg = mock.Mock(text=" hello world")
        fws._model = mock.Mock(transcribe=mock.Mock(return_value=([seg], object())))
        r = client.post("/inference", files={"file": ("a.wav", b"RIFFxxxx", "audio/wav")},
                        data={"temperature": "0.0", "response_format": "json"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["text"], "hello world")

    def test_inference_empty_upload_returns_blank(self):
        fws, client = self._client()
        fws._model = mock.Mock()
        r = client.post("/inference", files={"file": ("a.wav", b"", "audio/wav")})
        self.assertEqual(r.json()["text"], "")

    def test_inference_503_when_model_not_loaded(self):
        fws, client = self._client()
        fws._model = None
        r = client.post("/inference", files={"file": ("a.wav", b"RIFF", "audio/wav")})
        self.assertEqual(r.status_code, 503)

    def test_health(self):
        fws, client = self._client()
        fws._model = None
        self.assertEqual(client.get("/health").json()["status"], "loading")


class _FakePath:
    """A Path stand-in whose .exists() is scripted and str() is stable (for piper/voice existence)."""
    def __init__(self, exists: bool):
        self._exists = exists

    def exists(self) -> bool:
        return self._exists

    def __str__(self) -> str:
        return "/fake/piper"

    def __fspath__(self) -> str:
        return "/fake/piper"


if __name__ == "__main__":
    unittest.main()
