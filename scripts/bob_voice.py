"""Voice capability core: STT (whisper client) + TTS (piper) + speech-safe text formatting,
on the Python agent loop. One importable core shared by
`bob-voice-capture.py` (the STT CLI), the shell `/voice` mode (bob.shell), and the agent tools. Mic-in
/ speaker-out go through the osenv seam; STT/TTS stay standalone servers/binaries (whisper POST /
piper binary) — this module is the client + the round-trip glue, not the servers.
The `/voice` mode wraps the same agent turn as text, so voice inherits memory + write-back
+ one persona + retry + logging + tools automatically."""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import osenv
from bob_core import load_defaults

REPO = Path(__file__).resolve().parent.parent

# Voice settings live once in config/defaults.json.runtime.voice.
# These per-key fallbacks read from there rather than re-inlining a literal (ports go through _port).
_VOICE_DEFAULTS = load_defaults().get("runtime", {}).get("voice", {})


# --- speech-safe text ----------------------------------------------------------------------------
# Strip markdown/typography before TTS so piper speaks words, not asterisks and backticks. System prompts
# ask the model to avoid formatting; this is the reliable safety net. Ordered: typographic normalization
# first, then structural markdown removal, then whitespace cleanup.
_TYPO = {
    "—": ", ",   # em dash —
    "–": " to ", # en dash –
    "‘": "'", "’": "'",   # single quotes
    "“": '"', "”": '"',   # double quotes
    "…": "...", # ellipsis
    " ": " ",   # non-breaking space
}
_MD_SUBS = [
    (re.compile(r"```[a-zA-Z]*\r?\n?"), ""),   # fenced code blocks — strip the opening fence
    (re.compile(r"`([^`]+)`"), r"\1"),          # inline code
    (re.compile(r"\*\*([^*]+)\*\*"), r"\1"),   # **bold**
    (re.compile(r"\*([^*\n]+)\*"), r"\1"),      # *italic*
    (re.compile(r"__([^_]+)__"), r"\1"),        # __bold__
    (re.compile(r"_([^_\n]+)_"), r"\1"),        # _italic_
    (re.compile(r"(?m)^#{1,6}\s+"), ""),         # headings
    (re.compile(r"(?m)^[ \t]*[-*+]\s+"), ""),   # unordered bullets
    (re.compile(r"(?m)^[ \t]*\d+\.\s+"), ""),   # numbered lists (keep the text)
    (re.compile(r"(?m)^[-*_]{3,}\s*$"), ""),    # horizontal rules
    (re.compile(r"\[([^\]]+)\]\([^\)]+\)"), r"\1"),  # [link text](url)
    (re.compile(r"(?m)^>\s?"), ""),              # blockquotes
]


def format_for_speech(text: str) -> str:
    """Strip markdown/typographic formatting so a TTS engine speaks the words, not the syntax."""
    t = text or ""
    for src, dst in _TYPO.items():
        t = t.replace(src, dst)
    for pattern, repl in _MD_SUBS:
        t = pattern.sub(repl, t)
    t = t.replace("```", "")           # any residual fence markers
    t = t.replace("|", " ")            # table pipes → space
    t = re.sub(r"[ \t]{2,}", " ", t)   # collapse runs of spaces/tabs
    t = re.sub(r"(\r?\n){3,}", "\n\n", t)  # collapse excess blank lines
    return t.strip()


# --- STT: whisper-server client (record via the osenv seam, transcribe via HTTP) -----------------

def stt_port(config: dict) -> int:
    from bob_core import _port
    return _port(config, "sttPort")


def stt_ready(config: dict) -> bool:
    """True if the whisper STT port is open (TCP connect; mirrors bob_core.check_litellm)."""
    import socket
    try:
        with socket.create_connection(("localhost", stt_port(config)), timeout=2):
            return True
    except OSError:
        return False


