# Module H — Developer Services

**Priority:** Independent. No dependency on Modules A–G.

These are infrastructure services that run alongside the inference stack. They are all
CPU-only and run via Docker Compose, while GPU tools (llama.cpp, whisper, Qdrant) remain
native for maximum performance.

Ports are configurable via `config/user.psd1`. Defaults live in the `defaults` block of
`config/models.psd1` and are written to `tools/compose/.env` by `scripts/setup-docker.ps1`.

---

## Overview

| Sub | Name | Port (default) | Type | Effort |
|-----|------|---------------|------|--------|
| H1 | LiteLLM proxy | 8081 | Python venv | 3 h |
| H2 | Langfuse | 3001 | Docker service | 2 h |
| H3 | SearXNG | 8888 | Docker service | 1 h |
| H4 | n8n | 5678 | Docker service | 2 h |
| H5 | lm-evaluation-harness | — | Python venv | 2 h |

All Docker services (H2–H4) start together: `bob services start` or `.\scripts\setup-docker.ps1`.

---

## H1 — LiteLLM Proxy (API Gateway)

### Problem

All clients (Continue.dev, Cline, aider) call llama-swap directly. A failed 503 is
silently lost. No retry, no request logging, no path to cloud fallback.

### Architecture

```
Clients  →  LiteLLM :8081  →  llama-swap :8080
             retry + log          models
```

Clients can optionally point to `:8081` instead of `:8080`. llama-swap keeps running.

### Change

Isolated Python venv (`tools/venv-litellm/`), same pattern as aider and webui.

| File | Purpose |
|------|---------|
| `tools/litellm-requirements.txt` | `litellm[proxy]>=1.40.0` |
| `scripts/bootstrap-litellm.ps1` | Creates the venv |
| `config/litellm.yaml` | Route table — all models through llama-swap; optional Langfuse callbacks |
| `scripts/start-litellm.ps1` | Starts proxy (foreground or `-NoWindow`) |

**CLI:** `bob litellm` starts the proxy interactively.

**Port:** 8081. Reads `defaults.litellmPort` from `config/models.psd1` if set.

**Enabling Langfuse tracing** (after H2 is running):
In `config/litellm.yaml`, uncomment:
```yaml
litellm_settings:
  success_callback: ["langfuse"]
  failure_callback: ["langfuse"]
  langfuse_host: http://localhost:3001   # or your langfusePort
  langfuse_public_key: pk-lf-...        # from Langfuse UI → Settings → API Keys
  langfuse_secret_key: sk-lf-...
```

### Verification

```powershell
.\scripts\bootstrap-litellm.ps1
bob litellm                              # starts on :8081
curl http://localhost:8081/v1/models     # lists coder, planner, chat, fim, embed
```

---

## H2 — Langfuse (bob Observability)

### Problem

Zero visibility into what Continue.dev, Cline, and aider send. No latency data, no token
counts per session, no way to detect quality regressions when switching quants or profiles.

### Change

Docker Compose service. Langfuse + PostgreSQL. Managed by `setup-docker.ps1` and
`bob services start|stop`.

**Default port:** 3001 (configurable via `defaults.langfusePort` in `models.psd1`).

**Default credentials:** `admin@local.dev` / `admin123`

After setup, open `http://localhost:3001`, create an API key pair in
Settings → API Keys, then paste them into `config/litellm.yaml` to activate tracing.

### Verification

```powershell
.\scripts\setup-docker.ps1
# Open http://localhost:3001 — Langfuse dashboard loads
bob litellm   # start proxy (H1)
bob chat coder "test" --max 64
# → trace appears in Langfuse dashboard within ~2s
```

---

## H3 — SearXNG (Private Web Search)

### Problem

The Continue.dev MCP `searxng-search` server (E6) and fabric web patterns need a local
search endpoint. Public search APIs require an API key; SearXNG requires nothing.

### Change

Docker Compose service. Port 8888 (configurable via `defaults.searxngPort`).

Auto-generated config: `config/searxng/settings.yml` (written by `setup-docker.ps1` if absent).

**Wired to Continue MCP** in `config/continue/config.yaml` (already set in E6):
```yaml
- name: searxng-search
  command: npx
  args: ["-y", "mcp-searxng"]
  env:
    SEARXNG_URL: "http://localhost:8888"
```

