"""Config generators (scripts/tools/generate.py). Emit deterministic, byte-stable output across
every profile incl. the cpu tier.

Hermetic: reads the real config/models.json (the neutral registry) and writes the generated files to
their normal deterministic locations (idempotent — same bytes each run); gen_webui is tested against a
minimal temp sqlite db. No network."""
import os
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
        self.assertIn("  master_key: sk-local", out)

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


if __name__ == "__main__":
    unittest.main()
