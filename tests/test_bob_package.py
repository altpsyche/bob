"""The `python -m bob` package exposes the runtime API and serves the agent
HTTP app. Proves (OS-neutrally, no live socket) that `from bob import
run_agent_events` works and that the app the package's `agent serve` starts answers /health and an
owner-scoped session round-trip against a stub. This is the Linux proof, run in-process."""
import shutil
import tempfile
import unittest
from pathlib import Path

import _common
import bob_agent_server as srv
from bob_session import SessionStore
from fastapi.testclient import TestClient


class TestPackageApi(unittest.TestCase):
    def test_run_agent_events_importable(self):
        import bob

        self.assertTrue(callable(bob.run_agent_events))

    def test_agent_serve_wired_to_handler(self):
        from bob import cli, registry

        self.assertEqual(registry.by_name()["agent serve"]["handler"], "agent_serve")
        self.assertIn("agent_serve", cli._HANDLERS)


class TestServedApp(unittest.TestCase):
    """The FastAPI app that `python -m bob agent serve` launches, driven via TestClient."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bob-pkg-"))
        self._saved = (srv._config, srv._token_owner, srv._registry, srv._sessions)
        srv._config = _common.fake_config()
        srv._token_owner = {"sk-test": "alice", "sk-bob": "bob"}
        srv._registry = _common.FakeRegistry()
        srv._sessions = SessionStore(self.dir / "s.db")
        self.client = TestClient(srv.app)

    def tearDown(self):
        srv._sessions.close()
        srv._config, srv._token_owner, srv._registry, srv._sessions = self._saved
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_health(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")

    def test_owner_scoped_session_roundtrip(self):
        # create as alice
        r = self.client.post("/v1/sessions", headers={"Authorization": "Bearer sk-test"})
        self.assertEqual(r.status_code, 200)
        sid = r.json()["session_id"]
        # alice can read her own session
        self.assertEqual(self.client.get(f"/v1/sessions/{sid}",
                                         headers={"Authorization": "Bearer sk-test"}).status_code, 200)
        # bob cannot (owner isolation -> 404, no existence leak)
        self.assertEqual(self.client.get(f"/v1/sessions/{sid}",
                                         headers={"Authorization": "Bearer sk-bob"}).status_code, 404)
        # no auth -> 401
        self.assertEqual(self.client.get(f"/v1/sessions/{sid}").status_code, 401)


if __name__ == "__main__":
    unittest.main()
