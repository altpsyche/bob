#!/usr/bin/env python3
"""Bob agent HTTP server — exposes the agent tool loop as a REST + SSE endpoint.

Start: bob agent serve  (loopback :8084 by default; set agent.serveHost = '0.0.0.0' to expose)

Auth + identity (M5/M12/N1): every endpoint (except /health) requires  Authorization: Bearer
<token>  where <token> is the litellm key or an agent.apiTokens entry. Each token maps to an
owner id; sessions are owner-scoped — a token only sees/modifies sessions its owner created
(any other id returns 404, indistinguishable from unknown).

Endpoints:
  POST /v1/agent/completions          {"goal","agency","role","session_id"} -> {"result","session_id","error"}
  POST /v1/agent/completions/stream   same body -> text/event-stream of {type,...} events (M15)
  POST /v1/sessions                   -> {"session_id"} (M12; optional body {"token_budget"})
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
    session_id: Optional[str] = None  # M12 — continue a persisted conversation


class AgentResponse(BaseModel):
    result: Optional[str]
    session_id: Optional[str] = None
    error: Optional[str] = None


class SessionCreate(BaseModel):
    token_budget: int = 0  # 0 = unlimited; else reject once tokens_spent reaches it


# ---------------------------------------------------------------------------
# Startup — build the tool registry + session store once, share across requests.
# ---------------------------------------------------------------------------

_config: dict = {}
_registry = None          # ToolRegistry | None
_sessions = None          # SessionStore | None
_token_owner: dict = {}   # N1 — bearer token -> owner id (litellm key + agent.apiTokens)


def _build_token_owner(config: dict) -> dict:
    """Map each accepted bearer token to an owner id (N1). The litellm key maps to
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


@app.on_event("startup")
def _startup():
    global _config, _registry, _sessions, _token_owner
    from bob_core import load_config
    from tool_registry import ToolRegistry
    from bob_session import SessionStore

    _config = load_config()
    agent = _config.get("agent", {})

    # Auth + identity (N1): each accepted token maps to an owner id used to scope sessions.
    _token_owner = _build_token_owner(_config)

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

def _authed_owner(authorization: str) -> str:
    """Validate the bearer token and return its owner id (N1). Raises 401 for an unknown token."""
    token = authorization[7:] if authorization.startswith("Bearer ") else ""
    owner = _token_owner.get(token)
    if owner is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return owner


def _require_auth(authorization: str) -> None:
    """Back-compat shim: raise 401 unless the token is valid (identity discarded)."""
    _authed_owner(authorization)


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
    """MEM-4 — consolidate a session's turns into memory before it's deleted (the server's
    session-end seam). Gated on memory.enabled && memory.autoConsolidate; best-effort."""
    mem = (_config or {}).get("memory", {})
    if not (mem.get("enabled", False) and mem.get("autoConsolidate", True)) or _sessions is None:
        return
    session = _sessions.get_owned(session_id, owner)
    if not session or not session.get("history"):
        return
    try:
        from bob_core import consolidate_session
        consolidate_session(session["history"], config=_config, owner=owner)
    except Exception:
        pass


def _drain(gen) -> None:
    """Exhaust a generator so its finally-block (SIGINT restore / stream close) runs (N3)."""
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
    _maybe_consolidate(sid, owner)   # MEM-4 — extract durable facts before dropping the turns
    return {"deleted": bool(_sessions and _sessions.delete_owned(sid, owner))}


@app.post("/v1/agent/completions", response_model=AgentResponse)
def agent_completions(req: AgentRequest, authorization: str = Header(default="")):
    from bob_loop import run_agent

    owner = _authed_owner(authorization)
    if _registry is None:
        raise HTTPException(status_code=503, detail="Server not yet initialized")

    _, history = _load_session_or_404(req.session_id, owner)
    rid = uuid.uuid4().hex[:8]  # N5 — request id threaded into the loop's log lines
    try:
        result, _ = run_agent(
            req.goal, _config, role=req.role, agency=req.agency,
            registry=_registry, history=history, run_id=rid, owner=owner,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if result is None:  # N-review: don't record a bogus (answer-less) turn or charge tokens on 422
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
    """M15/N3 — Server-Sent Events: stream tool_call / tool_result / token / final events as the
    agent works. Each SSE line is `data: {json}`; exactly one terminal event has type 'final' or
    'error'. If the client disconnects, the run is cancelled promptly (N3) and no turn is recorded
    unless a real final answer was produced. The blocking generator runs in a worker thread so the
    event loop can poll disconnect."""
    import anyio
    from bob_loop import run_agent_events, CancelToken

    owner = _authed_owner(authorization)
    if _registry is None:
        raise HTTPException(status_code=503, detail="Server not yet initialized")

    _, history = _load_session_or_404(req.session_id, owner)
    cancel = CancelToken()
    sentinel = object()
    rid = uuid.uuid4().hex[:8]  # N5 — request id threaded into the loop's log lines

    async def _sse():
        final_result = None
        got_final = False
        gen = run_agent_events(
            req.goal, _config, role=req.role, agency=req.agency,
            registry=_registry, stream=True, history=history, cancel=cancel, run_id=rid, owner=owner,
        )
        try:
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
            cancel.cancel()
            try:  # drain in a worker thread so the generator's finally runs (SIGINT restore)
                await anyio.to_thread.run_sync(lambda: _drain(gen))
            except Exception:
                pass
            if got_final and final_result is not None:  # N3 — no bogus turn on disconnect/error/max_steps
                _record_turn(req.session_id, req.goal, final_result)

    return StreamingResponse(_sse(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn
    from bob_core import _port, load_config

    _agent = load_config().get("agent", {})
    # Default to loopback. Set agent.serveHost = '0.0.0.0' in bob.psd1 to expose on the LAN
    # (also harden web_fetch — see MODULE-M / M9 — before doing so).
    uvicorn.run(
        app,
        host=_agent.get("serveHost", "127.0.0.1"),
        port=_port(_agent, "agentPort"),
    )
