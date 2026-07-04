"""M13 gap-close — agent HTTP server: auth (M5/M12), ownership (N1), completion recording,
budget, and the SSE endpoint (M15) incl. cancellation/disconnect (N3). Calls the route functions
directly (no live socket) with fake registry/LLM so it stays hermetic and offline."""
import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path

import _common
import bob_agent_server as srv
import bob_loop
from bob_authstore import AuthStore
from bob_session import SessionStore
from fastapi import HTTPException

GOOD = "Bearer sk-test"     # owner: alice
GOOD_B = "Bearer sk-bob"    # owner: bob


class _FakeRequest:
    """Minimal stand-in for starlette.Request — only is_disconnected() is used by the SSE route."""

    def __init__(self, disconnected=False):
        self._disconnected = disconnected

    async def is_disconnected(self):
        return self._disconnected


async def _collect_sse(resp):
    """Drain a StreamingResponse's async body into a list of decoded strings."""
    out = []
    async for chunk in resp.body_iterator:
        out.append(chunk.decode() if isinstance(chunk, (bytes, bytearray)) else chunk)
    return out


class TestServer(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bob-srv-"))
        srv._config = _common.fake_config()
        srv._token_owner = {"sk-test": "alice", "sk-bob": "bob"}  # N1 — token -> owner
        srv._registry = _common.FakeRegistry()
        srv._sessions = SessionStore(self.dir / "s.db")

    def tearDown(self):
        srv._sessions.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    # --- auth (M5/M12) -------------------------------------------------------
    def test_auth_rejects_bad_token(self):
        with self.assertRaises(HTTPException) as ctx:
            srv._require_auth("Bearer nope")
        self.assertEqual(ctx.exception.status_code, 401)

    def test_auth_accepts_configured_token(self):
        srv._require_auth(GOOD)  # must not raise

    def test_health_needs_no_auth(self):
        self.assertEqual(srv.health()["status"], "ok")

    def test_completion_requires_auth(self):
        with self.assertRaises(HTTPException) as ctx:
            srv.agent_completions(srv.AgentRequest(goal="hi"), authorization="")
        self.assertEqual(ctx.exception.status_code, 401)

    # --- sessions (M12) ------------------------------------------------------
    def test_session_lifecycle(self):
        sid = srv.create_session(srv.SessionCreate(token_budget=50), authorization=GOOD)["session_id"]
        self.assertEqual(srv.get_session(sid, authorization=GOOD)["id"], sid)
        self.assertTrue(srv.delete_session(sid, authorization=GOOD)["deleted"])

    def test_unknown_session_404(self):
        with self.assertRaises(HTTPException) as ctx:
            srv.get_session("nope", authorization=GOOD)
        self.assertEqual(ctx.exception.status_code, 404)

    # --- ownership (N1) ------------------------------------------------------
    def _alice_session(self):
        return srv.create_session(srv.SessionCreate(), authorization=GOOD)["session_id"]

    def test_owner_cannot_read_others_session_404(self):
        sid = self._alice_session()
        with self.assertRaises(HTTPException) as ctx:
            srv.get_session(sid, authorization=GOOD_B)   # bob reading alice's session
        self.assertEqual(ctx.exception.status_code, 404)
        # alice still can
        self.assertEqual(srv.get_session(sid, authorization=GOOD)["id"], sid)

    def test_owner_cannot_delete_others_session(self):
        sid = self._alice_session()
        self.assertFalse(srv.delete_session(sid, authorization=GOOD_B)["deleted"])
        self.assertEqual(srv.get_session(sid, authorization=GOOD)["id"], sid)  # untouched

    def test_owner_cannot_complete_on_others_session_404(self):
        sid = self._alice_session()
        with self.assertRaises(HTTPException) as ctx:
            srv.agent_completions(
                srv.AgentRequest(goal="hi", session_id=sid), authorization=GOOD_B)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_owner_cannot_stream_on_others_session_404(self):
        sid = self._alice_session()
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(srv.agent_completions_stream(
                srv.AgentRequest(goal="hi", session_id=sid),
                _FakeRequest(), authorization=GOOD_B))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_unknown_and_unowned_are_indistinguishable(self):
        sid = self._alice_session()
        unknown = self._exc_status(lambda: srv.get_session("nope", authorization=GOOD_B))
        unowned = self._exc_status(lambda: srv.get_session(sid, authorization=GOOD_B))
        self.assertEqual(unknown, unowned, 404)

    @staticmethod
    def _exc_status(fn):
        try:
            fn()
        except HTTPException as e:
            return e.status_code
        return None

    def test_completion_records_turn(self):
        sid = srv.create_session(srv.SessionCreate(), authorization=GOOD)["session_id"]
        orig = bob_loop.run_agent
        bob_loop.run_agent = lambda *a, **k: ("answer", False)
        try:
            resp = srv.agent_completions(
                srv.AgentRequest(goal="hi", session_id=sid), authorization=GOOD)
        finally:
            bob_loop.run_agent = orig
        self.assertEqual(resp.result, "answer")
        self.assertEqual(resp.session_id, sid)
        self.assertEqual(len(srv.get_session(sid, authorization=GOOD)["history"]), 2)

    def test_completion_max_steps_422_records_no_turn(self):
        # N-review: on a 422 (result None) the non-stream route must NOT record a turn or charge tokens.
        sid = self._alice_session()
        orig = bob_loop.run_agent
        bob_loop.run_agent = lambda *a, **k: (None, False)
        try:
            with self.assertRaises(HTTPException) as ctx:
                srv.agent_completions(
                    srv.AgentRequest(goal="hi", session_id=sid), authorization=GOOD)
        finally:
            bob_loop.run_agent = orig
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(len(srv.get_session(sid, authorization=GOOD)["history"]), 0)

    def test_completion_over_budget_402(self):
        sid = srv.create_session(srv.SessionCreate(token_budget=1), authorization=GOOD)["session_id"]
        srv._sessions.append_turn(sid, "x", "y", tokens_used=10)  # exhaust the 1-token budget
        with self.assertRaises(HTTPException) as ctx:
            srv.agent_completions(srv.AgentRequest(goal="hi", session_id=sid), authorization=GOOD)
        self.assertEqual(ctx.exception.status_code, 402)

    # --- SSE endpoint (M15) + cancellation/disconnect (N3) -------------------
    def test_stream_requires_auth(self):
        with self.assertRaises(HTTPException):
            asyncio.run(srv.agent_completions_stream(
                srv.AgentRequest(goal="hi"), _FakeRequest(), authorization=""))

    def test_stream_returns_event_stream(self):
        orig = bob_loop.run_agent_events
        bob_loop.run_agent_events = lambda *a, **k: iter(
            [{"type": "final", "result": "ok", "exit_requested": False, "reason": "answer"}]
        )
        try:
            resp = asyncio.run(srv.agent_completions_stream(
                srv.AgentRequest(goal="hi"), _FakeRequest(), authorization=GOOD))
        finally:
            bob_loop.run_agent_events = orig
        self.assertEqual(resp.media_type, "text/event-stream")

    def test_stream_records_turn_on_real_final(self):
        sid = self._alice_session()
        orig = bob_loop.run_agent_events
        bob_loop.run_agent_events = lambda *a, **k: iter(
            [{"type": "final", "result": "done", "exit_requested": False, "reason": "answer"}]
        )
        try:
            resp = asyncio.run(srv.agent_completions_stream(
                srv.AgentRequest(goal="hi", session_id=sid), _FakeRequest(), authorization=GOOD))
            lines = asyncio.run(_collect_sse(resp))
        finally:
            bob_loop.run_agent_events = orig
        self.assertEqual(sum('"type": "final"' in ln for ln in lines), 1)
        self.assertEqual(len(srv.get_session(sid, authorization=GOOD)["history"]), 2)

    def test_stream_disconnect_stops_and_records_no_turn(self):
        sid = self._alice_session()
        orig = bob_loop.run_agent_events
        # Would yield a token then a final — but the client is already gone.
        bob_loop.run_agent_events = lambda *a, **k: iter([
            {"type": "token", "text": "hi"},
            {"type": "final", "result": "done", "exit_requested": False, "reason": "answer"},
        ])
        try:
            resp = asyncio.run(srv.agent_completions_stream(
                srv.AgentRequest(goal="hi", session_id=sid),
                _FakeRequest(disconnected=True), authorization=GOOD))
            lines = asyncio.run(_collect_sse(resp))
        finally:
            bob_loop.run_agent_events = orig
        self.assertEqual(lines, [])  # nothing emitted to a dead socket
        self.assertEqual(len(srv.get_session(sid, authorization=GOOD)["history"]), 0)  # no bogus turn


class TestServerAuthO8(unittest.TestCase):
    """O8 — DB token store (hot revoke), RBAC scopes (tool + role), per-owner rate limits."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bob-auth-srv-"))
        srv._config = _common.fake_config()
        srv._token_owner = {"sk-test": "alice"}
        srv._token_meta = {}
        srv._registry = _common.FakeRegistry()
        srv._sessions = SessionStore(self.dir / "s.db")
        srv._authstore = AuthStore(self.dir / "s.db", salt="t")
        srv._rate_buckets = {}
        self._orig_mono = srv._monotonic

    def tearDown(self):
        srv._monotonic = self._orig_mono
        srv._authstore.close()
        srv._sessions.close()
        srv._authstore = None
        srv._token_meta = {}
        srv._rate_buckets = {}
        shutil.rmtree(self.dir, ignore_errors=True)

    # --- DB token store + hot revoke -----------------------------------------
    def test_db_token_authenticates(self):
        tok = srv._authstore.issue("carol")
        self.assertEqual(srv._authed_owner(f"Bearer {tok}"), "carol")

    def test_hot_revoke_401_without_restart(self):
        tok = srv._authstore.issue("carol")
        self.assertEqual(srv._authed_owner(f"Bearer {tok}"), "carol")
        srv._authstore.revoke(tok)
        with self.assertRaises(HTTPException) as ctx:
            srv._authed_owner(f"Bearer {tok}")           # next call already 401 — no restart
        self.assertEqual(ctx.exception.status_code, 401)

    def test_config_token_still_works_with_store(self):
        self.assertEqual(srv._authed_owner("Bearer sk-test"), "alice")

    def test_unknown_token_401(self):
        with self.assertRaises(HTTPException) as ctx:
            srv._authed_owner("Bearer bob_nope")
        self.assertEqual(ctx.exception.status_code, 401)

    # --- rate limit ----------------------------------------------------------
    def test_rate_limit_429_then_recovers(self):
        ident = srv._Identity("dave", None, 2)          # 2 requests / min
        srv._check_rate(ident)
        srv._check_rate(ident)                           # bucket now empty
        with self.assertRaises(HTTPException) as ctx:
            srv._check_rate(ident)
        self.assertEqual(ctx.exception.status_code, 429)
        srv._monotonic = lambda: self._orig_mono() + 60  # a minute later -> refilled
        srv._check_rate(ident)                            # must not raise

    def test_rate_zero_is_unlimited(self):
        ident = srv._Identity("eve", None, 0)
        for _ in range(50):
            srv._check_rate(ident)                        # never raises

    # --- tool scopes ---------------------------------------------------------
    @staticmethod
    def _reg_with(names):
        from tool_registry import ToolRegistry
        reg = ToolRegistry()
        for n in names:
            reg.tool_schemas.append({"type": "function",
                                     "function": {"name": n, "parameters": {"type": "object"}}})
            reg.dispatch[n] = lambda **k: "ok"
        return reg

    def test_out_of_scope_tool_hidden_and_refused(self):
        srv._registry = self._reg_with(["file_read", "file_write", "web_fetch"])
        scoped = srv._scoped_registry(srv._Identity("alice", ["file_*"], 0))
        names = {s["function"]["name"] for s in scoped.tool_schemas}
        self.assertEqual(names, {"file_read", "file_write"})       # web_fetch hidden
        self.assertIn("not available", scoped.dispatch_call("web_fetch", "{}"))
        self.assertEqual(scoped.dispatch_call("file_read", "{}"), "ok")

    def test_no_scopes_returns_base_registry(self):
        srv._registry = self._reg_with(["file_read"])
        self.assertIs(srv._scoped_registry(srv._Identity("a", None, 0)), srv._registry)

    def test_only_role_scopes_leaves_tools_unrestricted(self):
        srv._registry = self._reg_with(["file_read", "web_fetch"])
        self.assertIs(srv._scoped_registry(srv._Identity("a", ["role:code"], 0)), srv._registry)

    # --- role scopes ---------------------------------------------------------
    def test_role_scope_refused_403(self):
        ident = srv._Identity("alice", ["role:code"], 0)
        with self.assertRaises(HTTPException) as ctx:
            srv._check_role_scope(ident, "chat")
        self.assertEqual(ctx.exception.status_code, 403)
        srv._check_role_scope(ident, "code")   # allowed -> no raise
        srv._check_role_scope(ident, None)     # default role -> no raise

    def test_no_role_scopes_allows_any_role(self):
        srv._check_role_scope(srv._Identity("a", ["file_*"], 0), "chat")  # no role: entries -> ok


if __name__ == "__main__":
    unittest.main()
