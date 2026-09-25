"""Config generators (scripts/tools/generate.py). Emit deterministic, byte-stable output across
every profile incl. the cpu tier.

Hermetic: reads the real config/models.json (the neutral registry) but writes every generated file under
a temp directory (generate.REPO is patched for the whole module, so the real config/ is never touched);
gen_webui is tested against a minimal temp sqlite db and dsh against a temp $DSH_HOME. No network."""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _common  # noqa: F401 — puts scripts/ on sys.path
import osenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "tools"))
import generate as gen  # noqa: E402
import bob_core  # noqa: E402

CFG = bob_core.load_config()
gen.configure(CFG)

_TMP = None
_REPO_PATCH = None
_OLD_FIXED_KEY = "sk-" + "local"   # the retired well-known LiteLLM key; no generated file may carry it


def setUpModule():
    global _TMP, _REPO_PATCH
    _TMP = tempfile.TemporaryDirectory(prefix="bob-gen-")
    _REPO_PATCH = mock.patch.object(gen, "REPO", Path(_TMP.name))
    _REPO_PATCH.start()


def tearDownModule():
    _REPO_PATCH.stop()
    _TMP.cleanup()


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
                      f'--reasoning-format deepseek -np 1"', out)
        self.assertIn('kv: "--cache-type-k q8_0 --cache-type-v q8_0"', out)
        # Aliased roles are names, not loadable models — only the concrete ones can be group members.
        self.assertIn("members: [chat, vision, fim]", out)
        # embed/rerank would otherwise fall into llama-swap's implicit exclusive default
        # group, where one memory lookup evicts the chat model.
        self.assertIn("  resident:\n    swap: false\n    exclusive: false\n    persistent: true\n    members: [embed, rerank]", out)

    def test_setparams_and_ttl(self):
        out = self._gen("8gb")
        # chat setParams sorted
        self.assertIn("setParams: { temperature: 0.7, top_p: 0.9 }", out)
        # fim/embed ttl 0
        self.assertRegex(out, r"fim:\n.*\n\s+ttl: 0")

    def test_alias_collapses_roles_onto_one_server(self):
        # 16gb: one Qwen3.8-27B serves chat/coder/ponder/writer/agent. The aliased roles get no cmd of
        # their own (a second cmd would be a second copy of the same 10 GB model in VRAM), and their
        # per-role sampling rides setParamsByID, which llama-swap applies by requested model id.
        out = self._gen("16gb")
        self.assertIn("    aliases: [ponder, coder, writer, agent]", out)
        self.assertEqual(out.count("qwen3.8-27b-gsq-rco-iq3_xxs.gguf"), 1)
        for role in ("ponder", "coder", "writer", "agent"):
            self.assertNotIn(f"  {role}:\n    cmd:", out)
        self.assertIn("      setParamsByID:", out)
        self.assertIn("        writer: { temperature: 0.6, top_p: 0.95 }", out)
        self.assertIn("        agent: { temperature: 0.1 }", out)

    def test_alias_target_is_downloaded_once(self):
        import provision
        _, models = provision.resolve_fetch_set("24gb")
        ggufs = [m["gguf"] for m in models]
        self.assertEqual(len(ggufs), len(set(ggufs)))
        self.assertIn("qwen3.8-27b-gsq-rco-iq3_s-mtp.gguf", ggufs)

    def test_mtp_draft_head_rides_the_model_file(self):
        # The -mtp GGUF carries its own draft block: --spec-type draft-mtp, NOT -md (which would load a
        # second full model and blow the card). 16gb cannot afford the ~900 MiB the draft costs, so it
        # runs the base build; 24gb and up take the speed.
        out = self._gen("24gb")
        chat = next(ln for ln in out.splitlines() if "qwen3.8-27b" in ln)
        self.assertIn("--spec-type draft-mtp --spec-draft-n-max 2", chat)
        self.assertNotIn("-md ", chat)
        self.assertNotIn("--spec-type", self._gen("16gb"))

    def test_every_model_pins_its_context(self):
        # llama-server with no -c reserves the model's FULL trained window: measured 4.8 GB for the 0.6B
        # embedder and 5.7 GB for the 0.6B reranker, versus 1.65 GB each at -c 4096.
        import bob_models
        for profile in bob_models.load_models_config()["profiles"]:
            for role, spec in bob_models.profile_roles(profile).items():
                self.assertIsNotNone(spec.get("ctx"), f"{profile}/{role} has no ctx")

    def test_single_slot_by_default(self):
        # llama-server defaults to four slots, each with its own KV/recurrent-state cache.
        self.assertIn("-np 1", self._gen("16gb").split("models:")[0])

    def test_per_model_kv_quant_overrides_the_macro(self):
        # A long-context model held entirely in VRAM can only afford q4_0; the profile macro stays q8_0
        # for everything else.
        out = self._gen("16gb")
        chat = next(ln for ln in out.splitlines() if "qwen3.8-27b" in ln)
        self.assertIn("--cache-type-k q4_0 --cache-type-v q4_0", chat)
        self.assertNotIn("${kv}", chat)
        out24 = self._gen("24gb")
        self.assertIn("${kv}", next(ln for ln in out24.splitlines() if "qwen3.8-27b" in ln))

    def test_parallel_slots_only_on_the_big_tier(self):
        # --parallel 2 --no-kv-unified gives two agent sessions private KV slots instead of one shared pool.
        chat32 = next(ln for ln in self._gen("32gb").splitlines() if "qwen3.8-27b" in ln)
        self.assertIn("--parallel 2 --no-kv-unified", chat32)
        self.assertNotIn("--no-kv-unified", self._gen("16gb"))

    def test_coder_moe_offload_per_profile(self):
        # The 12gb tier keeps Qwen3-Coder-30B-A3B (MoE) with its experts spilled to RAM; 8gb stays dense.
        def coder_line(profile):
            out = self._gen(profile)
            return next(ln for ln in out.splitlines()
                        if "qwen3-coder-30b-a3b" in ln or "qwen-coder-7b" in ln)
        self.assertIn("--n-cpu-moe 34", coder_line("12gb"))
        self.assertIn("qwen-coder-7b", coder_line("8gb"))       # small dense coder, no offload
        self.assertNotIn("--n-cpu-moe", coder_line("8gb"))

    def test_moe_offload_emitted_for_overflow_model(self):
        out = self._gen("12gb")
        ponder = next(ln for ln in out.splitlines() if "qwen3.6-35b-a3b" in ln)
        self.assertIn("--n-cpu-moe 34", ponder)   # 35B MoE spills experts to RAM so it fits 12GB
        chat = next(ln for ln in out.splitlines() if "qwen3.5-9b" in ln)
        self.assertNotIn("--n-cpu-moe", chat)     # dense model that fits: no offload

    def test_no_profile_still_spills_a_dense_model(self):
        # The 1.4 refresh put every GPU tier on a model that fits the card outright: no ngl="auto"
        # (dense-overflow) model is left in the registry.
        import bob_models
        for profile in bob_models.load_models_config()["profiles"]:
            for role, spec in bob_models.profile_roles(profile).items():
                self.assertNotEqual(str(spec.get("ngl", "")).lower(), "auto", f"{profile}/{role}")

    def test_auto_ngl_omits_the_flag_so_llama_cpp_fits_it(self):
        # A DENSE model bigger than the card can't use --n-cpu-moe, and any explicit -ngl aborts
        # llama.cpp's fit-to-free-VRAM. ngl="auto" must therefore emit NO -ngl at all, while keeping
        # flash-attn and the reasoning format the macro would have supplied. No shipped profile needs
        # it any more, so it is exercised against a patched registry.
        import copy
        import unittest.mock as m
        import bob_models
        mcfg = copy.deepcopy(bob_models.load_models_config())
        mcfg["profiles"]["16gb"]["chat"]["ngl"] = "auto"
        with m.patch.object(bob_models, "load_models_config", return_value=mcfg):
            out = self._gen("16gb")
        chat = next(ln for ln in out.splitlines() if "qwen3.8-27b" in ln)
        self.assertNotIn("-ngl", chat)
        self.assertNotIn("${srv}", chat)          # expanded inline, not via the macro
        self.assertIn("--flash-attn on", chat)
        self.assertIn("--reasoning-format deepseek", chat)
        # every other model still rides the macro (which carries -ngl 99)
        self.assertIn("-ngl 99", out.split("models:")[0])
        self.assertIn("${srv}", next(ln for ln in out.splitlines() if "qwen3-vl-8b" in ln))

    def test_auto_ngl_ignored_on_the_cpu_tier(self):
        # The CPU tier pins -ngl 0; "auto" there would hand llama.cpp a GPU it doesn't have.
        out = self._gen("cpu")
        self.assertIn("-ngl 0", out)
        for ln in out.splitlines():
            if ".gguf" in ln:
                self.assertIn("${srv}", ln)

    def test_vision_keeps_flash_attn_with_mmproj(self):
        # mtmd auto-detects flash-attn support per backend and falls back on its own (clip.cpp), so a
        # vision model rides the same srv macro as everything else — including --reasoning-format.
        out = self._gen("16gb")
        vision_line = next(ln for ln in out.splitlines() if "qwen3-vl" in ln)
        self.assertIn("${srv}", vision_line)
        self.assertIn("--mmproj ${env.LLAMA_LOCAL_ROOT}/models/mmproj-Qwen3VL-8B-Instruct-F16.gguf", vision_line)

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
        self.assertIn("      model: deepseek/deepseek-v4-flash", out)
        self.assertIn("      api_key: os.environ/DEEPSEEK_API_KEY", out)

    def test_pro_output_cap_is_the_peer_limit_with_role_overrides(self):
        """Every client that sends no max_tokens gets maxOutputTokens: long enough for a large tool
        call, and a role can raise it (ponder spends its thinking against the same cap)."""
        out = self._gen()
        self.assertIn("  - model_name: coder-pro\n    litellm_params:\n      model: deepseek/deepseek-v4-flash\n"
                      "      api_base: https://api.deepseek.com\n      api_key: os.environ/DEEPSEEK_API_KEY\n"
                      "      max_tokens: 32768\n", out)
        self.assertIn("      model: deepseek/deepseek-v4-pro\n      api_base: https://api.deepseek.com\n"
                      "      api_key: os.environ/DEEPSEEK_API_KEY\n      max_tokens: 65536\n", out)
        for short in (2048, 4096, 8192):
            self.assertNotIn(f"max_tokens: {short}\n", out)

    def test_settings(self):
        out = self._gen()
        self.assertIn("  num_retries: 3", out)
        self.assertIn("  request_timeout: 600", out)
        self.assertIn("  master_key: os.environ/LITELLM_MASTER_KEY\n", out)
        self.assertNotIn(_OLD_FIXED_KEY, out)
        self.assertNotIn("$true", out)

    def test_the_header_warns_a_hand_run_without_the_key_is_unauthenticated(self):
        out = self._gen()
        header = out.split("model_list:")[0]
        self.assertIn("LITELLM_MASTER_KEY", header)
        self.assertIn("UNAUTHENTICATED", header)
        self.assertTrue(all(ln.startswith("#") for ln in header.strip().splitlines()))
        import yaml
        self.assertEqual(yaml.safe_load(out)["general_settings"]["master_key"], "os.environ/LITELLM_MASTER_KEY")

    def test_the_master_key_is_never_written(self):
        """The proxy reads its key from LITELLM_MASTER_KEY, so the file (readable by anything that can read
        the repo) carries no key at all; llama-swap takes none either."""
        out = self._gen()
        key = bob_core._litellm_key(CFG)
        self.assertEqual(out.count(key), 0)
        self.assertIn("  - model_name: chat\n    litellm_params:\n      model: openai/chat\n"
                      "      api_base: http://127.0.0.1:8080/v1\n      api_key: none\n", out)

    def test_langfuse_reads_the_top_level_switch_and_env_references(self):
        with mock.patch.object(gen, "_cfg", dict(CFG, langfuseEnabled=True)):
            out = self._gen()
        self.assertIn('  success_callback: ["langfuse"]', out)
        self.assertIn("  LANGFUSE_PUBLIC_KEY: os.environ/LANGFUSE_PUBLIC_KEY", out)
        self.assertIn("  LANGFUSE_SECRET_KEY: os.environ/LANGFUSE_SECRET_KEY", out)
        self.assertIn("  LANGFUSE_HOST: os.environ/LANGFUSE_HOST", out)
        self.assertNotIn("pk-lf-", out)
        off = self._gen()
        self.assertNotIn("success_callback", off)
        self.assertNotIn("environment_variables", off)

    def test_no_dead_registry_defaults(self):
        """The runtime reads these from config/defaults.json; a copy in models.json defaults is dead."""
        import bob_models
        d = bob_models.load_models_config()["defaults"]
        for dead in ("webuiSecret", "port", "langfusePort", "n8nTimezone", "langfuseEnabled"):
            self.assertNotIn(dead, d)

    def test_glm_and_kimi_peers_when_enabled(self):
        # 1.2 cloud-peer refresh: GLM-5.2 (z.ai) and Kimi K2.7 Code (moonshot) ship enabled:false
        # (opt-in, one coding peer at a time). Force-enable a copy of the registry and assert the wiring.
        import copy
        import unittest.mock as m
        import bob_models
        mcfg = copy.deepcopy(bob_models.load_models_config())
        mcfg["peers"]["zhipu"]["enabled"] = True
        mcfg["peers"]["kimi"]["enabled"] = True
        with m.patch.object(bob_models, "load_models_config", return_value=mcfg):
            out = self._gen()
        self.assertIn("      model: openai/glm-5.3", out)
        self.assertIn("      api_base: https://api.z.ai/api/paas/v4", out)
        self.assertIn("      api_key: os.environ/ZHIPU_API_KEY", out)
        self.assertIn("      model: openai/kimi-k3", out)
        self.assertIn("      api_base: https://api.moonshot.ai/v1", out)
        self.assertIn("      api_key: os.environ/MOONSHOT_API_KEY", out)