def transcribe(wav_path: str, port: int) -> str:
    """POST a WAV file to the STT server (whisper.cpp or faster-whisper share the /inference contract),
    return the transcript text. Every backend failure (unreachable, timeout, 5xx crash mid-request,
    malformed body) is wrapped as a RuntimeError with an actionable message, so the /voice loop and the
    STT CLI can recover instead of surfacing a raw traceback. Single source for both callers."""
    import requests

    url = f"http://localhost:{port}/inference"
    resp = None
    try:
        with open(wav_path, "rb") as f:
            resp = requests.post(
                url,
                files={"file": ("audio.wav", f, "audio/wav")},
                data={"temperature": "0.0", "response_format": "json"},
                timeout=30,
            )
        resp.raise_for_status()
        return resp.json().get("text", "").strip()
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError(
            f"STT server not reachable at {url}. Start it with: bob whisper (or bob up)"
        ) from e
    except requests.exceptions.Timeout as e:
        raise RuntimeError(
            f"STT server timed out at {url} — the engine may be overloaded or stuck."
        ) from e
    except requests.exceptions.HTTPError as e:
        code = resp.status_code if resp is not None else "?"
        raise RuntimeError(
            f"STT server error ({code}) at {url} — the engine may have crashed mid-request. "
            "Check logs/whisper.log."
        ) from e
    except ValueError as e:   # json.JSONDecodeError (a ValueError) — malformed / non-JSON body
        raise RuntimeError(f"STT server returned an unreadable response from {url}.") from e


def record(config: dict, silence_sec: float = None) -> bytes:
    """Record the mic until silence (osenv seam); returns WAV bytes (b'' if nothing was captured). Split
    from listen() so callers (the /voice loop) can show a 'transcribing…' phase between record and STT."""
    voice = config.get("voice", {}) if isinstance(config, dict) else {}
    secs = (silence_sec if silence_sec is not None
            else float(voice.get("silenceSec", _VOICE_DEFAULTS.get("silenceSec", 1.5))))
    rms_floor = int(voice.get("rmsSilence", _VOICE_DEFAULTS.get("rmsSilence", 200)))
    return osenv.record_audio(secs, rms_silence=rms_floor)


def transcribe_bytes(wav_bytes: bytes, port: int) -> str:
    """Transcribe raw WAV bytes via whisper-server ('' if empty). The temp-file seam shared by listen()
    and the /voice loop, so both handle STT identically."""
    if not wav_bytes:
        return ""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav_bytes)
        tmp = f.name
    try:
        return transcribe(tmp, port)
    finally:
        Path(tmp).unlink(missing_ok=True)


def listen(config: dict, silence_sec: float = None) -> str:
    """Record the mic until silence (osenv seam), transcribe via whisper-server, return the transcript
    ('' when nothing was captured). Raises RuntimeError if the audio stack or the server is missing."""
    return transcribe_bytes(record(config, silence_sec), stt_port(config))


# --- TTS: piper binary (synth to WAV, play through the osenv seam) --------------------------------

def _piper_exe() -> Path:
    return REPO / "bin" / ("piper.exe" if osenv.is_windows() else "piper")


def _voice_model(config: dict) -> Path:
    voice = config.get("voice", {}) if isinstance(config, dict) else {}
    name = voice.get("ttsVoice", _VOICE_DEFAULTS.get("ttsVoice", "en_GB-alan-medium"))
    return REPO / "bin" / "voices" / f"{name}.onnx"


def speak(text: str, config: dict) -> bool:
    """Synthesize `text` with piper and play it (osenv.play_audio). Returns True on success; False (with a
    stderr note) when piper/voice/audio-player is missing — a voice turn must degrade, not crash.
    piper reads text on stdin, writes a WAV, we play it."""
    if not (text or "").strip():
        return False
    piper = _piper_exe()
    voice = _voice_model(config)
    if not piper.exists():
        print("piper not found — run: bob setup-voice", file=sys.stderr)
        return False
    if not voice.exists():
        print(f"voice model not found at {voice} — run: bob setup-voice", file=sys.stderr)
        return False
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp_wav = f.name
    try:
        proc = subprocess.run(
            [str(piper), "--model", str(voice), "--output_file", tmp_wav, "--quiet"],
            input=text.encode("utf-8"),
            capture_output=True,
        )
        if proc.returncode != 0:
            print(f"piper failed (exit {proc.returncode}): {proc.stderr.decode(errors='replace')}",
                  file=sys.stderr)
            return False
        if not osenv.play_audio(tmp_wav):
            print(f"no audio player found — WAV written to {tmp_wav}", file=sys.stderr)
            return False
        return True
    finally:
        Path(tmp_wav).unlink(missing_ok=True)
