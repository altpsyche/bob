"""Speech-to-text server wrapping faster-whisper (Whisper on CTranslate2).

Drop-in replacement for the whisper.cpp `whisper-server`: exposes the SAME HTTP contract
(`POST /inference`, multipart `file`, returns `{"text": ...}`) on `sttPort`, so bob_voice's
transcribe client, the /voice loop, and the stack lifecycle need no change to talk to it.
The CT2 model is loaded ONCE at startup (warm), with built-in Silero VAD for endpointing.

Config (set via env vars by scripts/tools/stack.py):
  STT_PORT         — port to listen on (default: sttPort from config/defaults.json)
  STT_MODEL        — model size/name for auto-download (default "small")
  STT_MODEL_DIR    — local CT2 model directory; used verbatim when it exists (offline / pinned)
  STT_COMPUTE_TYPE — "auto" (float16 on GPU, int8 on CPU), or a CT2 compute type
  STT_DEVICE       — "auto" | "cuda" | "cpu"
"""
import os
import sys
import tempfile
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException

from bob_core import _port   # the STT port default lives in config/defaults.json

STT_PORT = int(os.environ.get("STT_PORT") or _port({}, "sttPort"))
STT_MODEL = os.environ.get("STT_MODEL", "small")
STT_MODEL_DIR = os.environ.get("STT_MODEL_DIR", "")
STT_COMPUTE_TYPE = os.environ.get("STT_COMPUTE_TYPE", "auto")
STT_DEVICE = os.environ.get("STT_DEVICE", "auto")

app = FastAPI(title="faster-whisper-stt-server")

# Loaded once at startup (warm). Kept module-level so every request reuses the resident model.
_model = None
_model_ref = ""


def _resolve_device_and_compute() -> tuple:
    """Pick device + CT2 compute type. 'auto' -> CUDA/float16 when a GPU is visible, else CPU/int8."""
    device = STT_DEVICE
    has_cuda = False
    try:
        import ctranslate2
        has_cuda = ctranslate2.get_cuda_device_count() > 0
    except Exception:
        has_cuda = False
    if device == "auto":
        device = "cuda" if has_cuda else "cpu"
    compute = STT_COMPUTE_TYPE
    if compute == "auto":
        compute = "float16" if device == "cuda" else "int8"
    return device, compute


def _load_model():
    """Build the WhisperModel from a pinned local dir when present, else the size name (auto-download)."""
    from faster_whisper import WhisperModel

    global _model, _model_ref
    ref = STT_MODEL_DIR if (STT_MODEL_DIR and Path(STT_MODEL_DIR).exists()) else STT_MODEL
    device, compute = _resolve_device_and_compute()
    _model = WhisperModel(ref, device=device, compute_type=compute)
    _model_ref = f"{ref} ({device}/{compute})"
    print(f"faster-whisper: loaded {_model_ref}", file=sys.stderr)


@app.get("/health")
def health():
    return {"status": "ok" if _model is not None else "loading", "model": _model_ref}


@app.post("/inference")
async def inference(file: UploadFile = File(...),
                    temperature: str = Form("0.0"),
                    response_format: str = Form("json")):
    """whisper.cpp-compatible endpoint: accept a WAV upload, return {"text": transcript}."""
    if _model is None:
        raise HTTPException(503, "STT model not loaded yet")
    data = await file.read()
    if not data:
        return {"text": ""}
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(data)
        tmp = f.name
    try:
        try:
            temp = float(temperature)
        except (TypeError, ValueError):
            temp = 0.0
        segments, _info = _model.transcribe(tmp, temperature=temp, vad_filter=True)
        text = "".join(seg.text for seg in segments).strip()
        return {"text": text}
    except Exception as e:   # never leak a stack trace to the HTTP client; the loop wraps 5xx
        raise HTTPException(500, f"transcription failed: {e}")
    finally:
        Path(tmp).unlink(missing_ok=True)


if __name__ == "__main__":
    import uvicorn
    _load_model()   # warm the model before the port opens, so a port probe == ready
    uvicorn.run(app, host="127.0.0.1", port=STT_PORT)
