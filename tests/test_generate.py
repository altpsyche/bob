"""Config generators (scripts/tools/generate.py). Emit deterministic, byte-stable output across
every profile incl. the cpu tier.

Hermetic: reads the real config/models.json (the neutral registry) and writes the generated files to
their normal deterministic locations (idempotent — same bytes each run); gen_webui is tested against a
minimal temp sqlite db. No network."""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import _common  # noqa: F401 — puts scripts/ on sys.path
import osenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "tools"))
import generate as gen  # noqa: E402
import bob_core  # noqa: E402

CFG = bob_core.load_config()
gen.configure(CFG)


class TestGenerateToolSurface(unittest.TestCase):
    def test_tool_registered_and_mutating(self):
        self.assertEqual(set(gen.DISPATCH), {"gen"})
        self.assertEqual(gen.MUTATING_TOOLS, {"gen"})


class TestFmt(unittest.TestCase):
    def test_bool(self):
        self.assertEqual(gen._fmt(True), "true")
        self.assertEqual(gen._fmt(False), "false")

    def test_int_and_integral_float(self):
        self.assertEqual(gen._fmt(16384), "16384")
        self.assertEqual(gen._fmt(1.0), "1")   # integral float drops the decimal
        self.assertEqual(gen._fmt(30.0), "30")

    def test_fractional_float(self):
        self.assertEqual(gen._fmt(0.7), "0.7")
        self.assertEqual(gen._fmt(0.9), "0.9")


class TestEnabledPeers(unittest.TestCase):
    def test_filters_disabled(self):
        import bob_models
        peers = gen.enabled_peers(bob_models.load_models_config())
        names = {p["name"] for p in peers}
        self.assertIn("deepseek", names)     # enabled
        self.assertNotIn("zhipu", names)     # enabled: false


class TestLlamaSwap(unittest.TestCase):
    def _gen(self, profile=None):
        gen.gen_llama_swap(profile)
        return (gen.REPO / "config" / "llama-swap.yaml").read_text(encoding="utf-8")

    def test_macros_and_group(self):
        out = self._gen("16gb")
        server = osenv.exe_name("llama-server")   # llama-server.exe on Windows
        self.assertIn(f'srv: "${{env.LLAMA_LOCAL_ROOT}}/bin/{server} --port ${{PORT}} -ngl 99 --flash-attn on '
                      f'--reasoning-format deepseek"', out)
        self.assertIn('kv: "--cache-type-k q8_0 --cache-type-v q8_0"', out)
        self.assertIn("members: [ponder, coder, chat, vision, agent]", out)

    def test_draft_and_setparams_and_ttl(self):
        out = self._gen("16gb")
        # coder has a pinned draft (fim) -> speculative decode flags appended
        self.assertIn("-md ${env.LLAMA_LOCAL_ROOT}/models/qwen-coder-3b-q8_0.gguf -ngld 99", out)
        # chat setParams sorted
        self.assertIn("setParams: { temperature: 0.7, top_p: 0.9 }", out)
        # fim/embed ttl 0
        self.assertRegex(out, r"fim:\n.*\n\s+ttl: 0")

    def test_moe_offload_emitted_for_overflow_model(self):
        out = self._gen("16gb")
        ponder = next(ln for ln in out.splitlines() if "qwen3-30b-a3b" in ln)
        self.assertIn("--n-cpu-moe 24", ponder)   # 30B MoE spills experts to RAM so it fits 16GB
        chat = next(ln for ln in out.splitlines() if "qwen3-14b" in ln)
        self.assertNotIn("--n-cpu-moe", chat)     # dense model that fits: no offload

    def test_moe_offload_per_profile(self):
        # The 30B-A3B ponder overflows the 16gb and 24gb cards (b9993 no longer auto-spills at -ngl 99),
        # so each carries its own tuned offload; the 32gb Q6_K fits with headroom and gets none.
        out24 = self._gen("24gb")
        self.assertIn("--n-cpu-moe 12", next(ln for ln in out24.splitlines() if "qwen3-30b-a3b" in ln))
        out32 = self._gen("32gb")
        self.assertNotIn("--n-cpu-moe", next(ln for ln in out32.splitlines() if "qwen3-30b-a3b" in ln))

    def test_vision_expands_srv_without_flashattn(self):
        # mmproj is incompatible with flash-attn -> that model's cmd uses an inline srv sans --flash-attn
        out = self._gen("16gb")
        vision_line = next(ln for ln in out.splitlines() if "qwen2-vl" in ln)
        self.assertIn("-ngl 99 -m", vision_line)
        self.assertNotIn("--flash-attn", vision_line)
        self.assertIn("--mmproj ${env.LLAMA_LOCAL_ROOT}/models/mmproj-Qwen2-VL-7B-Instruct-f16.gguf", vision_line)

    def test_cpu_profile_no_gpu_no_kv(self):
        out = self._gen("cpu")
        self.assertIn("-ngl 0", out)
        self.assertNotIn("--flash-attn", out)
        self.assertNotIn("--cache-type-k", out)  # kv macro empty on cpu


