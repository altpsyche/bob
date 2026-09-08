# Vendor Changelog

Bob vendors four upstream projects as git submodules under `external/`, pinned in
[`versions.lock`](../versions.lock). This file records what changed in each upstream project when Bob
bumps a pin. For Bob's own release notes, see [CHANGELOG.md](../CHANGELOG.md).

## 2026-09-08: submodule refresh

| Submodule | From | To | Commits |
|---|---|---|---|
| llama.cpp | b9993 | b10853 | 860 |
| llama-swap | v239 | v255 | 64 |
| whisper.cpp | 0ae02cdb (v1.9.1+75) | v1.9.3 | 182 |
| fabric | v1.4.458 | v1.4.478 | 103 |

whisper.cpp lands back on a release tag: v1.9.3 is now ahead of the commit the pin was parked on, so
the "bumping to the newest tag would be a downgrade" caveat from the 1.1 refresh no longer applies.

### llama.cpp (b9993 to b10853)

- **Models:** Qwen3.8-Flash-Next (`qwen4exp`) support, so the 180B-A-class Qwen3.8 MoE is loadable for
  the first time; Kimi-K3 text model plus recurrent-state rollback; Tencent Hy 4 (`hy_v4`) preview;
  BailingMoE3; MiniMax-M3 (MiniMax Sparse Attention) and MiniMaxText01/M1; Nemotron-3-Puzzle-75B-A9B;
  Nanbeige4.2; dots3-note. The `qwen35` / `qwen35moe` architectures behind Qwen3.5/3.6/3.8 were already
  supported at the old pin.
- **Speculative decoding:** MTP for Qwen3-Next, Nemotron, GLM-4.5-Air and GLM-4.7-Flash; NextN/MTP for
  GLM_DSA (GLM-5.2).
- **CUDA:** MoE weighted-expert-reduction fusion; MoE fusion extended to speculative decoding and to
  multi-token GLU/topk-router fusion; `mm_ids_helper` fast path for any `n_expert_used`; branchless
  Q4_K/Q5_K unpack for mmvq; XOR-swizzled flash-attention K/V smem fp16 tiles; sparse flash attention for
  DSV4/GLM; concurrent streams per split on multi-GPU; hardware- and quant-specific mvq-to-MMQ decode
  crossover tuning; races fixed in `mmid` and `mmf`.
- **Server:** `--kv-unified-per-slot` (per-slot context); `data:` URLs accepted for `input_video` and
  `input_audio`; models endpoints made private when authentication is on; a dedup-cache-models preset;
  `/metrics` reachable while sleeping; router `startup_models` lazy-loading; an LRU hang on concurrent
  requests to the same model fixed; prefilled assistant messages carrying tool calls now rejected.
- **Multimodal (mtmd):** `--mmproj-device`; webp via ffmpeg; DeepSeek-V4-Flash-Vision-Exp and dots3-note
  vision+audio; Pillow-accurate resize for all models.
- **Conversion:** `--fuse-qkv` to fuse Q/K/V during HF-to-GGUF conversion; GGUF loader hardened against
  malformed tensor dims and metadata types.

### llama-swap (v239 to v255)

- `-validate` flag to check a config and exit; config path argument replaced with jq query support.
- Filters can set params only when undefined via a `?` key suffix; startup profile hook.
- `context_window` and `meta.n_ctx` exposed on the models endpoint; capability tags on the Models page.
- Client disconnects recorded as 499 instead of 200/502; OpenAI-compatible error bodies.
- vLLM speculative-decoding metrics parsed; argv-based vLLM startup in `vllm-wrapper`.
- Docker unified image gains CUDA 13 with arm64 multi-platform, configurable CUDA version and
  architectures, and `llama-bench` / `audio.cpp`; `/v1/task/run` added for audio.cpp.
- Tailcat private server and peer connectivity; ANSI colors rendered in the log view.

### whisper.cpp (v1.9.1+75 to v1.9.3)

Mostly a ggml sync (0.19.x to 0.20.2) carrying the llama.cpp backend work:

