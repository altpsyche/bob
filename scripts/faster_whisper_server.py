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
  STT_IDLE_SECONDS — free the model after this long with no transcription (0 = never). The GPU model
                     holds ~1 GB of VRAM, which on a 16 GB card is a whole quantization step of the
                     chat model, so it does not squat while nobody is talking.
  STT_PRELOAD      — "1" to load the model at startup instead of on the first request.
"""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException

from bob_core import _port   # the STT port default lives in config/defaults.json

STT_PORT = int(os.environ.get("STT_PORT") or _port({}, "sttPort"))
STT_MODEL = os.environ.get("STT_MODEL", "small")
STT_MODEL_DIR = os.environ.get("STT_MODEL_DIR", "")
STT_COMPUTE_TYPE = os.environ.get("STT_COMPUTE_TYPE", "auto")
STT_DEVICE = os.environ.get("STT_DEVICE", "auto")
STT_IDLE_SECONDS = int(os.environ.get("STT_IDLE_SECONDS") or 900)
STT_PRELOAD = os.environ.get("STT_PRELOAD", "") == "1"

app = FastAPI(title="faster-whisper-stt-server")

# Loaded on first use (or at startup with STT_PRELOAD) and kept module-level so every request reuses
# the resident model. _lock serializes load/unload against in-flight transcriptions; _last_used drives
# the idle reaper.
_model = None
_model_ref = ""
_lock = threading.RLock()
_last_used = 0.0


def _preload_cuda_libs() -> None:
    """Make the CT2 GPU path find its CUDA runtime. faster-whisper's CTranslate2 needs cuBLAS/cuDNN for
    the CUDA major it was built against (12); when the `nvidia-*-cu12` wheels are installed in this venv
    their libraries live under site-packages/nvidia/ where CT2's own dlopen won't look. Preload them by
    absolute path (RTLD_GLOBAL on POSIX; add_dll_directory on Windows) so CT2 resolves them at encode
    time, without depending on LD_LIBRARY_PATH being set at exec. A no-op when the wheels aren't installed,
    so _load_model then falls back to CPU int8. cuBLAS is loaded before cuDNN (cuDNN depends on it)."""
    import ctypes
    import sysconfig

    purelib = sysconfig.get_paths().get("purelib")
    if not purelib:
        return
    nvidia = Path(purelib) / "nvidia"
    if not nvidia.is_dir():
        return
    win = os.name == "nt"
    for sub in ("cublas", "cudnn"):
        libdir = nvidia / sub / ("bin" if win else "lib")
        if not libdir.is_dir():
            continue
        if win:
            try:
                os.add_dll_directory(str(libdir))
            except OSError:
                pass
        else:
            for lib in sorted(libdir.glob("*.so*")):
                try:
                    ctypes.CDLL(str(lib), mode=ctypes.RTLD_GLOBAL)
                except OSError:
                    pass


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


def _warmup(model) -> None:
    """Force an encode so a missing/mismatched CUDA runtime (CT2 needs cuBLAS/cuDNN for the exact CUDA
    major it was built against) surfaces here, at load, rather than as a 500 on the first turn.
    Transcribing a short silent buffer runs detect_language -> encode, which is where a GPU library
    mismatch throws."""
    import numpy as np
    segments, _info = model.transcribe(np.zeros(16000, dtype="float32"), vad_filter=False)
    list(segments)   # segments is a generator; consume it to actually run the compute


def _load_model():
    """Load the CT2 model from a pinned local dir when present, else the size name (auto-download). Tries
    the resolved device but, if the GPU path can't actually run (the CT2 wheel's cuBLAS/cuDNN major must
    match the installed CUDA, which it may not), falls back to CPU int8 so STT always works by default."""
    _preload_cuda_libs()   # let CT2 find bundled cu12 cuBLAS/cuDNN before it imports/encodes
    from faster_whisper import WhisperModel

    global _model, _model_ref
    ref = STT_MODEL_DIR if (STT_MODEL_DIR and Path(STT_MODEL_DIR).exists()) else STT_MODEL
    device, compute = _resolve_device_and_compute()
    try:
        model = WhisperModel(ref, device=device, compute_type=compute)
        _warmup(model)
    except Exception as e:  # noqa: BLE001 — any GPU/library failure: degrade to CPU, never fail to load
        if device == "cpu":
            raise
        print(f"faster-whisper: {device} path unavailable ({e}); falling back to cpu/int8", file=sys.stderr)
        device, compute = "cpu", "int8"
        model = WhisperModel(ref, device=device, compute_type=compute)
        _warmup(model)
    _model = model
    _model_ref = f"{ref} ({device}/{compute})"
    print(f"faster-whisper: loaded {_model_ref}", file=sys.stderr)


def _ensure_model():
    """The resident model, loading it if this is the first request or the idle reaper freed it."""
    global _last_used
    with _lock:
        if _model is None:
            _load_model()
        _last_used = time.monotonic()
        return _model


def _idle_reaper():
    """Drop the model (and its VRAM) after STT_IDLE_SECONDS without a transcription. The port stays
    open and the next request reloads, so the lifecycle and every client contract are unchanged."""
    global _model, _model_ref
    while True:
        time.sleep(min(60, max(5, STT_IDLE_SECONDS // 4)))
        with _lock:
            if _model is None or time.monotonic() - _last_used < STT_IDLE_SECONDS:
                continue
            _model, _model_ref = None, ""
        import gc
        gc.collect()
        print(f"faster-whisper: idle for {STT_IDLE_SECONDS}s — model unloaded, VRAM released",
              file=sys.stderr)


@app.get("/health")
def health():
    # "ok" the moment the port answers: the model is loaded on demand, so a client must not wait for it.
    return {"status": "ok", "model": _model_ref or "(unloaded)"}


@app.post("/inference")
async def inference(file: UploadFile = File(...),
                    temperature: str = Form("0.0"),
                    response_format: str = Form("json")):
    """whisper.cpp-compatible endpoint: accept a WAV upload, return {"text": transcript}."""
    model = _ensure_model()
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
        segments, _info = model.transcribe(tmp, temperature=temp, vad_filter=True)
        text = "".join(seg.text for seg in segments).strip()
        return {"text": text}
    except Exception as e:   # never leak a stack trace to the HTTP client; the loop wraps 5xx
        raise HTTPException(500, f"transcription failed: {e}")
    finally:
        Path(tmp).unlink(missing_ok=True)


if __name__ == "__main__":
    import uvicorn
    if STT_PRELOAD:
        _load_model()   # warm before the port opens, so a port probe == ready
    if STT_IDLE_SECONDS > 0:
        threading.Thread(target=_idle_reaper, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=STT_PORT)
