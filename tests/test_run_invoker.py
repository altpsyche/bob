"""ONE-C §1a — the deterministic invoker `bob --run <cap> '{json}'`.

Proves `--run` routes through the EXACT agent path (ToolRegistry.dispatch_call): same capability, same
args, no model, no parallel dispatcher. Validation (missing cap / non-JSON / non-object) short-circuits
before any registry build, so those cases are hermetic; the dispatch cases stub _build_registry with a
recording fake so nothing touches real tools or the network."""
import io
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

import _common  # noqa: F401 — puts scripts/ on sys.path
from bob import cli


class _RecordingRegistry:
    """Captures (name, args_json) and returns a scripted result — so a test can assert the exact
    capability + arguments reached dispatch_call unchanged."""

    def __init__(self, result="[ok]"):
        self.calls = []
        self._result = result

    def dispatch_call(self, name, arguments_json, context=None):
        self.calls.append((name, arguments_json))
        return self._result


def _run(argv):
    """Invoke cli.main(argv), returning (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class TestRunValidation(unittest.TestCase):
    """Pre-dispatch validation — no registry build, no dispatch."""

    def test_no_capability_is_usage_error(self):
        code, _, err = _run(["--run"])
        self.assertEqual(code, 2)
        self.assertIn("usage", err.lower())

    def test_malformed_json_is_error(self):
        code, _, err = _run(["--run", "some_cap", "not json"])
        self.assertEqual(code, 2)
        self.assertIn("JSON object", err)

    def test_non_object_json_is_error(self):
        code, _, err = _run(["--run", "some_cap", "[1, 2]"])
        self.assertEqual(code, 2)
        self.assertIn("JSON object", err)

    def test_validation_never_builds_a_registry(self):
        # A malformed call must fail before importing/loading tools (fast + no side effects).
        with mock.patch.object(cli, "_build_registry",
                               side_effect=AssertionError("registry must not be built")):
            code, _, _ = _run(["--run", "cap", "bad"])
        self.assertEqual(code, 2)


class TestRunDispatch(unittest.TestCase):
    """Dispatch path — stubbed registry, so it stays hermetic and asserts exact passthrough."""

    def test_capability_and_args_pass_through_unchanged(self):
        reg = _RecordingRegistry(result="recalled: 3 hits")
        with mock.patch.object(cli, "_build_registry", return_value=reg):
            code, out, _ = _run(["--run", "memory_recall", '{"query": "bob", "k": 3}'])
        self.assertEqual(code, 0)
        self.assertEqual(reg.calls, [("memory_recall", '{"query": "bob", "k": 3}')])
        self.assertIn("recalled: 3 hits", out)

    def test_omitted_json_defaults_to_empty_object(self):
        reg = _RecordingRegistry()
        with mock.patch.object(cli, "_build_registry", return_value=reg):
            code, _, _ = _run(["--run", "git_status"])
        self.assertEqual(code, 0)
        self.assertEqual(reg.calls, [("git_status", "{}")])

    def test_unknown_capability_exits_nonzero(self):
        # dispatch_call returns the real "Unknown tool: X" marker -> _is_error_result -> non-zero.
        reg = _RecordingRegistry(result="Unknown tool: no_such_tool")
        with mock.patch.object(cli, "_build_registry", return_value=reg):
            code, out, _ = _run(["--run", "no_such_tool", "{}"])
        self.assertEqual(code, 1)
        self.assertIn("Unknown tool", out)

    def test_tool_error_result_exits_nonzero(self):
        reg = _RecordingRegistry(result="Tool error (memory_recall): boom")
        with mock.patch.object(cli, "_build_registry", return_value=reg):
            code, _, _ = _run(["--run", "memory_recall", "{}"])
        self.assertEqual(code, 1)

    def test_successful_result_exits_zero(self):
        reg = _RecordingRegistry(result="stored memory 42")
        with mock.patch.object(cli, "_build_registry", return_value=reg):
            code, out, _ = _run(["--run", "memory_store", '{"content": "x"}'])
        self.assertEqual(code, 0)
        self.assertIn("stored memory 42", out)


if __name__ == "__main__":
    unittest.main()
