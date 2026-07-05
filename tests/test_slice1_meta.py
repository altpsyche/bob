"""ONE-C Slice 1 — memory + meta capabilities on Python.

Covers the registry wiring (verbs flipped to runtime=python with a live handler), the budget capability
core (port of bob-budget.ps1, network-mocked), and the CLI handlers for remember/recall/memory/fabric/
aider/plugins. Everything that would touch a service or subprocess is mocked, so the suite stays hermetic."""
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

import _common  # noqa: F401 — puts scripts/ + scripts/tools on sys.path
from bob import cli, registry

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "tools"))
import budget  # noqa: E402


def _run(handler, rest):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = handler(rest)
    return code, out.getvalue(), err.getvalue()


class TestRegistryWiring(unittest.TestCase):
    """Each Slice-1 verb is runtime=python with a handler that exists in cli._HANDLERS."""

    SLICE1 = {
        "remember": "remember", "recall": "recall", "memory": "memory", "budget": "budget",
        "tools": "tools", "plugins": "plugins", "fabric": "fabric", "aider": "aider",
    }

    def test_verbs_flipped_to_python_with_live_handlers(self):
        by_name = registry.by_name()
        for verb, handler_key in self.SLICE1.items():
            entry = by_name[verb]
            self.assertEqual(entry["handler"], handler_key, f"{verb} handler key")
            self.assertIn(handler_key, cli._HANDLERS, f"{handler_key} missing from _HANDLERS")


class TestBudgetCore(unittest.TestCase):
    """budget.budget_summary — the one core fn the tool, verb, and --run all call."""

    def _config(self, db_path):
        return {"litellmPort": 8081, "langfusePort": 3000, "memory": {"dbPath": db_path}}

    def test_litellm_down_shows_hint_and_local_db(self):
        import requests
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "bob.db"
            db.write_bytes(b"x" * 2048)  # 2 KB
            with mock.patch("bob_core._get_db_path", return_value=str(db)), \
                 mock.patch.object(requests, "get", side_effect=requests.RequestException("down")):
                out = budget.budget_summary(self._config(str(db)))
        self.assertIn("LiteLLM not running", out)
        self.assertIn("Local memory DB", out)
        self.assertIn("cost: $0", out)

    def test_reports_all_time_spend_when_litellm_up(self):
        import requests

        def fake_get(url, **kw):
            resp = mock.Mock()
            if url.endswith("/global/spend"):
                resp.json.return_value = {"spend": 1.23456}
            else:
                resp.json.return_value = {}
            return resp

        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "bob.db"
            with mock.patch("bob_core._get_db_path", return_value=str(db)), \
                 mock.patch.object(requests, "get", side_effect=fake_get):
                out = budget.budget_summary(self._config(str(db)))
        self.assertIn("Spend (all-time)", out)
        self.assertIn("1.2346", out)  # rounded to 4 dp

    def test_registered_as_agent_tool(self):
        self.assertIn("budget_summary", budget.DISPATCH)
        self.assertEqual(budget.TOOL_DEFS[0]["function"]["name"], "budget_summary")


class TestMemoryVerbHandlers(unittest.TestCase):
    def test_remember_wires_to_memory_store(self):
        with mock.patch("bob_core.memory_store", return_value="Stored (id=1): hi") as store:
            code, out, _ = _run(cli._handle_remember, ["hi", "there"])
        self.assertEqual(code, 0)
        store.assert_called_once_with("hi there")
        self.assertIn("Stored", out)

    def test_remember_empty_is_usage_error(self):
        code, _, err = _run(cli._handle_remember, [])
        self.assertEqual(code, 1)
        self.assertIn("usage", err.lower())

    def test_recall_wires_to_memory_recall(self):
        with mock.patch("bob_core.memory_recall", return_value="a note") as rc:
            code, out, _ = _run(cli._handle_recall, ["what", "did", "i", "say"])
        self.assertEqual(code, 0)
        rc.assert_called_once_with("what did i say")
        self.assertIn("a note", out)

    def test_memory_delegates_to_bob_memory_with_db(self):
        seen = {}

        def fake_main():
            seen["argv"] = list(sys.argv)

        with mock.patch("bob_memory.main", side_effect=fake_main):
            code, _, _ = _run(cli._handle_memory, ["list"])
        self.assertEqual(code, 0)
        self.assertEqual(seen["argv"][0], "bob_memory")
        self.assertIn("--db", seen["argv"])
        self.assertEqual(seen["argv"][-1], "list")

    def test_memory_no_arg_defaults_to_status(self):
        seen = {}
        with mock.patch("bob_memory.main", side_effect=lambda: seen.update(argv=list(sys.argv))):
            _run(cli._handle_memory, [])
        self.assertEqual(seen["argv"][-1], "status")

    def test_memory_unknown_subcommand_rejected_before_dispatch(self):
        with mock.patch("bob_memory.main", side_effect=AssertionError("must not run")):
            code, _, err = _run(cli._handle_memory, ["frobnicate"])
        self.assertEqual(code, 1)
        self.assertIn("Usage", err)


class TestPassthroughHandlers(unittest.TestCase):
    def test_fabric_missing_binary_returns_error(self):
        import osenv
        with mock.patch.object(osenv, "bin_exe", return_value=Path("/nonexistent/fabric")):
            code, _, err = _run(cli._handle_fabric, [])
        self.assertEqual(code, 1)
        self.assertIn("fabric-setup", err)

    def test_aider_missing_venv_returns_error(self):
        import osenv
        with mock.patch.object(osenv, "venv_exe", return_value=Path("/nonexistent/aider")):
            code, _, err = _run(cli._handle_aider, [])
        self.assertEqual(code, 1)
        self.assertIn("aider", err.lower())

    def test_plugins_lists_installed(self):
        code, out, _ = _run(cli._handle_plugins, ["list"])
        self.assertEqual(code, 0)
        self.assertIn("Installed plugins", out)

    def test_plugins_rejects_bad_subcommand(self):
        code, _, err = _run(cli._handle_plugins, ["nope"])
        self.assertEqual(code, 1)
        self.assertIn("Usage", err)


if __name__ == "__main__":
    unittest.main()
