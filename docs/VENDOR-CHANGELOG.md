# Vendor Changelog

Bob vendors four upstream projects as git submodules under `external/`, pinned in
[`versions.lock`](../versions.lock). This file records what changed in each upstream project when Bob
bumps a pin. For Bob's own release notes, see [CHANGELOG.md](../CHANGELOG.md).

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
