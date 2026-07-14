# FALLBACKS

Because all clients point at a single OpenAI-compatible endpoint, the stack is layered: swapping one component rarely requires touching the others. This document describes working alternatives at each layer and fixes for the most common failure modes.

**Diagnose first.** Before reaching for a fallback, run the built-in checks, they tell you which layer actually failed:

```
bob doctor      # full pre-flight diagnostics (build, models, ports, privileges)
bob diagnose    # system + model health check
```

| Layer | Primary | Fallback 1 | Fallback 2 (no build required) |
|---|---|---|---|
| Inference engine | llama.cpp (source build, CUDA 12.8) | Official prebuilt llama.cpp (CUDA 12.4 zip) | Ollama |
| Proxy / model router | llama-swap (Go build) | llama-swap release binary | Ollama's built-in model swapping |
| Chat and RAG UI | Open WebUI (Python 3.12, port 3000) | AnythingLLM desktop installer | LM Studio |
| IDE autocomplete | Continue.dev | twinny | LM Studio + Continue |
| Plan and edit separately | aider architect mode | Cline Plan/Act | Cline single-model |
| Embeddings | bge-m3 | nomic-embed-text | Open WebUI's built-in nomic |

## Engine won't build

`bob build` detects your GPU and CUDA version automatically and should work on RTX 3000, 4000, and 5000 series cards, on both Windows and Linux (the OS-specific bits, Visual Studio + CUDA DLLs on Windows, Ninja + `.so` on Linux, go through the `scripts/osenv.py` seam). If the build fails anyway, the options below are ordered from least to most disruptive.

**No GPU?** Build the CPU tier:

```
bob build --cpu
```

This is auto-selected when no GPU is detected. It produces a `-DGGML_CUDA=OFF` engine, and `bob profile auto` switches to the tiny `cpu` profile (`bob profile cpu` to force it). It's for correctness/wiring and CI, not performance. See [PORTABILITY.md](PORTABILITY.md).

**Prebuilt llama.cpp binary:** Download `*-bin-win-cuda-12.4-x64.zip` (Windows) or the matching Linux CUDA build from the [llama.cpp releases page](https://github.com/ggml-org/llama.cpp/releases). Extract the binaries to `bin/` and also copy the matching CUDA runtime libraries into `bin/` (`bob build` copies these automatically, but the prebuilt zip does not include them). This works on all supported GPU generations. On Blackwell it's slightly slower than a CUDA 12.8 source build; on Ada and Ampere the difference is negligible.

**Ollama:** If you want to skip the build entirely, Ollama has GPU support for all three generations with no compile step. Install it from the official site, then change every client's API base from `http://localhost:8081/v1` to `http://localhost:11434/v1`. The Continue, aider, and Open WebUI configs all use `apiBase`, so it's a one-line change per config. Peak performance is lower than a native build, but all clients work correctly.

**Any external OpenAI-compatible endpoint:** To skip local inference altogether, point clients at any OpenAI-compatible URL. Two ways:

- **Per client:** change each tool's base URL to the external endpoint (same one-liner as the Ollama case).
- **Through Bob's proxy:** add the provider as a peer in `config/user.json` so it flows through the LiteLLM proxy on `:8081` and is reachable as a `--pro` model. Give it a `proxy` (the endpoint's `api_base`) and an `apiKeyEnv` (the environment variable holding its key), then run `bob gen`:

  ```json
  {
    "peers": {
      "myprovider": {
        "enabled": true,
        "proxy": "https://api.example.com/v1",
        "apiKeyEnv": "MYPROVIDER_API_KEY",
        "litellmPrefix": "openai",
        "pro": { "chat": "gpt-4o-mini", "coder": "gpt-4o" }
      }
    }
  }
  ```

  Export the key first (Linux: `export MYPROVIDER_API_KEY=…`; Windows: `set MYPROVIDER_API_KEY=…`), then `bob chat --pro "…"` routes to it while local roles keep working.

## No Go compiler for llama-swap

`bob build` builds llama-swap from the Go submodule. If Go isn't installed, download the release binary for your OS from the [llama-swap releases page](https://github.com/mostlygeek/llama-swap/releases) (`llama-swap.exe` on Windows, the Linux binary as `llama-swap`) and place it in `bin/`. `bob build` and `bob serve` will use it as-is and skip the Go build.

## Open WebUI won't install

Open WebUI needs Python 3.11 or 3.12. Python 3.14 is too new; 3.10 is too old. It lives in its own virtual environment (`tools/venv-webui`), separate from aider's (`tools/venv-aider`), because their dependency pins conflict and can't share an environment. If pip still fails on the right Python version, the most reliable alternative is AnythingLLM, which is a desktop installer with no Python dependency. Install it, add an OpenAI connection pointing at `http://localhost:8081/v1`, choose a separate embedding backend, and organize documents per workspace.

## Model file not found on download

If `bob fetch` fails with a 404 or file-not-found error, the HuggingFace repository or filename for that model has probably changed. Open the model's page on huggingface.co, find the correct repo path and exact filename, update that model's `repo`, `path`, and `gguf` fields in `config/models.json`, then run `bob fetch` again. Use `bob fetch --list` first to preview the resolved download URLs without pulling anything.