- CUDA graphs only disabled when `mul_mat_id` actually needs a stream sync; warp-per-row wkv7 kernel for
  single-token decode; UMA override skipped on HIP builds.
- New default load-mode `auto` avoids mmap on iGPUs.
- Ternary TQ2_0 support on Vulkan and Metal; recurrent-state rollback in `ggml_ssm_scan`.
- SYCL gate+up+GLU fusion for q4_K dense FFN, pinned host memory, ESIMD Q3_K DMMV kernel.
- CPU flash-attention V-cache F16-to-F32 conversion vectorized; kleidiai runtime feature detection.

### fabric (v1.4.458 to v1.4.478)

- Secure storage paths, and authentication on the REST and Ollama servers.
- Pzero and Synthorai added as OpenAI-compatible providers; raw mode enabled for GPT-6 models.
- Refreshed Codex tokens persisted and configured vendors reactivated.
- Split UTF-8 characters preserved across streaming/SSE chunk boundaries.
- Pattern loader temporary-directory leak fixed; `generate_frontmatter` registered for pattern discovery.
- Web stack modernized (SvelteKit/Shiki/Vite), PDF.js conversion replaced with a WASM worker.
- Turkish (tr) translation.

## 2026-07-14: 1.1 submodule refresh

| Submodule | From | To | Commits |
|---|---|---|---|
| llama.cpp | b9827 | b9993 | 166 |
| llama-swap | v230 | v239 | 22 |
| fabric | v1.4.455 | v1.4.458 | 10 |
| whisper.cpp | 0ae02cdb (v1.9.1+75) | unchanged | 0 |

whisper.cpp is left in place: its pin is already 75 commits past the newest release tag (v1.9.1), so
bumping to "latest release tag" would be a downgrade.

### llama.cpp (b9827 to b9993)

- **Models:** Hy3 (hy_v3) with MTP speculative decoding; Minimax2 eagle3 speculative support; DFlash
  speculative decoding plus `spec-draft-p-min`.
- **CUDA:** MMQ kernel configuration refactor; NVFP4 MMVQ post-scale fusion; f16 to f16
  `GGML_OP_SET_ROWS`; top_k/argsort now process in smaller chunks to cut temporary-buffer memory;
  cuBLAS refactor (removed `-sm row`); Turing P2P VMM pool allocation fix.
- **Server:** per-request `reasoning_budget_tokens` in chat completions; timings and progress on the
  `/responses` API stream; prompt-cache RAM limit; improved tools handling; checkpoint eviction within
  min-step; fix for image blocks dropped during Anthropic to OpenAI `tool_result` conversion; bracketed
  IPv6 URL authority handling.
- **Other backends:** SYCL fused top-k MoE and wider op coverage; OpenCL int8 dp4 dense and MoE prefill
  optimizations for Adreno, plus flash-attention decode perf; Vulkan NVFP4 (webgpu) and FA mask perf on
  GCN; Hexagon vision RoPE and MUL_MAT / FLASH_ATTN pipeline improvements.
- **API:** new `llama_model_ftype_name()`.

### llama-swap (v230 to v239)

- vLLM metrics support and improved metric calculation.
- Activity metrics persisted to SQLite; inflight and activity requests shown in the UI.
- New Svelte UI foundation (shadcn-svelte) with theming and rounded borders.
- `/props` and per-model status added to the `/v1/models` routes.
- Reject concurrency excess before streaming; log broadcast decoupled from writes.
- `llama-tts` binary added; UI embed gated behind an `embed_ui` build tag; macro resolution in
  capabilities fields; YAML anchors preserved in capabilities.

### fabric (v1.4.455 to v1.4.458)

- Add Claude Sonnet 5 Anthropic support.
- Respect Anthropic chat-option max-token overrides.
- Changelog generation for closed pull requests; duplicate-model listing cleanup.

### whisper.cpp

Unchanged. Pin remains `0ae02cdb` (v1.9.1 plus 75 commits), already newer than the latest tag.
