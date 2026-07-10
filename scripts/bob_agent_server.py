#!/usr/bin/env python3
"""Bob agent HTTP server — exposes the agent tool loop as a REST + SSE endpoint.

Start: bob agent serve  (loopback :8084 by default; set agent.serveHost = '0.0.0.0' to expose)

Auth + identity: every endpoint (except /health) requires  Authorization: Bearer
<token>  where <token> is the litellm key or an agent.apiTokens entry. Each token maps to an
owner id; sessions are owner-scoped — a token only sees/modifies sessions its owner created
(any other id returns 404, indistinguishable from unknown).

Endpoints:
  POST /v1/agent/completions          {"goal","agency","role","session_id"} -> {"result","session_id","error"}
  POST /v1/agent/completions/stream   same body -> text/event-stream of {type,...} events
  POST /v1/sessions                   -> {"session_id"} (optional body {"token_budget"})
  GET  /v1/sessions/{sid}             -> session (history, budget, spend)
  DELETE /v1/sessions/{sid}           -> {"deleted": bool}
  GET  /health                        -> tool counts (no auth)

Wire into n8n:
  URL: http://host.docker.internal:8084/v1/agent/completions
  Header: Authorization: Bearer <litellm key>
  Body: {"goal": "{{ $json.goal }}"}
"""
import json
import sys
import threading
import uuid
from pathlib import Path
from typing import Optional

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "tools"))

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

app = FastAPI(title="Bob Agent API", version="1.1")


class AgentRequest(BaseModel):
    goal: str
    agency: str = "silent"
    role: Optional[str] = None
    session_id: Optional[str] = None  # continue a persisted conversation


class AgentResponse(BaseModel):
    result: Optional[str]
    session_id: Optional[str] = None
    error: Optional[str] = None


class SessionCreate(BaseModel):
    token_budget: int = 0  # 0 = unlimited; else reject once tokens_spent reaches it


class SteerRequest(BaseModel):
    run_id: str            # the id emitted in the stream's run_started event
    message: str           # injected into the running loop at its next step boundary


class TaskCreate(BaseModel):
    goal: str              # the durable, detached run to enqueue


# Live streaming runs, keyed by run id, so an owner can steer a run in flight (POST /v1/agent/steer).
# Registered when a stream starts, removed when it ends. Owner-scoped on lookup (no cross-owner steer).
_live_runs: dict = {}     # run_id -> (owner, CancelToken)
_live_runs_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Startup — build the tool registry + session store once, share across requests.
# ---------------------------------------------------------------------------

_config: dict = {}
_registry = None          # ToolRegistry | None
_sessions = None          # SessionStore | None
_token_owner: dict = {}   # bearer token -> owner id (litellm key + agent.apiTokens)
_token_meta: dict = {}    # bearer token -> {"scopes": [...]|None, "rate": int} for config tokens
_authstore = None         # AuthStore | None (DB-backed tokens: hot revoke + scopes + rate)
_rate_buckets: dict = {}  # owner -> [tokens_float, last_monotonic] token bucket


def _build_token_owner(config: dict) -> dict:
    """Map each accepted bearer token to an owner id. The litellm key maps to
    agent.defaultOwner; agent.apiTokens entries may be {token, owner} records or bare
    strings (legacy: token maps to itself as the owner)."""
    agent = config.get("agent", {})
    default_owner = agent.get("defaultOwner", "local")
    owners = {config.get("litellmKey", "sk-local"): default_owner}
    for entry in agent.get("apiTokens", []):
        if isinstance(entry, dict) and entry.get("token"):
            owners[entry["token"]] = entry.get("owner") or default_owner
        elif isinstance(entry, str) and entry:
            owners[entry] = entry  # legacy flat-string token -> token-as-owner
    return owners


