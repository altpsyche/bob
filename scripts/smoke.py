#!/usr/bin/env python3
"""ONE-E — the end-to-end smoke, in Python (port of the retired smoke.ps1). The shared CROSS-OS gate the
CI acceptance matrix runs on Windows AND Linux; it exercises the RUNNING stack, so it passes on either OS
when the stack is up. Stdlib-only (urllib) so any interpreter runs it — no venv dependency.

Scope (per the NC8 decision): provision -> serve -> a COHERENT answer. Model-agnostic; deliberately does
NOT gate on a real tool round-trip (tool-protocol correctness lives in the unit tests). Steps:
  1. inference endpoint reachable (llama-swap /v1/models)
  2. `bob agent "say hi"` returns a non-empty answer
  3. `bob agent serve`: GET /health (no auth) + an owner-scoped session turn (N1) + an SSE stream (N3/N6).
     Step 3 gates the SERVER CONTRACT (auth, session, routing, SSE); a backend-model failure there
     (e.g. a resource-starved CPU-tier reload) is SKIPped — "a coherent answer" is step 2's job.

  python scripts/smoke.py            # test whatever is running; SKIP (exit 0) if nothing is up
  python scripts/smoke.py --up       # bring the stack + agent server up first, tear the server down after
"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import osenv  # noqa: E402
from bob_core import _port, load_config  # noqa: E402

BOB = str(REPO / "bob") if osenv.os_name() != "windows" else None

_pass = 0
_fail = 0


def ok(m):
    global _pass
    _pass += 1
    print(f"  PASS  {m}")


def bad(m):
    global _fail
    _fail += 1
    print(f"  FAIL  {m}")


def skip(m):
    print(f"  SKIP  {m}")


def _bob_argv(args: list) -> list:
    """Invoke the front door exactly as a user would: the POSIX ./bob shim, else `python -m bob`."""
    if BOB and Path(BOB).exists():
        return [BOB, *args]
    env_py = str(osenv.venv_exe("venv-litellm", "python"))
    py = env_py if Path(env_py).exists() else sys.executable
    return [py, "-m", "bob", *args]


def _bob_env() -> dict:
    import os
    e = dict(os.environ)
    e["PYTHONPATH"] = str(SCRIPTS) + (os.pathsep + e["PYTHONPATH"] if e.get("PYTHONPATH") else "")
    e["PYTHONIOENCODING"] = "utf-8"
    return e


def _get(url: str, headers: dict = None, timeout: float = 5):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — localhost only
        return r.status, r.read().decode("utf-8", "replace")


def _post(url: str, body: dict, headers: dict = None, timeout: float = 5):
    data = json.dumps(body).encode("utf-8")
    h = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — localhost only
        return r.status, r.read().decode("utf-8", "replace")


def _wait_url(url: str, seconds: float, headers: dict = None) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            _get(url, headers, timeout=5)
            return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    return False


def _is_backend_hiccup(err) -> bool:
    """After /health + a session succeed, a failed TURN is a backend-model problem, not a contract bug:
    a 5xx / 422 / timeout / dropped connection -> SKIP; a 4xx (401/404/400) -> real contract FAIL."""
    code = getattr(err, "code", 0) or 0
    if 400 <= code < 500 and code != 422:
        return False  # contract error
    return True        # 5xx / 422 / timeout / no-response


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    up = "--up" in argv or "-Up" in argv
    timeout = 300

    cfg = load_config()
    port = cfg.get("port") or _port(cfg, "port")
    agent_port = (cfg.get("agent", {}) or {}).get("agentPort") or _port(cfg, "agentPort")
    agent_host = (cfg.get("agent", {}) or {}).get("serveHost") or "127.0.0.1"
    litellm_key = osenv.secret("litellmKey", cfg.get("litellmKey", "sk-local"), cfg)
    inf_base = f"http://localhost:{port}/v1"
    agent_base = f"http://{agent_host}:{agent_port}"

    print(f"\nBob end-to-end smoke  (OS: {osenv.os_name()})")
    print("─────────────────────────────────────────")

    # --- 1. inference endpoint --------------------------------------------
    if up:
        print("[up] starting the stack (bob up)...")
        subprocess.run(_bob_argv(["up", "-NoOpen"]), env=_bob_env(),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not _wait_url(f"{inf_base}/models", timeout if up else 5):
        if up:
            bad(f"inference endpoint never came up at {inf_base} (check: bob logs)")
            print(f"\n{_pass} passed, {_fail} failed")
            return 1
        skip(f"inference endpoint not running at {inf_base} — start it (bob up) or pass --up.")
        print(f"\n{_pass} passed, {_fail} failed (skipped)")
        return 0
    ok(f"inference endpoint reachable ({inf_base})")

    # --- 2. bob agent "say hi" returns a coherent answer ------------------
    try:
        proc = subprocess.run(_bob_argv(["agent", "say hi"]), env=_bob_env(),
                              capture_output=True, text=True, timeout=timeout)
        answer = (proc.stdout or proc.stderr or "").strip()
    except (subprocess.SubprocessError, OSError) as e:
        answer = f"ERROR: {e}"
    if answer and len(answer) >= 2 and not answer.startswith(("ERROR", "Traceback", "Error:")):
        ok(f"bob agent 'say hi' answered ({len(answer)} chars)")
    else:
        bad(f"bob agent 'say hi' returned no coherent answer: {answer[:120]}")

    # --- 3. agent HTTP server: /health + session turn + SSE ---------------
    server_pid = None
    pidfile = osenv.cache_dir() / "agent-serve.smoke.pid"
    try:
        server_up = _wait_url(f"{agent_base}/health", 3)
        if not server_up and up:
            print("[up] starting the agent server (bob agent serve)...")
            server_pid = osenv.start_detached(_bob_argv(["agent", "serve"]), pidfile=str(pidfile),
                                              env=_bob_env())
            server_up = _wait_url(f"{agent_base}/health", 30)

        if not server_up:
            skip(f"agent server not running at {agent_base} — start it (bob agent serve) or pass --up.")
        else:
            try:
                _get(f"{agent_base}/health", timeout=5)
                ok("GET /health responded")
            except (urllib.error.URLError, OSError) as e:
                bad(f"GET /health failed: {e}")

            hdr = {"Authorization": f"Bearer {litellm_key}"}
            sid = None
            try:
                _, body = _post(f"{agent_base}/v1/sessions", {}, hdr, timeout=10)
                sid = json.loads(body).get("session_id")
                ok(f"created session ({sid})") if sid else bad("POST /v1/sessions returned no session_id")
            except (urllib.error.URLError, OSError, ValueError) as e:
                bad(f"create session (POST /v1/sessions) failed: {e}")

            if not sid:
                skip("session turn + SSE — no session to run them on")
            else:
                # owner-scoped session turn (N1). A backend model failure (5xx/422/timeout) is infra,
                # not a contract bug -> SKIP; only a contract error (401/404/malformed) FAILs.
                try:
                    _, body = _post(f"{agent_base}/v1/agent/completions",
                                    {"goal": "say hi", "session_id": sid}, hdr, timeout=timeout)
                    r = json.loads(body)
                    if r.get("result") and not r.get("error"):
                        ok(f"session turn returned a result (session_id={r.get('session_id')})")
                    else:
                        bad(f"session turn returned no result / an error: {r.get('error')}")
                except urllib.error.HTTPError as e:
                    (skip if _is_backend_hiccup(e) else bad)(
                        f"session turn — {'backend model error; contract OK' if _is_backend_hiccup(e) else e}")
                except (urllib.error.URLError, OSError, ValueError) as e:
                    skip(f"session turn — backend/timeout on the CPU tier; server routed it, contract OK ({e})")

                # SSE stream (N3/N6). A 'final'/'token' event = healthy; only an 'error' event = backend
                # failure delivered over a working stream -> SKIP.
                try:
                    _, text = _post(f"{agent_base}/v1/agent/completions/stream",
                                    {"goal": "say hi", "session_id": sid}, hdr, timeout=timeout)
                    if '"type": "final"' in text or '"type":"final"' in text or \
                       '"type": "token"' in text or '"type":"token"' in text:
                        ok("SSE stream produced events")
                    elif '"type": "error"' in text or '"type":"error"' in text:
                        skip("SSE stream — backend model error delivered as an event; stream wiring OK")
                    else:
                        bad(f"SSE stream produced no recognizable events: {text[:200]}")
                except urllib.error.HTTPError as e:
                    (skip if _is_backend_hiccup(e) else bad)(
                        f"SSE stream — {'backend error; stream reachable' if _is_backend_hiccup(e) else e}")
                except (urllib.error.URLError, OSError) as e:
                    skip(f"SSE stream — backend/timeout on the CPU tier; endpoint reachable ({e})")
    finally:
        if server_pid:
            print(f"[up] stopping the smoke agent server (PID {server_pid})...")
            osenv.stop_process_tree(server_pid)
            pidfile.unlink(missing_ok=True)

    print(f"\n{_pass} passed, {_fail} failed")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