class TestContinue(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(gen, "_have_npx", return_value=True)   # npx-backed servers present
        p.start()
        self.addCleanup(p.stop)

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
        self.assertNotIn("searxng", out)   # SearXNG is opt-in: no MCP server for a service that is off
        agent = dict(CFG.get("agent", {}), searchProvider="searxng")
        with mock.patch.object(gen, "_cfg", dict(CFG, agent=agent)):
            self.assertIn('SEARXNG_URL: "http://localhost:', self._gen())

    def test_bob_mcp_server_when_enabled(self):
        self.assertNotIn("  - name: bob\n", self._gen())
        agent = dict(CFG.get("agent", {}), mcpEnabled=True)
        with mock.patch.object(gen, "_cfg", dict(CFG, agent=agent)):
            out = self._gen()
        self.assertIn('  - name: bob\n    command: ', out)
        self.assertIn('    args:\n      - "agent"\n      - "mcp"', out)

    def test_npx_servers_dropped_without_node(self):
        agent = dict(CFG.get("agent", {}), searchProvider="searxng")
        with mock.patch.object(gen, "_have_npx", return_value=False), \
             mock.patch.object(gen, "_cfg", dict(CFG, agent=agent)):
            msg = gen.gen_continue()
        out = (gen.REPO / "config" / "continue" / "config.yaml").read_text(encoding="utf-8")
        self.assertNotIn("command: npx", out)
        self.assertIn("  - name: fetch\n    command: uvx", out)      # uvx server does not need Node
        self.assertIn("notice: npx not found", msg)
        for name in ("filesystem", "github", "searxng-search"):
            self.assertIn(name, msg)
        self.assertNotIn("notice", gen.gen_continue())                # npx back (setUp) -> no notice

    def test_context_length_is_the_per_slot_window(self):
        gen.gen_continue("32gb")
        out = (gen.REPO / "config" / "continue" / "config.yaml").read_text(encoding="utf-8")
        self.assertIn("contextLength: 196608", out)
        self.assertNotIn("393216", out)

    def test_pro_roles_inherit_the_role_prompt(self):
        import bob_models
        prompts = bob_models.load_models_config()["prompts"]
        out = self._gen()
        self.assertIn("  - name: coder-pro\n", out)
        block = out.split("  - name: coder-pro\n")[1].split("\n\n")[0]
        self.assertIn(f"systemMessage: {gen._yaml_str(prompts['coder'])}", block)
        self.assertIn("ponder", prompts)


class TestDsh(unittest.TestCase):
    """The DeepSeek Harness drop-ins: a pi-ai provider route for Bob's LiteLLM proxy, and Bob's MCP
    server as a dsh plugin instance. install_dsh is exercised against a temp $DSH_HOME."""

    def _gen(self, profile=None):
        gen.gen_dsh(profile)
        return (gen.REPO / "config" / "dsh" / "settings.yaml").read_text(encoding="utf-8")

    def _install(self, home, mcp=True):
        import unittest.mock as m
        cfg = dict(CFG)
        cfg["agent"] = dict(cfg.get("agent", {}), mcpEnabled=mcp)
        with m.patch.dict("os.environ", {"DSH_HOME": str(home)}), m.patch.object(gen, "_cfg", cfg):
            gen.gen_dsh()
            return gen.install_dsh()

    def test_route_points_at_the_litellm_proxy(self):
        out = self._gen("16gb")
        self.assertIn("    bob:\n", out)
        self.assertIn("      api: openai-completions", out)
        self.assertIn(f"      baseURL: http://localhost:{bob_core._port(CFG, 'litellmPort')}/v1", out)
        self.assertIn("      apiKeyEnv: BOB_LITELLM_KEY", out)

    def test_compat_switches_for_a_llama_cpp_gateway(self):
        # pi-ai addresses an unrecognized endpoint as OpenAI itself: the developer role and
        # max_completion_tokens would both be refused by llama-server.
        out = self._gen("16gb")
        self.assertIn("        supportsDeveloperRole: false", out)
        self.assertIn("        maxTokensField: max_tokens", out)

    def test_models_skip_non_chat_roles(self):
        out = self._gen("16gb")
        self.assertIn("        - id: coder\n          contextWindow: 40960", out)
        for skipped in ("agent", "fim", "embed", "rerank"):
            self.assertNotIn(f"        - id: {skipped}\n", out)

    def test_local_vision_is_marked_image_capable(self):
        out = self._gen("24gb")
        self.assertIn("        - id: vision\n          contextWindow: 98304\n"
                      "          input: [text, image]", out)

    def test_a_window_too_small_for_an_agent_is_left_out(self):
        """pi-ai caps output at window - prompt - 4096, so a 4096-token model would answer in one
        token; the route must not offer it."""
        import bob_models
        self.assertNotIn("        - id: vision\n", self._gen("16gb"))   # 16gb vision runs at 4096
        entries, skipped = gen._dsh_models(bob_models.load_models_config(), "16gb")
        self.assertIn("vision (4096 ctx < 16384)", skipped)
        self.assertNotIn("vision", [e[0] for e in entries])

    def test_split_slots_advertise_the_per_request_window(self):
        """--parallel 2 --no-kv-unified gives each request half of -c; advertising all of it lets
        dsh overrun the slot before it ever compacts."""
        out = self._gen("32gb")
        self.assertIn("        - id: chat\n          contextWindow: 196608", out)
        self.assertNotIn("393216", out)

    def test_slot_ctx(self):
        d = {"parallel": 1}
        self.assertEqual(gen._slot_ctx({"ctx": 8192}, d), 8192)
        self.assertEqual(gen._slot_ctx({"ctx": 8192, "flags": ["--parallel", "4"]}, d), 2048)
        self.assertEqual(gen._slot_ctx({"ctx": 8192, "flags": ["-np", "2", "--kv-unified"]}, d), 8192)
        self.assertEqual(gen._slot_ctx({"ctx": 8192}, {"parallel": 2}), 4096)

    def test_pro_peers_carry_real_capacities_not_the_chat_cap(self):
        out = self._gen("16gb")
        self.assertIn("        - id: coder-pro\n          contextWindow: 1000000\n"
                      "          maxTokens: 32768", out)
        self.assertIn("        - id: ponder-pro\n          contextWindow: 1000000\n"
                      "          maxTokens: 65536", out)       # the role override wins
        self.assertNotIn("maxTokens: 4096", out)

    def test_a_vision_pro_role_that_takes_no_images_is_left_out(self):
        import unittest.mock as m
        self.assertNotIn("vision-pro", self._gen("16gb"))   # deepseek-v4-flash takes no images
        mcfg = {"defaults": {}, "peers": {"p": {"pro": {"vision": {"model": "m", "supportsVision": True},
                                                        "chat": {"model": "m"}}}}}
        with m.patch.object(gen, "_ordered_models", return_value=("x", [])):
            entries, _ = gen._dsh_models(mcfg)
        self.assertEqual(entries, [("chat-pro", 0, 0, False), ("vision-pro", 0, 0, True)])

    def test_install_removes_a_route_with_no_models(self):
        """pi-ai refuses a route resolving no models, so a profile with nothing that fits must drop a
        stale route rather than write an empty one."""
        import unittest.mock as m
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            self._install(home)
            self.assertIn("bob:", (home / "settings.yaml").read_text(encoding="utf-8"))
            with m.patch.object(gen, "_dsh_models", return_value=([], [])):
                msg = self._install(home)
            self.assertIn("removed the 'bob' route", msg)
            self.assertNotIn("bob:", (home / "settings.yaml").read_text(encoding="utf-8"))

    def test_every_profile_generates(self):
        import bob_models
        for profile in bob_models.load_models_config()["profiles"]:
            if profile.startswith("_"):
                continue
            self.assertIn("    bob:", self._gen(profile), profile)

    def test_mcp_patch_runs_bob_agent_mcp_in_the_harness_cwd(self):
        gen.gen_dsh()
        patch = (gen.REPO / "config" / "dsh" / "cordis.patch.yml").read_text(encoding="utf-8")
        self.assertIn("      name: '@deepseek-ai/dsh-mcp-client'", patch)
        self.assertIn("        args: [agent, mcp]", patch)
        self.assertIn("        cwd: !!js process.cwd()", patch)   # the project dsh is open in

    def test_mcp_patch_switches_to_http_when_configured(self):
        """agent.mcpTransport = http makes the generated plugin instance dial a RUNNING Bob instead of
        spawning one, which is what lets the harness sit on another machine."""
        lines = "\n".join(gen._dsh_mcp_lines(
            {"agent": {"mcpTransport": "http", "mcpPort": 8085, "mcpHost": "127.0.0.1"}}))
        self.assertIn("        transport: http", lines)
        self.assertIn('        url: "http://127.0.0.1:8085/mcp"', lines)
        self.assertIn("Authorization:", lines)
        self.assertNotIn("args: [agent, mcp]", lines)           # nothing is spawned over HTTP

    def test_mcp_http_header_reads_the_env_with_no_fixed_fallback(self):
        lines = "\n".join(gen._dsh_mcp_lines({"agent": {"mcpTransport": "http"}}))
        self.assertIn("process.env.BOB_LITELLM_KEY ?? ''", lines)
        self.assertNotIn(_OLD_FIXED_KEY, lines)
        self.assertNotIn(bob_core._litellm_key(CFG), lines)     # the key is never written into the patch

    def test_install_replaces_the_entry_when_the_transport_changes(self):
        """Append-once would leave a stdio entry in place after agent.mcpTransport switched to http."""
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / "cordis.patch.yml").write_text(
                "# mine\n- insert:\n    - id: user-thing\n      config:\n        cwd: !!js process.cwd()\n",
                encoding="utf-8")
            self._install(home)
            http = dict(CFG, agent=dict(CFG.get("agent", {}), mcpEnabled=True, mcpTransport="http"))
            with mock.patch.dict("os.environ", {"DSH_HOME": str(home)}), mock.patch.object(gen, "_cfg", http):
                gen.gen_dsh()
                self.assertIn("replaced the 'bob-tools' entry", gen.install_dsh())
                self.assertIn("already carries", gen.install_dsh())
            text = (home / "cordis.patch.yml").read_text(encoding="utf-8")
            self.assertEqual(text.count("id: bob-tools"), 1)
            self.assertIn("transport: http", text)
            self.assertNotIn("transport: stdio", text)
            self.assertTrue(text.startswith("# mine\n- insert:\n    - id: user-thing\n"))
            self.assertIn("cwd: !!js process.cwd()", text)       # the user's tag survives
            self._install(home)                                    # and back to stdio
            text = (home / "cordis.patch.yml").read_text(encoding="utf-8")
            self.assertIn("transport: stdio", text)
            self.assertEqual(text.count("id: bob-tools"), 1)
            self.assertIn("id: user-thing", text)

    def test_mcp_http_url_is_dialable_not_the_bind_address(self):
        """0.0.0.0 is a bind address; the generated url must be something a client can actually open,
        and agent.mcpUrl is how a remote harness is given the real one."""
        wild = "\n".join(gen._dsh_mcp_lines({"agent": {"mcpTransport": "http", "mcpHost": "0.0.0.0"}}))
        self.assertIn("127.0.0.1", wild)
        self.assertNotIn("0.0.0.0", wild)
        named = "\n".join(gen._dsh_mcp_lines(
            {"agent": {"mcpTransport": "http", "mcpUrl": "https://bob.example/mcp"}}))
        self.assertIn('        url: "https://bob.example/mcp"', named)

    def test_install_skips_when_dsh_is_absent(self):
        with tempfile.TemporaryDirectory() as d:
            msg = self._install(Path(d) / "nope")
            self.assertIn("no DeepSeek Harness home", msg)

    def test_install_merges_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / "settings.yaml").write_text(
                "llm-deepseek:\n  reasoningEffort: max\n"
                "llm-pi-ai:\n  providers:\n    anthropic:\n      apiKeyEnv: ANTHROPIC_API_KEY\n",
                encoding="utf-8")
            self.assertIn("merged the 'bob' route", self._install(home))
            text = (home / "settings.yaml").read_text(encoding="utf-8")
            self.assertIn("anthropic:", text)        # the user's other provider survives
            self.assertIn("reasoningEffort: max", text)   # and their other sections
            self.assertIn("bob:", text)
            self.assertIn("already current", self._install(home))

    def test_install_appends_the_mcp_entry_once_keeping_js_tags(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / "cordis.patch.yml").write_text(
                "- insert:\n    - id: user-thing\n      name: whatever\n"
                "      config:\n        cwd: !!js process.cwd()\n", encoding="utf-8")
            self.assertIn("appended the 'bob-tools' entry", self._install(home))
            text = (home / "cordis.patch.yml").read_text(encoding="utf-8")
            self.assertIn("id: user-thing", text)
            self.assertEqual(text.count("id: bob-tools"), 1)
            self.assertIn("already carries", self._install(home))
            self.assertEqual(
                (home / "cordis.patch.yml").read_text(encoding="utf-8").count("id: bob-tools"), 1)

    def test_install_stores_the_key_owner_only_on_a_fresh_home(self):
        """A new user has nothing exported: the route only authenticates because install_dsh puts the
        key in dsh's credential store, which dsh refuses to load if other users can read it."""
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            self.assertIn("stored BOB_LITELLM_KEY", self._install(home))
            cred = home / ".credentials.yaml"
            key = bob_core._litellm_key(CFG)
            self.assertEqual(cred.read_text(encoding="utf-8"),
                             f'version: 1\n\nrefs:\n  BOB_LITELLM_KEY: "{key}"\n')
            if os.name == "posix":
                self.assertEqual(cred.stat().st_mode & 0o777, 0o600)
            self.assertIn("already carries", self._install(home))

    def test_install_adds_the_key_beside_the_users_credentials(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            cred = home / ".credentials.yaml"
            cred.write_text("version: 1\n\n# my deepseek key\nrefs:\n    DEEPSEEK_API_KEY: sk-mine\n\n"
                            "records:\n  llm-pi-ai/amazon-bedrock:\n    kind: api-key\n",
                            encoding="utf-8")
            self._install(home)
            text = cred.read_text(encoding="utf-8")
            self.assertIn("# my deepseek key\nrefs:\n    BOB_LITELLM_KEY: ", text)   # child indent kept
            self.assertIn("    DEEPSEEK_API_KEY: sk-mine\n", text)
            self.assertIn("records:\n  llm-pi-ai/amazon-bedrock:\n    kind: api-key\n", text)
            self.assertEqual(text.count("BOB_LITELLM_KEY"), 1)

    def test_install_rewrites_a_stale_key_and_appends_missing_refs(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            cred = home / ".credentials.yaml"
            cred.write_text("version: 1\nrefs:\n  BOB_LITELLM_KEY: old\n  X_KEY: x\n", encoding="utf-8")
            self.assertIn("updated BOB_LITELLM_KEY", self._install(home))
            text = cred.read_text(encoding="utf-8")
            self.assertNotIn(": old", text)
            self.assertIn("  X_KEY: x\n", text)
            cred.write_text("version: 1\n", encoding="utf-8")
            self._install(home)
            self.assertIn("refs:\n  BOB_LITELLM_KEY: ", cred.read_text(encoding="utf-8"))

    def test_install_leaves_mcp_alone_when_disabled(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            msg = self._install(home, mcp=False)
            self.assertIn("set agent.mcpEnabled", msg)
            self.assertFalse((home / "cordis.patch.yml").exists())


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

    def test_update_keeps_the_users_fields(self):
        """Bob owns only params.system: a user's name, meta, other params and active flag survive."""
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "webui.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE user (id TEXT, role TEXT)")
            conn.execute("INSERT INTO user (id, role) VALUES ('u1', 'admin')")
            conn.execute("""CREATE TABLE model (id TEXT PRIMARY KEY, user_id TEXT, base_model_id TEXT,
                            name TEXT, params TEXT, meta TEXT, updated_at INTEGER, created_at INTEGER,
                            is_active INTEGER)""")
            conn.execute("INSERT INTO model VALUES ('chat','u9','chat','My Chat',?,?,1,1,0)",
                         (json.dumps({"system": "old", "temperature": 0.2}), json.dumps({"tags": ["x"]})))
            conn.commit()
            conn.close()
            gen._webui_write(str(db), [{"id": "chat", "prompt": "Be concise."}])
            conn = sqlite3.connect(db)
            row = conn.execute("SELECT user_id, name, params, meta, created_at, is_active FROM model "
                               "WHERE id='chat'").fetchone()
            conn.close()
            self.assertEqual(row[0], "u9")
            self.assertEqual(row[1], "My Chat")
            self.assertEqual(json.loads(row[2]), {"system": "Be concise.", "temperature": 0.2})
            self.assertEqual(json.loads(row[3]), {"tags": ["x"]})
            self.assertEqual((row[4], row[5]), (1, 0))
            gen._webui_write(str(db), [{"id": "chat", "prompt": ""}])
            conn = sqlite3.connect(db)
            params = conn.execute("SELECT params FROM model WHERE id='chat'").fetchone()[0]
            conn.close()
            self.assertEqual(json.loads(params), {"temperature": 0.2})


class TestRegistryConsistency(unittest.TestCase):
    """Registry-level invariants the generators rely on."""

    def _profiles(self):
        import bob_models
        mcfg = bob_models.load_models_config()
        return mcfg, {p: bob_models.profile_roles(p, mcfg) for p in mcfg["profiles"] if not p.startswith("_")}

    def test_no_sampling_in_flags(self):
        _, profiles = self._profiles()
        for p, roles in profiles.items():
            for role, spec in roles.items():
                for f in spec.get("flags") or []:
                    self.assertNotIn(f, gen._SAMPLING_FLAGS, f"{p}/{role} sets {f} in flags")

    def test_sampling_flag_lint_warns(self):
        warn = gen.sampling_flag_warnings([{"role": "chat", "flags": ["--jinja", "--temp", "0.7"]},
                                           {"role": "writer", "_aliasOf": "chat", "flags": ["--temp", "0.7"]}])
        self.assertEqual(len(warn), 1)
        self.assertIn("[chat] sampling flag --temp", warn[0])
        import copy
        import contextlib
        import io
        import bob_models
        mcfg = copy.deepcopy(bob_models.load_models_config())
        mcfg["profiles"]["16gb"]["fim"]["flags"] = ["--top-k", "40"]
        err = io.StringIO()
        with mock.patch.object(bob_models, "load_models_config", return_value=mcfg), \
                contextlib.redirect_stderr(err):
            gen.gen_llama_swap("16gb")
        self.assertIn("[fim] sampling flag --top-k", err.getvalue())

    def test_role_sampling_is_the_same_on_every_tier(self):
        # chat differs by model family, which each tier's _notes states; every other role matches.
        _, profiles = self._profiles()
        for role in ("coder", "ponder", "writer", "agent"):
            seen = {p: roles[role].get("setParams") for p, roles in profiles.items() if role in roles}
            self.assertTrue(all(seen.values()), f"{role} has no setParams on some tier: {seen}")
            self.assertEqual(len({json.dumps(v, sort_keys=True) for v in seen.values()}), 1, f"{role}: {seen}")

    def test_rerank_ubatch_covers_its_context(self):
        """A rank-pooling reranker rejects any input longer than its ubatch."""
        _, profiles = self._profiles()
        for p, roles in profiles.items():
            spec = roles.get("rerank")
            if not spec:
                continue
            flags = spec["flags"]
            ub = int(flags[flags.index("-ub") + 1])
            self.assertGreaterEqual(ub, spec["ctx"], p)
            self.assertEqual(int(flags[flags.index("-b") + 1]), ub, p)

    def test_cpu_roles_share_one_server(self):
        _, profiles = self._profiles()
        cpu = profiles["cpu"]
        self.assertEqual(cpu["writer"]["_aliasOf"], "chat")
        self.assertEqual(cpu["agent"]["_aliasOf"], "chat")
        out = gen.render_all("cpu")["config/llama-swap.yaml"]
        self.assertEqual(out.count("qwen3.5-0.8b-q8_0.gguf"), 1)
        self.assertIn("    aliases: [writer, agent]", out)

    def test_vision_pro_route_is_image_capable(self):
        """No pro role is offered for vision unless it takes images, and the vision pro route points at a
        model that does."""
        mcfg, profiles = self._profiles()
        vision_pro = [p for p in gen.enabled_peers(mcfg) if "vision" in (p.get("pro") or {})]
        for p in vision_pro:
            rv = p["pro"]["vision"]
            self.assertTrue(rv.get("supportsVision", p.get("supportsVision")), p["name"])
        role = bob_core.load_defaults()["runtime"]["vision"]["visionProRole"]
        if not role.endswith("-pro"):
            self.assertTrue(any(roles.get(role, {}).get("supportsVision") for roles in profiles.values()))

    def test_prompts_one_per_role(self):
        mcfg, _ = self._profiles()
        self.assertIn("ponder", mcfg["prompts"])
        for peer in mcfg["peers"].values():
            for role, rv in (peer.get("pro") or {}).items():
                self.assertNotIn("systemPrompt", rv if isinstance(rv, dict) else {}, f"{role} duplicates")

    def test_stale_vram_text(self):
        mcfg, _ = self._profiles()
        self.assertNotIn("8192/4096", mcfg["profiles"]["12gb"]["_targetVRAM"])


class TestRoutingWarnings(unittest.TestCase):
    def test_cpu_warns_for_each_missing_role(self):
        import bob_models
        warn = "\n".join(gen.routing_warnings(bob_models.load_models_config(), "cpu", CFG))
        self.assertIn("routing.codeRole = 'coder'", warn)
        self.assertIn("falls back to chat", warn)
        self.assertIn("vision.visionRole = 'vision'", warn)
        self.assertIn("vision requests are refused", warn)

    def test_a_full_profile_warns_only_about_unserved_pro_routes(self):
        import bob_models
        for w in gen.routing_warnings(bob_models.load_models_config(), "24gb", CFG):
            self.assertIn("-pro'", w)


class TestAider(unittest.TestCase):
    def _gen(self, profile):
        gen.gen_aider(profile)
        base = gen.REPO / "config" / "aider"
        return ((base / ".aider.conf.yml").read_text(encoding="utf-8"),
                json.loads((base / "model-metadata.json").read_text(encoding="utf-8")))

    def test_architect_pair_from_the_profile(self):
        import yaml
        conf, meta = self._gen("16gb")
        doc = yaml.safe_load(conf)
        self.assertTrue(doc["architect"])
        self.assertEqual((doc["model"], doc["editor-model"]), ("openai/ponder", "openai/coder"))
        self.assertEqual(doc["openai-api-base"], f"http://127.0.0.1:{bob_core._port(CFG, 'litellmPort')}/v1")
        self.assertEqual(doc["openai-api-key"], bob_core._litellm_key(CFG))
        self.assertEqual(meta["openai/ponder"]["max_input_tokens"], 40960)
        self.assertEqual(doc["map-tokens"], gen._aider_map_tokens(40960))
        self.assertTrue(doc["model-metadata-file"].endswith("config/aider/model-metadata.json"))

    def test_cpu_falls_back_to_chat(self):
        import yaml
        doc = yaml.safe_load(self._gen("cpu")[0])
        self.assertFalse(doc["architect"])
        self.assertEqual(doc["model"], "openai/chat")
        self.assertNotIn("editor-model", doc)

    def test_split_slots_use_the_per_request_window(self):
        _, meta = self._gen("32gb")
        self.assertEqual(meta["openai/ponder"]["max_input_tokens"], 196608)

    def test_map_tokens_scale_with_the_window(self):
        self.assertEqual(gen._aider_map_tokens(8192), 512)
        self.assertEqual(gen._aider_map_tokens(16384), 1024)
        self.assertEqual(gen._aider_map_tokens(393216), 4096)


@unittest.skipIf(os.name == "nt", "POSIX file modes")
class TestKeyDrift(unittest.TestCase):
    """A generated config written for another LiteLLM key (the retired fixed key, a key from another data
    dir, a rotated secret) is detected by a text check and regenerated by its own generator."""

    def setUp(self):
        with mock.patch.object(gen, "_have_npx", return_value=True):
            gen.gen_litellm(); gen.gen_continue(); gen.gen_aider("16gb")
        self.key = bob_core._litellm_key(CFG)

    def _rewrite(self, rel, old, new):
        path = gen.REPO / rel
        path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")

    def test_fresh_output_is_current(self):
        self.assertEqual(gen.stale_key_files(CFG), [])

    def test_a_foreign_key_in_continue_and_aider_is_stale(self):
        self._rewrite("config/continue/config.yaml", self.key, "sk-bob-b7ea80a4")
        self._rewrite("config/aider/.aider.conf.yml", self.key, _OLD_FIXED_KEY)
        self.assertEqual(sorted(gen.stale_key_files(CFG)),
                         ["config/aider/.aider.conf.yml", "config/continue/config.yaml"])

    def test_a_literal_master_key_is_stale_even_when_it_is_current(self):
        self._rewrite("config/litellm.yaml", "os.environ/LITELLM_MASTER_KEY", self.key)
        self.assertEqual(gen.stale_key_files(CFG), ["config/litellm.yaml"])

    def test_refresh_regenerates_only_the_stale_files(self):
        self._rewrite("config/continue/config.yaml", self.key, _OLD_FIXED_KEY)
        self._rewrite("config/litellm.yaml", "os.environ/LITELLM_MASTER_KEY", _OLD_FIXED_KEY)
        aider = gen.REPO / "config" / "aider" / ".aider.conf.yml"
        before = aider.stat().st_mtime_ns
        lines = gen.refresh_stale_key_files(CFG)
        self.assertEqual(len(lines), 2)
        self.assertEqual(gen.stale_key_files(CFG), [])
        self.assertNotIn(_OLD_FIXED_KEY, (gen.REPO / "config" / "continue" / "config.yaml").read_text())
        self.assertEqual(aider.stat().st_mtime_ns, before)
        self.assertEqual(gen.refresh_stale_key_files(CFG), [])

    def test_missing_files_are_not_stale(self):
        with mock.patch.object(gen, "REPO", Path(tempfile.mkdtemp(prefix="bob-nokeys-"))):
            self.assertEqual(gen.stale_key_files(CFG), [])


class TestDshCredentialRefresh(unittest.TestCase):
    """stack's per-start key sync refreshes dsh's credential store, line-edited, only when it exists."""

    def _home(self):
        home = Path(tempfile.mkdtemp(prefix="bob-dsh-"))
        self.addCleanup(shutil.rmtree, home, True)
        return home

    def _refresh(self, home):
        with mock.patch.dict(os.environ, {"DSH_HOME": str(home)}):
            return gen.refresh_dsh_credential(CFG)

    def test_a_stale_stored_key_is_updated_in_place(self):
        home = self._home()
        cred = home / ".credentials.yaml"
        cred.write_text("version: 1\n\n# keep me\nrefs:\n  OTHER: 'x'\n  BOB_LITELLM_KEY: 'sk-old'\n",
                        encoding="utf-8")
        line = self._refresh(home)
        self.assertIn("updated BOB_LITELLM_KEY", line)
        text = cred.read_text(encoding="utf-8")
        self.assertIn(bob_core._litellm_key(CFG), text)
        self.assertIn("# keep me", text)
        self.assertIn("  OTHER: 'x'", text)
        self.assertNotIn("sk-old", text)
        self.assertEqual(self._refresh(home), "")          # current: nothing to report

    def test_nothing_is_created_without_dsh_or_its_store(self):
        self.assertEqual(self._refresh(self._home() / "absent"), "")
        home = self._home()
        self.assertEqual(self._refresh(home), "")
        self.assertFalse((home / ".credentials.yaml").exists())


class TestWebuiKeySync(unittest.TestCase):
    """Open WebUI's persistent config wins over its environment, so the stored key of each connection to
    Bob's LiteLLM is rewritten; a connection the user added to another server keeps its key."""

    def _db(self, rows):
        d = tempfile.mkdtemp(prefix="bob-webui-")
        db = Path(d) / "webui.db"
        conn = sqlite3.connect(db)
        conn.execute('CREATE TABLE config ("key" TEXT NOT NULL, value JSON NOT NULL, updated_at BIGINT, '
                     'PRIMARY KEY ("key"))')
        for k, v in rows.items():
            conn.execute("INSERT INTO config VALUES (?,?,1)", (k, json.dumps(v)))
        conn.commit()
        conn.close()
        return db

    def _rows(self, db):
        conn = sqlite3.connect(db)
        out = {k: json.loads(v) for k, v in conn.execute("SELECT key, value FROM config")}
        conn.close()
        return out

    def test_rewrites_bobs_connections_and_keeps_others(self):
        key = bob_core._litellm_key(CFG)
        db = self._db({"openai.api_base_urls": ["http://localhost:8081/v1", "https://api.example.com/v1",
                                                "http://127.0.0.1:8081/v1/"],
                       "openai.api_keys": [_OLD_FIXED_KEY, "sk-user-own"],
                       "rag.openai.api_base_url": "http://localhost:8081/v1",
                       "rag.openai.api_key": "sk-bob-stale"})
        msg = gen.webui_sync_key(db, CFG)
        self.assertIn("updated the stored LiteLLM key", msg)
        rows = self._rows(db)
        self.assertEqual(rows["openai.api_keys"], [key, "sk-user-own", key])
        self.assertEqual(rows["rag.openai.api_key"], key)
        self.assertEqual(gen.webui_sync_key(db, CFG), "")   # idempotent

    def test_a_running_webui_is_told_to_restart(self):
        rows = {"rag.openai.api_base_url": "http://localhost:8081/v1", "rag.openai.api_key": "sk-bob-stale"}
        with mock.patch.object(osenv, "is_port_in_use", return_value=True):
            msg = gen.webui_sync_key(self._db(rows), CFG)
        self.assertIn("keeps the old key until it restarts", msg)
        with mock.patch.object(osenv, "is_port_in_use", return_value=False):
            msg = gen.webui_sync_key(self._db(rows), CFG)
        self.assertIn("updated the stored LiteLLM key", msg)
        self.assertNotIn("restart", msg)

    def test_a_non_bob_embedding_connection_is_left_alone(self):
        db = self._db({"rag.openai.api_base_url": "https://api.openai.com/v1", "rag.openai.api_key": "sk-mine"})
        self.assertEqual(gen.webui_sync_key(db, CFG), "")
        self.assertEqual(self._rows(db)["rag.openai.api_key"], "sk-mine")

    def test_absent_db_and_old_schema_are_skipped(self):
        self.assertEqual(gen.webui_sync_key(Path(tempfile.mkdtemp()) / "webui.db", CFG), "")
        d = Path(tempfile.mkdtemp()) / "webui.db"
        sqlite3.connect(d).close()
        self.assertEqual(gen.webui_sync_key(d, CFG), "")

    def test_a_locked_db_is_skipped_with_a_warning(self):
        db = self._db({"rag.openai.api_base_url": "http://localhost:8081/v1", "rag.openai.api_key": "old"})
        holder = sqlite3.connect(db)
        holder.execute("BEGIN EXCLUSIVE")
        try:
            with mock.patch.object(sqlite3, "connect",
                                   side_effect=lambda p, timeout=5: sqlite3.Connection(p, timeout=0.1)):
                msg = gen.webui_sync_key(db, CFG)
        finally:
            holder.rollback()
            holder.close()
        self.assertIn("locked", msg)


class TestFabricKeyRefresh(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bob-fabric-"))
        self.env = self.dir / ".env"
        self.patch = mock.patch.object(osenv, "home_config_dir", return_value=self.dir)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_bobs_route_with_an_old_key_is_re_merged(self):
        self.env.write_text("LITELLM_API_KEY=sk-local\nLITELLM_API_BASE_URL=http://localhost:8081/v1\n"
                            "OPENAI_API_KEY=sk-user\n", encoding="utf-8")
        self.assertIn("Updated fabric", gen.refresh_fabric_env())
        text = self.env.read_text(encoding="utf-8")
        self.assertIn(f"LITELLM_API_KEY={bob_core._litellm_key(CFG)}", text)
        self.assertIn("OPENAI_API_KEY=sk-user", text)
        self.assertEqual(gen.refresh_fabric_env(), "")

    def test_no_env_or_another_route_is_left_alone(self):
        self.assertEqual(gen.refresh_fabric_env(), "")
        self.env.write_text("LITELLM_API_KEY=x\nLITELLM_API_BASE_URL=https://litellm.example/v1\n",
                            encoding="utf-8")
        self.assertEqual(gen.refresh_fabric_env(), "")
        self.assertIn("LITELLM_API_KEY=x", self.env.read_text(encoding="utf-8"))


class TestFabricLegacyMigration(unittest.TestCase):
    """The fabric .env resolves through the real osenv.home_config_dir seam against a temp HOME."""

    def test_the_legacy_openai_pair_is_migrated_under_a_temp_home(self):
        # The .env an earlier fabric-setup wrote: no LITELLM_* keys, only OPENAI_* at Bob's proxy.
        port = bob_core._port(CFG, "litellmPort")
        home = Path(tempfile.mkdtemp(prefix="bob-fabric-home-"))
        self.addCleanup(shutil.rmtree, home, True)
        env = home / ".config" / "fabric" / ".env"
        env.parent.mkdir(parents=True)
        env.write_text(f"OPENAI_API_KEY=sk-local\nOPENAI_API_BASE_URL=http://localhost:{port}/v1\n"
                       "DEFAULT_VENDOR=OpenAI\nDEFAULT_MODEL=chat\n", encoding="utf-8")
        env_vars = {k: v for k, v in os.environ.items() if k != "XDG_CONFIG_HOME"}
        env_vars.update(HOME=str(home), USERPROFILE=str(home))
        with mock.patch.dict(os.environ, env_vars, clear=True):
            self.assertEqual(osenv.home_config_dir("fabric"), env.parent)
            self.assertIn("Migrated fabric", gen.refresh_fabric_env())
            text = env.read_text(encoding="utf-8")
            self.assertIn(f"LITELLM_API_KEY={bob_core._litellm_key(CFG)}", text)
            self.assertIn(f"LITELLM_API_BASE_URL=http://localhost:{port}/v1", text)
            self.assertIn("DEFAULT_VENDOR=LiteLLM", text)
            self.assertNotIn("OPENAI_API_KEY", text)
            self.assertEqual(gen.refresh_fabric_env(), "")


class TestKeyBearingFilesArePrivate(unittest.TestCase):
    """Every generated file that embeds the LiteLLM key is 0600; key-free llama-swap.yaml keeps the umask."""

    def _mode(self, *parts):
        import stat
        return stat.S_IMODE((gen.REPO / "config" / Path(*parts)).stat().st_mode)

    def test_modes(self):
        with mock.patch.object(gen, "_have_npx", return_value=True):
            gen.gen_llama_swap(); gen.gen_litellm(); gen.gen_continue(); gen.gen_dsh(); gen.gen_aider()
        for parts in (("litellm.yaml",), ("continue", "config.yaml"), ("dsh", "settings.yaml"),
                      ("dsh", "cordis.patch.yml"), ("aider", ".aider.conf.yml"), ("aider", "model-metadata.json")):
            self.assertEqual(self._mode(*parts), 0o600, parts)
        self.assertNotEqual(self._mode("llama-swap.yaml"), 0o600)

    def test_existing_world_readable_file_is_tightened(self):
        dest = gen.REPO / "config" / "litellm.yaml"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("old\n", encoding="utf-8")
        os.chmod(dest, 0o644)
        gen.gen_litellm()
        self.assertEqual(self._mode("litellm.yaml"), 0o600)
        self.assertNotEqual(dest.read_text(encoding="utf-8"), "old\n")


class TestSideEffectFree(unittest.TestCase):
    def test_tool_test_writes_nothing(self):
        """tool_loader --test generate must not write config/, the WebUI db or the real ~/.dsh."""
        import sqlite3 as _sq
        bob_core._litellm_key(CFG)   # the key is generated once on first use; that write is not gen's
        with mock.patch.object(Path, "write_text", side_effect=AssertionError("wrote a file")), \
                mock.patch.object(_sq, "connect", side_effect=AssertionError("opened a db")), \
                mock.patch.object(gen, "install_dsh", side_effect=AssertionError("installed dsh")):
            self.assertIn("rendered", gen.test())

    def test_no_output_carries_the_old_fixed_key(self):
        import bob_models
        for profile in bob_models.load_models_config()["profiles"]:
            for rel, text in gen.render_all(profile).items():
                self.assertNotIn(_OLD_FIXED_KEY, text, f"{profile}: {rel}")

    def test_generate_run_never_touches_the_real_config_dir(self):
        self.assertNotEqual(gen.REPO, Path(bob_core.REPO))


class TestDefaultsKeys(unittest.TestCase):
    def test_runtime_defaults(self):
        rt = bob_core.load_defaults()["runtime"]
        a = rt["agent"]
        self.assertEqual(a["maxContextTokens"], 0)
        self.assertEqual(a["outputReserveTokens"], 1024)
        self.assertEqual(a["mcpAllowTools"], [])
        self.assertIs(a["subAgentAllowPro"], False)
        self.assertIs(a["acceptLitellmKey"], True)
        self.assertEqual(a["maxDuplicateToolCalls"], 2)
        self.assertIs(a["checkpointEdits"], False)
        self.assertEqual(a["checkpointDbPath"], "")
        self.assertEqual(rt["bindHost"], "127.0.0.1")
        self.assertEqual(rt["memory"]["transcriptMaxRows"], 20000)
        self.assertEqual(rt["memory"]["transcriptMaxDays"], 90)
        self.assertNotIn("autoSummarize", rt["memory"])
        self.assertNotIn("sttEngine", rt["voice"])
        self.assertNotIn("ttsEngine", rt["voice"])
        self.assertEqual(rt["litellmKey"], "")   # empty: generated on first use

    def test_user_example_uses_real_top_level_keys_at_their_defaults(self):
        """A verbatim copy of config/user.json.example must change nothing, and name only real keys."""
        import bob_models
        ex = json.loads((Path(bob_core.REPO) / "config" / "user.json.example").read_text(encoding="utf-8"))
        rt = bob_core.load_defaults()["runtime"]
        mcfg = json.loads(bob_models.MODELS_FILE.read_text(encoding="utf-8"))
        self.assertNotIn("bob", ex)

        def check(over, base, where):
            for k, v in over.items():
                if k.startswith("_"):
                    continue
                self.assertIn(k, base, f"{where}.{k} is not a real key")
                if isinstance(v, dict):
                    check(v, base[k], f"{where}.{k}")
                else:
                    self.assertEqual(v, base[k], f"{where}.{k} differs from the shipped default")

        for k, v in ex.items():
            if k.startswith("_"):
                continue
            check({k: v}, rt if k in rt else mcfg, "user.json")


if __name__ == "__main__":
    unittest.main()