def _build_token_meta(config: dict) -> dict:
    """Per-config-token scopes + rate, parallel to _build_token_owner. A dict apiTokens entry may
    carry optional `scopes` (tool globs / role:<name>) and `rate` (per-min); everything else defaults to
    unrestricted scopes + agent.defaultRatePerMin, so plain config tokens are unchanged (scopes None +
    rate 0 => no filtering, no rate limit)."""
    agent = config.get("agent", {})
    default_rate = int(agent.get("defaultRatePerMin", 0) or 0)
    meta = {config.get("litellmKey", "sk-local"): {"scopes": None, "rate": default_rate}}
    for entry in agent.get("apiTokens", []):
        if isinstance(entry, dict) and entry.get("token"):
            meta[entry["token"]] = {"scopes": entry.get("scopes"),
                                    "rate": int(entry.get("rate", default_rate) or 0)}
        elif isinstance(entry, str) and entry:
            meta[entry] = {"scopes": None, "rate": default_rate}
    return meta


@app.on_event("startup")
def _startup():
    global _config, _registry, _sessions, _token_owner, _token_meta, _authstore
    from bob_core import load_config
    from tool_registry import ToolRegistry
    from bob_session import SessionStore

    _config = load_config()
    agent = _config.get("agent", {})

    # Auth + identity: each accepted token maps to an owner id used to scope sessions.
    _token_owner = _build_token_owner(_config)
    _token_meta = _build_token_meta(_config)   # scopes + rate per config token

    # DB-backed token store (hot revoke / scopes / rate). Additive: off => config tokens only.
    if agent.get("authStore", False):
        from bob_authstore import AuthStore
        _authstore = AuthStore(REPO / agent.get("sessionDbPath", "data/sessions.db").replace("\\", "/"))

    disabled_raw = agent.get("disabledTools", [])
    disabled = set(disabled_raw) if isinstance(disabled_raw, list) else {
        t.strip() for t in disabled_raw.split(",") if t.strip()
    }
    _registry = ToolRegistry.build(_config, disabled)

    session_db = REPO / agent.get("sessionDbPath", "data/sessions.db").replace("\\", "/")
    _sessions = SessionStore(session_db, default_owner=agent.get("defaultOwner", "local"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _Identity:
    """The resolved caller: owner id + optional RBAC scopes + per-minute rate. `scopes=None`
    means unrestricted; a list restricts tools (globs) and roles (`role:<name>` entries)."""
    __slots__ = ("owner", "scopes", "rate")

    def __init__(self, owner: str, scopes, rate: int):
        self.owner = owner
        self.scopes = scopes
        self.rate = int(rate or 0)


def _authenticate(authorization: str) -> "_Identity":
    """Resolve a bearer token to an _Identity. Checks the static config map first,
    then — only when the auth store is enabled — hashes the bearer and looks it up (hot revocation: a
    revoked/unknown store token yields None). Raises 401 for an unknown token."""
    token = authorization[7:] if authorization.startswith("Bearer ") else ""
    owner = _token_owner.get(token)
    if owner is not None:
        meta = _token_meta.get(token) or {}
        return _Identity(owner, meta.get("scopes"), meta.get("rate", 0))
    if _authstore is not None and token:
        rec = _authstore.lookup(token)   # None if absent or revoked
        if rec is not None:
            return _Identity(rec["owner"], rec.get("scopes"), rec.get("rate_per_min", 0))
    raise HTTPException(status_code=401, detail="Unauthorized")


def _authed_owner(authorization: str) -> str:
    """Validate the bearer token and return its owner id. Raises 401 for an unknown token."""
    return _authenticate(authorization).owner


def _require_auth(authorization: str) -> None:
    """Back-compat shim: raise 401 unless the token is valid (identity discarded)."""
    _authenticate(authorization)


def _monotonic() -> float:
    import time
    return time.monotonic()


def _check_rate(identity: "_Identity") -> None:
    """Per-owner token-bucket rate limit. rate<=0 => unlimited (default, no-op). Raises 429 when
    the owner has spent its allowance; the bucket refills at `rate` tokens/min."""
    rate = identity.rate
    if rate <= 0:
        return
    now = _monotonic()
    tokens, last = _rate_buckets.get(identity.owner, (float(rate), now))
    tokens = min(float(rate), tokens + (now - last) * rate / 60.0)
    if tokens < 1.0:
        _rate_buckets[identity.owner] = (tokens, now)
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    _rate_buckets[identity.owner] = (tokens - 1.0, now)


def _expand_scopes(registry, globs) -> set:
    """Tool names in `registry` matching any glob in `globs` (fnmatch). The allow-set for filtered()."""
    import fnmatch
    names = [s.get("function", {}).get("name") for s in getattr(registry, "tool_schemas", [])]
    return {n for n in names if n and any(fnmatch.fnmatch(n, g) for g in globs)}


def _scoped_registry(identity: "_Identity"):
    """A registry VIEW restricted to the identity's tool scopes (reusing the filtered() seam), so
    an out-of-scope tool is neither offered to the model nor dispatchable. No scopes / only role-scopes
    => the base registry unchanged."""
    reg = _registry
    if reg is None or not identity.scopes:
        return reg
    tool_globs = [s for s in identity.scopes if not s.startswith("role:")]
    if not tool_globs:
        return reg
    return reg.filtered(allow=_expand_scopes(reg, tool_globs))


def _check_role_scope(identity: "_Identity", role) -> None:
    """If the identity carries `role:<name>` scopes, an explicitly-requested role must be among
    them (else 403). No role scopes, or no explicit role, => unrestricted (uses the default role)."""
    if not identity.scopes or not role:
        return
    allowed = [s[5:] for s in identity.scopes if s.startswith("role:")]
    if allowed and role not in allowed:
        raise HTTPException(status_code=403, detail=f"Role '{role}' not permitted for this token")


def _session_max_tokens() -> int:
    return int(_config.get("agent", {}).get("maxSessionTokens", 0))


def _load_session_or_404(session_id: Optional[str], owner: str):
    """Return (session dict|None, history list) for the owner. Raises 404 for an unknown id OR
    another owner's id (indistinguishable — no existence leak), 402 over budget."""
    if not session_id:
        return None, None
    session = _sessions.get_owned(session_id, owner) if _sessions else None
    if session is None:
        raise HTTPException(status_code=404, detail=f"Unknown session_id: {session_id}")
    if _sessions.over_budget(session_id):
        raise HTTPException(status_code=402, detail="Session token budget exhausted")
    return session, session["history"]


def _record_turn(session_id: Optional[str], goal: str, result: Optional[str]) -> None:
    if not session_id or _sessions is None:
        return
    from bob_loop import _estimate_tokens
    used = _estimate_tokens(goal) + _estimate_tokens(result or "")
    _sessions.append_turn(session_id, goal, result, tokens_used=used)


def _maybe_consolidate(session_id: str, owner: str) -> None:
    """Consolidate a session's turns into memory before it's deleted (the server's
    session-end seam). Gated on memory.enabled && memory.autoConsolidate; best-effort."""
    mem = (_config or {}).get("memory", {})
    if not (mem.get("enabled", False) and mem.get("autoConsolidate", True)) or _sessions is None:
        return
    session = _sessions.get_owned(session_id, owner)
    if not session or not session.get("history"):
        return
    try:
        from bob_core import consolidate_session
        consolidate_session(session["history"], config=_config, owner=owner, session_id=session_id)
    except Exception:
        pass


def _drain(gen) -> None:
    """Exhaust a generator so its finally-block (SIGINT restore / stream close) runs."""
    for _ in gen:
        pass


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    loaded = len(_registry._loaded_names) if _registry else 0
    errors = len(_registry.errors) if _registry else 0
    return {"status": "ok", "tools_loaded": loaded, "tools_failed": errors}


@app.post("/v1/sessions")
def create_session(req: SessionCreate = SessionCreate(), authorization: str = Header(default="")):
    owner = _authed_owner(authorization)
    if _sessions is None:
        raise HTTPException(status_code=503, detail="Server not yet initialized")
    budget = req.token_budget or _session_max_tokens()
    session = _sessions.create(token_budget=budget, owner_id=owner)
    return {"session_id": session["id"], "token_budget": session["token_budget"]}


@app.get("/v1/sessions/{sid}")
def get_session(sid: str, authorization: str = Header(default="")):
    owner = _authed_owner(authorization)
    session = _sessions.get_owned(sid, owner) if _sessions else None
    if session is None:  # unknown id OR another owner's id — same 404, no existence leak
        raise HTTPException(status_code=404, detail="Unknown session_id")
    return session


@app.delete("/v1/sessions/{sid}")
def delete_session(sid: str, authorization: str = Header(default="")):
    owner = _authed_owner(authorization)
    _maybe_consolidate(sid, owner)   # extract durable facts before dropping the turns
    return {"deleted": bool(_sessions and _sessions.delete_owned(sid, owner))}


@app.post("/v1/agent/completions", response_model=AgentResponse)
def agent_completions(req: AgentRequest, authorization: str = Header(default="")):
    from bob_loop import run_agent

    identity = _authenticate(authorization)
    if _registry is None:
        raise HTTPException(status_code=503, detail="Server not yet initialized")
    _check_rate(identity)              # per-owner rate limit (429)
    _check_role_scope(identity, req.role)   # RBAC role gate (403)
    owner = identity.owner

    _, history = _load_session_or_404(req.session_id, owner)
    rid = uuid.uuid4().hex[:8]  # request id threaded into the loop's log lines
    try:
        result, _ = run_agent(
            req.goal, _config, role=req.role, agency=req.agency,
            registry=_scoped_registry(identity), history=history, run_id=rid, owner=owner,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if result is None:  # don't record a bogus (answer-less) turn or charge tokens on 422
        raise HTTPException(
            status_code=422,
            detail="Agent reached max steps without producing a final answer",
        )
    _record_turn(req.session_id, req.goal, result)
    return AgentResponse(result=result, session_id=req.session_id)


@app.post("/v1/agent/completions/stream")
async def agent_completions_stream(
    req: AgentRequest, request: Request, authorization: str = Header(default="")
):
    """Server-Sent Events: stream tool_call / tool_result / token / final events as the
    agent works. Each SSE line is `data: {json}`; exactly one terminal event has type 'final' or
    'error'. If the client disconnects, the run is cancelled promptly and no turn is recorded
    unless a real final answer was produced. The blocking generator runs in a worker thread so the
    event loop can poll disconnect."""
    import anyio
    from bob_loop import run_agent_events, CancelToken

    identity = _authenticate(authorization)
    if _registry is None:
        raise HTTPException(status_code=503, detail="Server not yet initialized")
    _check_rate(identity)              # per-owner rate limit (429)
    _check_role_scope(identity, req.role)   # RBAC role gate (403)
    owner = identity.owner
    scoped = _scoped_registry(identity)     # tool-scope restricted view

    _, history = _load_session_or_404(req.session_id, owner)
    cancel = CancelToken()
    sentinel = object()
    rid = uuid.uuid4().hex[:8]  # request id threaded into the loop's log lines

    async def _sse():
        final_result = None
        got_final = False
        with _live_runs_lock:
            _live_runs[rid] = (owner, cancel)      # register so the owner can steer this run in flight
        gen = run_agent_events(
            req.goal, _config, role=req.role, agency=req.agency,
            registry=scoped, stream=True, history=history, cancel=cancel, run_id=rid, owner=owner,
        )
        try:
            # Tell the client its run id up front so it can POST /v1/agent/steer while the run is live.
            # Guarded by the disconnect check so nothing is emitted into an already-dead socket.
            if await request.is_disconnected():
                cancel.cancel()
                return
            yield f"data: {json.dumps({'type': 'run_started', 'run_id': rid})}\n\n"
            while True:
                if await request.is_disconnected():
                    cancel.cancel()
                    break  # client gone — don't emit a terminal event into a dead socket
                ev = await anyio.to_thread.run_sync(lambda: next(gen, sentinel))
                if ev is sentinel:
                    break
                if ev["type"] == "final":
                    got_final = True
                    final_result = ev.get("result")
                    if req.session_id:
                        ev["session_id"] = req.session_id
                yield f"data: {json.dumps(ev)}\n\n"
        except Exception as e:  # exactly one terminal error event; never a raw traceback
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        finally:
            with _live_runs_lock:
                _live_runs.pop(rid, None)          # run over -> no longer steerable
            cancel.cancel()
            try:  # drain in a worker thread so the generator's finally runs (SIGINT restore)
                await anyio.to_thread.run_sync(lambda: _drain(gen))
            except Exception:
                pass
            if got_final and final_result is not None:  # no bogus turn on disconnect/error/max_steps
                _record_turn(req.session_id, req.goal, final_result)

    return StreamingResponse(_sse(), media_type="text/event-stream")


@app.post("/v1/agent/steer")
def agent_steer(req: SteerRequest, authorization: str = Header(default="")):
    """Inject a user message into a live streaming run WITHOUT cancelling it. The message is queued and
    picked up at the run's next step boundary. Owner-scoped: an unknown id or another owner's run both
    return 404 (no cross-owner existence leak)."""
    identity = _authenticate(authorization)
    with _live_runs_lock:
        entry = _live_runs.get(req.run_id)
    if entry is None or entry[0] != identity.owner:
        raise HTTPException(status_code=404, detail="no live run with that id")
    entry[1].steer(req.message)
    return {"status": "queued", "run_id": req.run_id}


# --- detached tasks -------------------------------------------------------------------------------
# Unlike the streaming endpoint (which cancels its run when the client disconnects), a task launches a
# detached worker and returns immediately: the run survives the client leaving AND a server restart.
# Owner-scoped throughout; an unknown id or another owner's task both 404 (no existence leak).

def _task_store():
    import bob_checkpoint
    agent = _config.get("agent", {})
    return bob_checkpoint.CheckpointStore(
        db_path=agent.get("checkpointDbPath") or None, default_owner=agent.get("defaultOwner", "local"))


def _task_summary(row: dict) -> dict:
    return {"run_id": row["run_id"], "status": row["status"], "step": row["step"],
            "goal": row["goal"], "result": row.get("result")}


@app.post("/v1/tasks")
def create_task(req: TaskCreate, authorization: str = Header(default="")):
    from bob import cli
    identity = _authenticate(authorization)
    _check_rate(identity)
    owner = identity.owner
    store = _task_store()
    rid = uuid.uuid4().hex[:8]
    store.save_run(rid, owner, "queued", req.goal, [], step=0)
    cli._launch_task(_config, rid, owner, req.goal, resume=False)
    return {"run_id": rid, "status": "queued"}


@app.get("/v1/tasks")
def list_tasks(authorization: str = Header(default="")):
    owner = _authed_owner(authorization)
    return {"tasks": [_task_summary(r) for r in _task_store().list_runs(owner)]}


@app.get("/v1/tasks/{run_id}")
def get_task(run_id: str, authorization: str = Header(default="")):
    owner = _authed_owner(authorization)
    row = _task_store().load_run(run_id, owner)
    if row is None:  # unknown id OR another owner's id — same 404, no existence leak
        raise HTTPException(status_code=404, detail="Unknown run_id")
    return _task_summary(row)


@app.get("/v1/tasks/{run_id}/logs")
def get_task_logs(run_id: str, authorization: str = Header(default="")):
    owner = _authed_owner(authorization)
    row = _task_store().load_run(run_id, owner)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown run_id")
    path = row.get("log_path")
    from pathlib import Path
    if not path or not Path(path).exists():
        return {"run_id": run_id, "log": ""}
    return {"run_id": run_id, "log": Path(path).read_text(encoding="utf-8", errors="replace")}


@app.post("/v1/tasks/{run_id}/cancel")
def cancel_task(run_id: str, authorization: str = Header(default="")):
    import osenv
    owner = _authed_owner(authorization)
    store = _task_store()
    row = store.load_run(run_id, owner)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown run_id")
    pid = row.get("pid")
    if pid and osenv.pid_alive(pid):
        osenv.stop_process_tree(pid)
    store.set_status(run_id, owner, "cancelled")
    return {"run_id": run_id, "status": "cancelled"}


@app.post("/v1/tasks/{run_id}/resume")
def resume_task(run_id: str, authorization: str = Header(default="")):
    from bob import cli
    identity = _authenticate(authorization)
    _check_rate(identity)
    owner = identity.owner
    store = _task_store()
    row = store.load_run(run_id, owner)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown run_id")
    cli._launch_task(_config, run_id, owner, row["goal"], resume=True)
    return {"run_id": run_id, "status": "resuming"}


if __name__ == "__main__":
    import uvicorn
    from bob_core import _port, load_config

    _agent = load_config().get("agent", {})
    # Default to loopback. Set agent.serveHost = '0.0.0.0' in config/user.json to expose on the LAN
    # (also harden web_fetch — the SSRF guard — before doing so).
    uvicorn.run(
        app,
        host=_agent.get("serveHost", "127.0.0.1"),
        port=_port(_agent, "agentPort"),
    )