class TestLitellm(unittest.TestCase):
    def _gen(self):
        gen.gen_litellm()
        return (gen.REPO / "config" / "litellm.yaml").read_text(encoding="utf-8")

    def test_local_and_pro_models(self):
        out = self._gen()
        self.assertIn("  - model_name: ponder\n    litellm_params:\n      model: openai/ponder", out)
        self.assertIn("      supports_vision: true", out)   # vision
        # pro models: deepseek peer, roles sorted
        self.assertIn("  - model_name: chat-pro", out)
        self.assertIn("      model: deepseek/deepseek-chat", out)
        self.assertIn("      api_key: os.environ/DEEPSEEK_API_KEY", out)

    def test_settings(self):
        out = self._gen()
        self.assertIn("  num_retries: 3", out)
        self.assertIn("  request_timeout: 600", out)
        self.assertIn("  master_key: sk-local", out)


class TestContinue(unittest.TestCase):
    def _gen(self):
        gen.gen_continue()
        return (gen.REPO / "config" / "continue" / "config.yaml").read_text(encoding="utf-8")

    def test_renames_and_skips_agent(self):
        out = self._gen()
        self.assertIn("  - name: autocomplete", out)   # fim -> autocomplete
        self.assertIn("  - name: embeddings", out)      # embed -> embeddings
        # 'agent' is Bob's own model, not a Continue client model
        self.assertNotIn("  - name: agent\n", out)

    def test_mcp_servers(self):
        out = self._gen()
        self.assertIn("mcpServers:", out)
        self.assertIn('SEARXNG_URL: "http://localhost:', out)


class TestWebui(unittest.TestCase):
    def test_skips_when_no_admin_user(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "webui.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE user (id TEXT, role TEXT)")   # no admin row
            conn.commit()
            conn.close()
            self.assertIn("no admin user found", gen._webui_write(str(db), [{"id": "chat", "prompt": "x"}]))

    def test_writes_prompts_to_minimal_db(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "webui.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE user (id TEXT, role TEXT)")
            conn.execute("INSERT INTO user (id, role) VALUES ('u1', 'admin')")
            conn.execute("""CREATE TABLE model (id TEXT PRIMARY KEY, user_id TEXT, base_model_id TEXT,
                            name TEXT, params TEXT, meta TEXT, updated_at INTEGER, created_at INTEGER,
                            is_active INTEGER)""")
            conn.commit()
            conn.close()
            msg = gen._webui_write(str(db), [{"id": "chat", "prompt": "Be concise."},
                                             {"id": "coder", "prompt": ""}])
            self.assertIn("Generated Open WebUI", msg)
            conn = sqlite3.connect(db)
            rows = dict(conn.execute("SELECT id, params FROM model").fetchall())
            conn.close()
            self.assertIn("chat", rows)
            self.assertIn("Be concise.", rows["chat"])
            self.assertEqual(rows["coder"], "{}")   # empty prompt -> cleared


if __name__ == "__main__":
    unittest.main()
