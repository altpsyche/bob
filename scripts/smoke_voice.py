"""Gating voice-STT smoke: prove the faster-whisper STT server serves the whisper.cpp /inference contract
on a fresh CPU install (fresh-install voice must work by default, not just be unit-wired).

Fetches the tiny CT2 model (~75 MB), starts the STT server for the configured engine via the stack seam
(so it runs under venv-litellm exactly as in production), waits for /health, then POSTs a synthetic WAV to
both routes: /inference (whisper.cpp contract) and /v1/audio/transcriptions (OpenAI contract). Each must
answer HTTP 200 with a string 'text'. A silent WAV transcribes to nothing, so the proof that the real
transcription path ran is /health reporting the CT2 model LOADED afterwards (the server loads it on the
first request). A server without the OpenAI route (404) is noted, not failed. Exit 0 on pass, 1 on fail.

Stdlib-only HTTP (urllib), so it runs on the CI interpreter; the server itself runs under venv-litellm.
Reuses the provision/stack seams rather than re-implementing the fetch, WAV, and launch."""
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "tools"))

import bob_core          # noqa: E402
from bob_core import _port   # noqa: E402
import provision         # noqa: E402
import stack             # noqa: E402

_CI_MODEL = "tiny"       # smallest CT2 model — keep the CI download light; quality isn't under test here
_READY_SECONDS = 120     # port-open budget: the server may preload the model before it listens


def _log(msg: str) -> None:
    print(f"[voice-smoke] {msg}", file=sys.stderr)


def _wait_ready(base: str, seconds: float) -> dict:
    """Poll GET /health until it answers 200; returns its JSON body. Raises TimeoutError."""
    end = time.monotonic() + seconds
    last = None
    while time.monotonic() < end:
        try:
            with urllib.request.urlopen(f"{base}/health", timeout=5) as r:  # noqa: S310 — localhost only
                if r.status == 200:
                    return json.loads(r.read().decode("utf-8", "replace") or "{}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = e
        time.sleep(0.5)
    raise TimeoutError(f"STT server not ready at {base}/health after {seconds:.0f}s ({last})")


def _post_wav(url: str, fields: dict, wav: Path) -> tuple:
    """(status, parsed JSON body) for a multipart WAV upload. HTTP errors return their status, not raise."""
    body, ctype = provision._multipart(fields, wav.name, wav.read_bytes())
    req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310 — localhost only
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]


def _has_text(data) -> bool:
    return isinstance(data, dict) and isinstance(data.get("text"), str)


def main() -> int:
    config = bob_core.load_config()
    voice = config.setdefault("voice", {})
    voice["sttModel"] = _CI_MODEL                          # override the profile default for CI speed
    provision.configure(config)
    stack.configure(config)

    out: list = []
    # Retry the model download: the CT2 fetch reaches out to Hugging Face, which occasionally resets the
    # connection. A transient network hiccup must not red this gating job.
    last_err = None
    for attempt in range(1, 4):
        try:
            provision._fetch_ct2_model(_CI_MODEL, force=False, out=out)
            last_err = None
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            _log(f"CT2 fetch attempt {attempt}/3 failed: {e}")
            time.sleep(5 * attempt)
    if last_err is not None:
        _log(f"FAIL: CT2 model fetch failed after retries: {last_err}")
        return 1
    for line in out:
        _log(line)

    stt_port = _port(config, "sttPort")
    base = f"http://localhost:{stt_port}"
    wav = Path(tempfile.gettempdir()) / f"bob-stt-smoke-{os.getpid()}.wav"
    provision._silent_wav(wav)
    try:
        _log(stack.service_control(config, "whisper", "start"))
        _wait_ready(base, _READY_SECONDS)

        code, data = _post_wav(f"{base}/inference", {"temperature": "0.0", "response_format": "json"}, wav)
        if code != 200 or not _has_text(data):
            _log(f"FAIL: /inference status={code} body={data!r}")
            return 1
        _log(f"/inference 200; transcript of silence: {data['text']!r}")

        code, data = _post_wav(f"{base}/v1/audio/transcriptions",
                               {"model": _CI_MODEL, "response_format": "json"}, wav)
        if code == 404:
            _log("NOTE: /v1/audio/transcriptions not served by this STT server (404); /inference only")
        elif code != 200 or not _has_text(data):
            _log(f"FAIL: /v1/audio/transcriptions status={code} body={data!r}")
            return 1
        else:
            _log(f"/v1/audio/transcriptions 200; transcript of silence: {data['text']!r}")

        health = _wait_ready(base, 10)
        model = str(health.get("model", ""))
        if not model or model == "(unloaded)":
            _log(f"FAIL: the transcription requests never loaded the model (/health: {health!r})")
            return 1
        _log(f"PASS: model loaded and transcribing ({model})")
        return 0
    except Exception as e:  # noqa: BLE001
        _log(f"FAIL: {e}")
        return 1
    finally:
        wav.unlink(missing_ok=True)
        try:
            stack.service_control(config, "whisper", "stop")
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