### Verification

```powershell
bob services start
# Open http://localhost:8888 — search for "llama.cpp" — results appear
# In Continue.dev chat: type @searxng-search "latest qwen3 benchmarks"
```

---

## H4 — n8n (Workflow Automation)

### Problem

No way to trigger the local LLM on external events — git pushes, CI results, file changes,
PR comments. Everything is manually initiated.

### Change

Docker Compose service. Port 5678 (configurable via `defaults.n8nPort`).

Data: `tools/n8n-data/` (gitignored, persists workflows across restarts).

**Example workflows to build:**
- `git push` → read diff → POST to LiteLLM → post comment to PR
- `file change in src/` → `bob eval coder humaneval` → notify if score drops
- Slack message → local LLM reply → Slack response

**n8n calls the local endpoint** the same as any other client:
- Base URL: `http://host.docker.internal:8080/v1` (Docker → host network)
- Or via LiteLLM: `http://host.docker.internal:8081/v1` (with retry)
- API Key: `sk-local`

### Verification

```powershell
bob services start
# Open http://localhost:5678 — n8n workflow editor loads
# Create a test workflow: HTTP Request node → POST to http://host.docker.internal:8080/v1/chat/completions
```

---

## H5 — lm-evaluation-harness (Model Quality Benchmarking)

### Problem

`bob bench` measures tokens/sec only. No quality metric. Switching Q4_K_M → Q5_K_M or
changing VRAM profiles may silently degrade reasoning accuracy.

### Change

Isolated Python venv (`tools/venv-eval/`), same pattern as aider and webui.

| File | Purpose |
|------|---------|
| `tools/eval-requirements.txt` | `lm-eval>=0.4.0` |
| `scripts/bootstrap-eval.ps1` | Creates the venv |
| `scripts/eval.ps1` | Runs benchmarks; saves results to `results/` (gitignored) |

**CLI:** `bob eval <role> [task] [--shots N]`

Supported tasks: `mmlu` (general knowledge), `humaneval` (code), `gsm8k` (math), `hellaswag` (reasoning).

### Verification

```powershell
.\scripts\bootstrap-eval.ps1
bob serve              # in another terminal
bob eval coder mmlu    # runs 0-shot MMLU against coder model
# Accuracy score printed; JSON results in results/eval-coder-mmlu-*/
```

---

## Port Configuration

All service ports read from `config/models.psd1` `defaults` block and written to
`tools/compose/.env` by `setup-docker.ps1`. Override in `config/user.psd1`:

```powershell
# config/user.psd1
@{
    defaults = @{
        langfusePort = 3001    # Langfuse dashboard
        searxngPort  = 8888    # SearXNG search
        n8nPort      = 5678    # n8n automation
        litellmPort  = 8081    # LiteLLM proxy
    }
}
```

After editing, re-run `.\scripts\setup-docker.ps1` to regenerate `tools/compose/.env`.

---

## Files Created / Modified

| File | Action |
|------|--------|
| `tools/litellm-requirements.txt` | New |
| `scripts/bootstrap-litellm.ps1` | New |
| `config/litellm.yaml` | New |
| `scripts/start-litellm.ps1` | New |
| `scripts/setup-docker.ps1` | New |
| `tools/compose/docker-compose.yml` | New |
| `tools/eval-requirements.txt` | New |
| `scripts/bootstrap-eval.ps1` | New |
| `scripts/eval.ps1` | New |
| `scripts/up.ps1` | Add `-WithServices` switch |
| `scripts/llm.ps1` | Add `litellm`, `services`, `eval` dispatch cases |
| `config/models.psd1` | Add `langfusePort`, `searxngPort`, `n8nPort`, `litellmPort` to defaults |
| `.gitignore` | Add `tools/langfuse-data/`, `tools/n8n-data/`, `tools/searxng-data/`, `results/` |

---

## Total Estimated Effort

| Sub | Hours |
|-----|-------|
| H1 LiteLLM proxy | 3 |
| H2 Langfuse | 2 |
| H3 SearXNG | 1 |
| H4 n8n | 2 |
| H5 lm-eval | 2 |
| **Total** | **~10 hours** |
