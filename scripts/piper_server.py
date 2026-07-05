"""
OpenAI-compatible TTS server wrapping piper CLI.
Exposes POST /v1/audio/speech so Open WebUI can use piper as its TTS engine.

Config (set by start-piper-server.ps1 via env vars):
  PIPER_EXE   — absolute path to bin/piper.exe
  PIPER_VOICE — absolute path to bin/voices/<voice>.onnx
  PIPER_PORT  — port to listen on (default 8083)

Note: the OpenAI 'voice' parameter (alloy, nova, echo, ...) is ignored.
Piper voices are ONNX files; the configured PIPER_VOICE is always used.
"""
import os
import asyncio
import tempfile
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

from bob_core import _port   # ONE-A Finding 4 — the TTS port default lives in config/defaults.json (NB1)

PIPER_EXE   = os.environ.get("PIPER_EXE", "")
PIPER_VOICE = os.environ.get("PIPER_VOICE", "")
PIPER_PORT  = int(os.environ.get("PIPER_PORT") or _port({}, "ttsPort"))

app = FastAPI(title="piper-tts-server")


class SpeechRequest(BaseModel):
    model: str = "tts-1"
    input: str
    voice: str = "alloy"   # accepted but ignored — piper voice is config-driven
    response_format: str = "wav"
    speed: float = 1.0


@app.get("/health")
def health():
    return {"status": "ok", "voice": Path(PIPER_VOICE).stem if PIPER_VOICE else "unconfigured"}


@app.get("/v1/models")
def list_models():
    return {
        "object": "list",
        "data": [{"id": "tts-1", "object": "model"}],
    }


@app.post("/v1/audio/speech")
async def speech(req: SpeechRequest):
    if not PIPER_EXE or not Path(PIPER_EXE).exists():
        raise HTTPException(500, f"piper.exe not found: {PIPER_EXE!r} — run: bob setup-voice")
    if not PIPER_VOICE or not Path(PIPER_VOICE).exists():
        raise HTTPException(500, f"Voice model not found: {PIPER_VOICE!r} — run: bob setup-voice")
    if not req.input.strip():
        raise HTTPException(400, "input text is empty")

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as _f:
        tmp_wav = _f.name
    try:
        # Piper reads text from stdin, writes audio to --output_file.
        proc = await asyncio.create_subprocess_exec(
            PIPER_EXE,
            "--model", PIPER_VOICE,
            "--output_file", tmp_wav,
            "--quiet",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate(input=req.input.encode("utf-8"))
        if proc.returncode != 0:
            raise HTTPException(500, f"piper failed (exit {proc.returncode}): {stderr.decode()}")

        wav_bytes = Path(tmp_wav).read_bytes()

        def _iter():
            yield wav_bytes

        return StreamingResponse(
            _iter(),
            media_type="audio/wav",
            headers={"Content-Disposition": "attachment; filename=speech.wav"},
        )
    finally:
        try:
            Path(tmp_wav).unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PIPER_PORT)
