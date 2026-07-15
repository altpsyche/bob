"""Gating voice-STT smoke: prove the faster-whisper STT server serves the whisper.cpp /inference contract
on a fresh CPU install (fresh-install voice must work by default, not just be unit-wired).

Fetches the tiny CT2 model (~75 MB), starts the STT server for the configured engine via the stack seam
(so it runs under venv-litellm exactly as in production), POSTs a synthetic silent WAV, and requires an
HTTP 200 with a JSON body carrying a 'text' key. Exit 0 on pass, 1 on fail.

Stdlib-only HTTP (urllib), so it runs on the CI interpreter; the server itself runs under venv-litellm.
Reuses the provision/stack seams rather than re-implementing the fetch, WAV, and launch."""
import json
import os
import sys
import tempfile
import time
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


def main() -> int:
    config = bob_core.load_config()
    voice = config.setdefault("voice", {})
    voice["sttModel"] = _CI_MODEL                          # override the profile default for CI speed
    voice.setdefault("sttEngine", "faster-whisper")
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
            print(f"[voice-smoke] CT2 fetch attempt {attempt}/3 failed: {e}", file=sys.stderr)
            time.sleep(5 * attempt)
    if last_err is not None:
        print(f"[voice-smoke] FAIL: CT2 model fetch failed after retries: {last_err}", file=sys.stderr)
        return 1
    for line in out:
        print(f"[voice-smoke] {line}", file=sys.stderr)

    stt_port = _port(config, "sttPort")
    wav = Path(tempfile.gettempdir()) / f"bob-stt-smoke-{os.getpid()}.wav"
    provision._silent_wav(wav)
    try:
        print(f"[voice-smoke] {stack.service_control(config, 'whisper', 'start')}", file=sys.stderr)
        time.sleep(2)   # model is loaded before the port opens; a small margin for the first request
        body, ctype = provision._multipart({"temperature": "0.0", "response_format": "json"},
                                           wav.name, wav.read_bytes())
        req = urllib.request.Request(f"http://localhost:{stt_port}/inference", data=body,
                                     headers={"Content-Type": ctype}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 — localhost only
            code = r.status
            data = json.loads(r.read().decode("utf-8", "replace"))
        if code != 200 or not isinstance(data, dict) or "text" not in data:
            print(f"[voice-smoke] FAIL: status={code} body={data!r}", file=sys.stderr)
            return 1
        print(f"[voice-smoke] PASS: /inference 200; transcript of silence: {data.get('text', '')!r}",
              file=sys.stderr)
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"[voice-smoke] FAIL: {e}", file=sys.stderr)
        return 1
    finally:
        wav.unlink(missing_ok=True)
        try:
            stack.service_control(config, "whisper", "stop")
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
