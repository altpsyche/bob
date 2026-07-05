# FALLBACKS

Because all clients point at a single OpenAI-compatible endpoint, the stack is layered: swapping one component rarely requires touching the others. This document describes working alternatives at each layer and fixes for the most common failure modes.

| Layer | Primary | Fallback 1 | Fallback 2 (no build required) |
|---|---|---|---|
| Inference engine | llama.cpp (source build, CUDA 12.8) | Official prebuilt llama.cpp (CUDA 12.4 zip) | Ollama |
| Proxy / model router | llama-swap (Go build) | llama-swap release binary | Ollama's built-in model swapping |
| Chat and RAG UI | Open WebUI (Python 3.12, port 3000) | Anythingbob desktop installer | LM Studio |
| IDE autocomplete | Continue.dev | twinny | LM Studio + Continue |
| Plan and edit separately | aider architect mode | Cline Plan/Act | Cline single-model |
| Embeddings | bge-m3 | nomic-embed-text | Open WebUI's built-in nomic |

## Engine won't build

The build script (`scripts/build-llama.ps1`) detects your GPU and CUDA version automatically and should work on RTX 3000, 4000, and 5000 series cards, on both Windows and Linux (via the `_platform.ps1` seam: Visual Studio + CUDA DLLs on Windows, Ninja + `.so` on Linux). If the build fails anyway, the options below are ordered from least to most disruptive.

**No GPU?** Build the CPU tier: `bob build --cpu` (auto-selected when no GPU is detected) produces a `-DGGML_CUDA=OFF` engine, and `bob profile auto` switches to the tiny `cpu` profile. It's for correctness/wiring and CI, not performance. See [PORTABILITY.md](PORTABILITY.md).

**Prebuilt llama.cpp binary:** Download `*-bin-win-cuda-12.4-x64.zip` from the [llama.cpp releases page](https://github.com/ggml-org/llama.cpp/releases), or install with `scoop install llama.cpp-cu124`. Extract the binaries to `bin/` and also copy the matching CUDA DLLs into `bin/` (the build script copies these automatically, but the prebuilt zip does not include them). This works on all supported GPU generations. On Blackwell it's slightly slower than a CUDA 12.8 source build; on Ada and Ampere the difference is negligible.

**Ollama:** If you want to skip the build entirely, Ollama has GPU support for all three generations with no compile step. Install it from the official site, then change every client's API base from `http://localhost:8081/v1` to `http://localhost:11434/v1`. The Continue, aider, and Open WebUI configs all use `apiBase`, so it's a one-line change per config. Peak performance is lower than a native build, but all clients work correctly.

## No Go compiler for llama-swap

Download the release binary for your OS from the [llama-swap releases page](https://github.com/mostlygeek/llama-swap/releases) (`llama-swap.exe` on Windows, the Linux binary as `llama-swap`) and place it in `bin/`. Skip `build-llama-swap.ps1`.

## Open WebUI won't install

Open WebUI needs Python 3.11 or 3.12. Python 3.14 is too new; 3.10 is too old. It lives in its own virtual environment (`tools/venv-webui`), separate from aider's (`tools/venv-aider`), because their dependency pins conflict and can't share an environment. If pip still fails on the right Python version, the most reliable alternative is AnythingLLM, which is a desktop installer with no Python dependency. Install it, add an OpenAI connection pointing at `http://localhost:8081/v1`, choose a separate embedding backend, and organize documents per workspace.

## Model file not found on download

If `bob fetch` fails with a 404 or file-not-found error, the HuggingFace repository or filename for that model has probably changed. Open the model's page on huggingface.co, find the correct repo path and exact filename, update that model's `repo`, `path`, and `gguf` fields in `config/models.psd1`, then run `bob fetch` again. Use `bob fetch --list` first to preview the resolved download URLs without pulling anything.
