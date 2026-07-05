"""ONE-D Slice D4 — the `eval` capability (scripts/tools/models.py:eval_model). Hermetic: the venv-eval
lm_eval binary, the endpoint check, and the lm_eval subprocess are mocked. eval is CLI-only + very long
(DD3: the isolated venv-eval), so it is NOT an agent tool and NOT on --run; it returns the process rc."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _common  # noqa: F401 — puts scripts/ on sys.path
from bob import cli, registry

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "tools"))
import models as models_mod  # noqa: E402

CFG = {"port": 8080}


class TestRegistryWiring(unittest.TestCase):
    def test_eval_flipped_to_python(self):
        entry = registry.by_name()["eval"]
        self.assertEqual(entry["runtime"], "python")
        self.assertEqual(entry["handler"], "eval")
        self.assertIn("eval", cli._HANDLERS)

    def test_eval_is_not_an_agent_tool(self):
        # very long + separate venv -> CLI-only, deliberately not exposed to the loop
        self.assertNotIn("eval_model", models_mod.DISPATCH)


class TestEvalModel(unittest.TestCase):
    def setUp(self):
        self.venv_exe = Path(tempfile.mkdtemp()) / "lm_eval"
        self.venv_exe.write_text("#!/bin/sh\n")  # exists
        self.roles = {"coder": {"gguf": "c.gguf", "tokenizer": "Qwen/Qwen2.5-Coder-14B"}}

    def _run(self, role="coder", task="mmlu", shots=0, limit=0, roles=None, endpoint_ok=True,
             venv_exists=True, captured=None):
        import requests
        exe = self.venv_exe if venv_exists else (self.venv_exe.parent / "missing")
        get = mock.Mock() if endpoint_ok else mock.Mock(side_effect=requests.RequestException("down"))

        def fake_sub(args, **kw):
            if captured is not None:
                captured["args"] = args
                captured["env"] = kw.get("env", {})
            return mock.Mock(returncode=0)

        # D8: a missing venv-eval triggers a lazy osenv.new_bob_venv provision (replacing the retired
        # bootstrap-eval.ps1). Simulate "couldn't provision" so the missing-venv path stays deterministic.
        with mock.patch("osenv.venv_exe", return_value=exe), \
             mock.patch("osenv.new_bob_venv", side_effect=RuntimeError("no venv here")), \
             mock.patch("bob_models.profile_roles", return_value=roles if roles is not None else self.roles), \
             mock.patch("requests.get", get), \
             mock.patch("subprocess.run", side_effect=fake_sub):
            return models_mod.eval_model(role, task, shots=shots, limit=limit, config=CFG, now="20260705-1200")

    def test_missing_venv_provisions_then_returns_1_when_unavailable(self):
        # venv-eval absent -> attempts provisioning; when that fails, returns 1 (not a crash).
        self.assertEqual(self._run(venv_exists=False), 1)

    def test_unknown_role_returns_1(self):
        self.assertEqual(self._run(role="nope"), 1)

    def test_missing_tokenizer_returns_1(self):
        self.assertEqual(self._run(roles={"coder": {"gguf": "c.gguf"}}), 1)

    def test_endpoint_down_returns_1(self):
        self.assertEqual(self._run(endpoint_ok=False), 1)

    def test_happy_path_builds_lm_eval_args(self):
        cap = {}
        rc = self._run(task="gsm8k", shots=5, limit=100, captured=cap)
        self.assertEqual(rc, 0)
        args = cap["args"]
        self.assertEqual(args[0], str(self.venv_exe))
        joined = " ".join(args)
        self.assertIn("--tasks gsm8k", joined)
        self.assertIn("--num_fewshot 5", joined)
        self.assertIn("--limit 100", joined)
        model_args = args[args.index("--model_args") + 1]
        self.assertIn("base_url=http://localhost:8080/v1/chat/completions", model_args)
        self.assertIn("model=coder", model_args)
        self.assertIn("tokenizer=Qwen/Qwen2.5-Coder-14B", model_args)
        self.assertEqual(cap["env"].get("PYTHONUTF8"), "1")

    def test_no_limit_omits_flag(self):
        cap = {}
        self._run(limit=0, captured=cap)
        self.assertNotIn("--limit", cap["args"])


class TestCliArgParsing(unittest.TestCase):
    def _dispatch(self, rest):
        seen = {}
        fake = mock.Mock()
        fake.eval_model = mock.Mock(side_effect=lambda *a, **k: seen.update(args=a, kw=k) or 0)
        with mock.patch.object(cli, "_models_mod", return_value=fake), \
             mock.patch.object(cli, "_cfg", return_value=CFG):
            rc = cli._handle_eval(rest)
        return rc, seen

    def test_positional_and_flags(self):
        rc, seen = self._dispatch(["planner", "hellaswag", "--shots", "5", "--limit", "50"])
        self.assertEqual(rc, 0)
        self.assertEqual(seen["args"][:2], ("planner", "hellaswag"))
        self.assertEqual(seen["kw"]["shots"], 5)
        self.assertEqual(seen["kw"]["limit"], 50)

    def test_defaults(self):
        _, seen = self._dispatch([])
        self.assertEqual(seen["args"][:2], ("coder", "mmlu"))
        self.assertEqual(seen["kw"]["shots"], 0)


if __name__ == "__main__":
    unittest.main()
