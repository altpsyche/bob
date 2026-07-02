# Module E — Tools Integration Fixes

**Priority:** Independent. Implement in any order, in any combination.

These are targeted fixes to the existing tool configurations and documentation.
None require script changes to the core inference stack.

## Overview

| Sub | Target | Fix |
|-----|--------|-----|
| E1 | `config/continue/config.yaml` | Add missing `contextLength` + `maxTokens` to all models |
| E2 | `docs/USAGE.md` | Expand `/no_think` paragraph → full Qwen3 Thinking Mode section |
| E3 | `docs/USAGE.md` | Add function-calling / tool-use example |
| E4 | `scripts/up.ps1` | ~~Read `webuiSecret` from defaults~~ **DONE** (Module D/A) |
| E5 | `scripts/setup-clients.ps1` | Check extensions are installed |
| E6 | `config/continue/config.yaml` | Add `mcpServers` block (filesystem, fetch, github, searxng) |
| E7 | `scripts/setup-fabric.ps1` | fabric CLI AI patterns — shell pipe text through 200+ prompt patterns **DONE** |

---

## E1 — Continue Context Window Fix

### Problem
`config/continue/config.yaml` configures four models but specifies no `contextLength`.
Without it, Continue.dev falls back to its default (8192 tokens for chat models).
USAGE.md documents 16384 — but the config doesn't enforce it, so users may see
truncated context on long files without any error message.

### Change: `config/continue/config.yaml`

Add `contextLength` to each model block. Also add `maxTokens` where appropriate:

```yaml
models:
  - name: coder
    provider: openai
    model: coder
    apiBase: http://localhost:8080/v1
    apiKey: sk-local
    temperature: 0.2
    contextLength: 16384    # <-- ADD
    maxTokens: 4096         # <-- ADD: max response tokens (not context window)
    roles:
      - chat
      - edit
      - apply

  - name: planner
    provider: openai
    model: planner
    apiBase: http://localhost:8080/v1
    apiKey: sk-local
    temperature: 0.3
    contextLength: 16384    # <-- ADD
    maxTokens: 8192         # <-- ADD: planner needs more room for long plans
    roles:
      - chat

  - name: autocomplete
    provider: openai
    model: fim
    apiBase: http://localhost:8080/v1
    apiKey: sk-local
    contextLength: 8192     # <-- ADD: fim model has 8192 ctx window
    roles:
      - autocomplete

  - name: embeddings
    provider: openai
    model: embed
    apiBase: http://localhost:8080/v1
    apiKey: sk-local
    roles:
      - embed
```

**Note on `maxTokens` vs `contextLength`:**
- `contextLength` = total tokens the model can process (input + output combined)
- `maxTokens` = maximum tokens in the model's response
- Both should be set explicitly. `maxTokens` not set = Continue uses its default (often 2048),
  which may truncate long code generations.

---

## E2 — Qwen3 Thinking / `/no_think` Documentation

### Problem
Qwen3 models (planner, chat) run an internal reasoning scratchpad by default. This scratchpad
consumes `max_tokens` silently before any visible output. A user doing `bob chat planner "plan X"`
with `max_tokens: 512` may get a truncated plan with no warning — the model spent 400 tokens
thinking before writing the plan.

Two common surprises:
1. Short responses when `max_tokens` is low (default 512)
2. Slow first token (scratchpad runs before any output streams)

### `docs/USAGE.md` addition

Insert after the existing "Direct API Calls" section:

```markdown
## Qwen3 Thinking Mode

Qwen3 models (planner, chat) use a reasoning scratchpad by default. Before responding,
the model internally reasons through the problem — this produces better answers but:

- **Consumes `max_tokens` silently.** The scratchpad counts toward your token limit.
  For complex tasks, set `max_tokens` to at least 2000 (or 8192 for deep planning).
- **Increases first-token latency.** The scratchpad runs before any visible output.
  For quick questions, use `/no_think` to skip it.

### Disabling the scratchpad

Append `/no_think` to your prompt:

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "chat",
    "max_tokens": 512,
    "messages": [{"role": "user", "content": "What is 2+2? /no_think"}]
  }'
```

Or via `bob chat`:
```powershell
bob chat chat "What is 2+2? /no_think" --max 128
```

### When to use each mode

| Mode | When to use | `max_tokens` |
|------|------------|-------------|
| Default (thinking on) | Complex reasoning, code architecture, planning | 2000–8192 |
| `/no_think` | Quick Q&A, simple edits, autocomplete-like tasks | 128–512 |

**In Continue.dev:** Add `/no_think` to the end of your message in the chat box.
Continue always uses the configured `maxTokens` — make sure it's large enough for planning tasks.

**In aider:** The planner model is used for architecture; thinking mode is appropriate.
aider auto-adjusts context size; no special configuration needed.
```

---

## E3 — Function-Calling / Tool-Use Example

### Problem
Qwen2.5-Coder-14B (coder) supports the OpenAI function-calling API including parallel tool calls.
This is powerful for agentic workflows (file operations, code execution, search) but is
completely undocumented in this project.

### `docs/USAGE.md` addition

Add a new section after the "Direct API Calls" section:

```markdown
## Function Calling (Tool Use)

The coder model supports the OpenAI tools API. You can define functions that the model
can request to call, then execute them in your application:

```powershell
$tools = @(
    @{
        type = 'function'
        function = @{
            name = 'read_file'
            description = 'Read the contents of a file'
            parameters = @{
                type = 'object'
                properties = @{
                    path = @{
                        type = 'string'
                        description = 'Path to the file'
                    }
                }
                required = @('path')
            }
        }
    }
)

$body = @{
    model    = 'coder'
    messages = @(
        @{ role = 'user'; content = 'What is in the file README.md?' }
    )
    tools = $tools
    tool_choice = 'auto'   # or 'required' to force tool use
} | ConvertTo-Json -Depth 10

$response = Invoke-RestMethod http://localhost:8080/v1/chat/completions `
    -Method POST -ContentType 'application/json' -Body $body

# Check if model wants to call a tool:
$choice = $response.choices[0]
if ($choice.finish_reason -eq 'tool_calls') {
    foreach ($tc in $choice.message.tool_calls) {
        $funcName = $tc.function.name
        $args     = $tc.function.arguments | ConvertFrom-Json
        Write-Host "Model wants to call: $funcName($($args | ConvertTo-Json -Compress))"
        # Execute the function, add result to messages, continue conversation...
    }
}
```

**Supported:** Qwen2.5-Coder-14B (`coder` role).
**Not supported:** Qwen3 models (`planner`, `chat`) — tool-use quality varies; use `coder` for agentic tasks.

**In aider:** aider handles tool use internally for file editing. No direct configuration needed.

**In Cline:** Cline uses its own tool-use protocol. Point it at `coder` for best results.
```

---

## E4 — WebUI Secret Key (Handled by Module A)

`WEBUI_SECRET_KEY` is currently hardcoded to `'bob-dev'` in `scripts/up.ps1`.

With Module A implemented, `up.ps1` reads from `cfg.defaults.webuiSecret`.
Users set a real secret in `config/user.psd1`:

```powershell
# config/user.psd1
@{
    defaults = @{
        webuiSecret = 'my-real-secret-32-chars-or-more'
    }
}
```

The only change in `scripts/up.ps1` (if Module A is not yet implemented):

```powershell
# Replace hardcoded:
$env:WEBUI_SECRET_KEY = 'bob-dev'

# With:
. "$PSScriptRoot\_models.ps1"
$cfg = Get-ModelsConfig
$env:WEBUI_SECRET_KEY = $cfg.defaults.webuiSecret ?? 'bob-dev'
```

---

## E5 — Extension Presence Check in `setup-clients.ps1`

### Problem
`setup-clients.ps1` creates symlinks to `~/.continue/config.yaml` and `~/.aider.conf.yml`
without checking whether the target tools are installed. If a user runs this script but hasn't
installed the VS Code Continue extension, the symlink sits silently and they wonder why
autocomplete isn't working.

### Change: `scripts/setup-clients.ps1`

Add after the symlink creation block:

```powershell
Write-Host "`nChecking tool installations..."

# Check VS Code Continue extension
$codeCmd = Get-Command code -ErrorAction SilentlyContinue
if ($codeCmd) {
    $continueInstalled = code --list-extensions 2>$null | Select-String -Quiet 'Continue.continue'
    if ($continueInstalled) {
        Write-Host "  [OK] Continue extension installed" -ForegroundColor Green
    } else {
        Write-Host "  [!] Continue extension NOT found" -ForegroundColor Yellow
        Write-Host "      Install: code --install-extension Continue.continue"
        Write-Host "      Or:      https://marketplace.visualstudio.com/items?itemName=Continue.continue"
    }

    # Check Cline extension (optional but common)
    $clineInstalled = code --list-extensions 2>$null | Select-String -Quiet 'saoudrizwan.claude-dev'
    if ($clineInstalled) {
        Write-Host "  [OK] Cline extension installed" -ForegroundColor Green
    } else {
        Write-Host "  [-] Cline extension not found (optional)"
        Write-Host "      Install: code --install-extension saoudrizwan.claude-dev"
    }
} else {
    Write-Host "  [-] VS Code (code) not on PATH — skipping extension checks" -ForegroundColor DarkGray
    Write-Host "      Install VS Code and re-run: .\scripts\setup-clients.ps1"
}

# Check aider is working
$aiderExe = Join-Path $PSScriptRoot '..\tools\venv-aider\Scripts\aider.exe'
if (Test-Path $aiderExe) {
    Write-Host "  [OK] aider installed at tools/venv-aider/" -ForegroundColor Green
} else {
    Write-Host "  [!] aider not found — run setup first: .\setup.bat" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Setup complete. Start the endpoint: bob serve"
```

---

## Verification

```powershell
# E1: Continue context
# Open VS Code with Continue, chat with a large file in context
# Verify it doesn't truncate at 8192 tokens (hard to test without a large codebase)
# OR: check that Continue's context window setting shows 16384 in the model picker

# E2: /no_think
bob chat chat "What is 2+2?" --max 64         # may be slow (thinking)
bob chat chat "What is 2+2? /no_think" --max 64  # should be fast

# E3: function-calling
# Run the PowerShell snippet from the docs; verify model returns finish_reason: 'tool_calls'

# E4: WebUI secret
# Add webuiSecret to user.psd1, run bob up, verify WEBUI_SECRET_KEY env var is set correctly
# (Start-Process sets child env; verify via: Get-Process open-webui | ... or check webui login)

# E5: extension check
.\scripts\setup-clients.ps1
# Should print OK/warning per extension
```

---

## E6 — Continue.dev MCP Context Providers

### Problem

Continue.dev v1 supports MCP (Model Context Protocol) servers as context providers,
but none are configured. Users can only reference `@codebase` and `@file`. Real-time
web context, GitHub PR/issue diffs, and external file references all require MCP servers.

### Change: `config/continue/config.yaml`

Add after the `models:` block:

```yaml
mcpServers:
  - name: filesystem
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "C:\\"]
  - name: fetch
    command: uvx
    args: ["mcp-server-fetch"]
  - name: github
    command: npx
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "${GITHUB_TOKEN}"
  - name: searxng-search
    command: npx
    args: ["-y", "mcp-searxng"]
    env:
      SEARXNG_URL: "http://localhost:8888"
```

**Prerequisites:**
- Node.js: `winget install OpenJS.NodeJS`
- uv: `winget install astral-sh.uv`
- `@github` requires a GitHub Personal Access Token in the environment as `GITHUB_TOKEN`
- `searxng-search` requires SearXNG running (Module F9 Docker stack, port 8888)

### Verification

Open VS Code with Continue, then type in the chat box:
- `@fetch https://docs.python.org/3/library/asyncio.html` — page content included as context
- `@filesystem C:\bob\README.md` — file included as context
- `@github` — prompts for repo/issue/PR search (requires `GITHUB_TOKEN` in env)

---

## E7 — fabric (Shell AI Patterns)

> **Status: DONE** — Implemented this session. Moved from Module F (F4) because it was built
> alongside E1–E6. The implementation lives in this repo and is fully functional.

### What was implemented

`danielmiessler/fabric` (Apache 2.0, ~47k+ stars) — pipes text through named AI prompt
patterns from the terminal. 254 patterns ship as part of the `external/fabric` submodule.

**Approach: Git submodule + Go build (not winget)**

fabric is added as a shallow git submodule at `external/fabric`. The Go binary is built
locally from `external/fabric/cmd/fabric/` (the main package — root has no `.go` files).
Patterns live at `external/fabric/data/patterns/` and are symlinked (or copied as fallback)
to `~/.config/fabric/patterns/`. No `fabric --updatepatterns` is needed — patterns stay in
sync with the pinned submodule commit.

**Files created/modified:**

- `.gitmodules` — added `external/fabric` submodule entry
- `scripts/setup-fabric.ps1` — init submodule, build `bin/fabric.exe` from `./cmd/fabric/`,
  write `~/.config/fabric/.env`, symlink/copy `data/patterns/` → `~/.config/fabric/patterns/`
- `scripts/llm.ps1` — added `fabric-setup` dispatch + `fabric` direct passthrough
- `docs/USAGE.md` — updated `## Shell AI Patterns — fabric` section

**Key implementation details:**
- Build: `go build -o bin\fabric.exe ./cmd/fabric/` (from `external/fabric/`)
- Config: `~/.config/fabric/.env` (not `.config/fabric/config.yaml` — fabric v1.4+ uses `.env`)
- Patterns: `external/fabric/data/patterns/` → `~/.config/fabric/patterns/` (254 patterns)
- Idempotent: binary skipped if `bin/fabric.exe` exists; patterns skipped if link/dir exists

**Usage after `bob fabric-setup`:**

```powershell
git diff --staged | fabric --pattern write_git_commit
cat error.log    | fabric --pattern explain
cat meeting.txt  | fabric --pattern extract_wisdom
cat myfile.py    | fabric --pattern code_review
fabric --listpatterns    # see all 254 patterns
```

fabric defaults to the `coder` model. Pass `--model planner` for reasoning-heavy patterns.

**Pattern table:**

| Pattern | Use case |
|---------|---------|
| `write_git_commit` | Conventional commit message from `git diff` |
| `code_review` | Security + quality review |
| `explain` | Plain-English explanation of errors/code |
| `summarize` | Bullet-point summary |
| `extract_wisdom` | Key insights from articles/meetings |
| `improve_writing` | Polish prose |

### Setup

```powershell
# One-time build + configure (requires Go on PATH — installed by bootstrap.ps1)
bob fabric-setup

# Verify
echo "hello world" | fabric --pattern summarize

# Update after `git submodule update --remote external/fabric`
Remove-Item bin\fabric.exe   # force rebuild
bob fabric-setup
```

Requires: `bob serve` running (fabric calls the local endpoint). Fully offline after setup —
no `--updatepatterns`, no external downloads. Pattern updates come via `git submodule update`.

---

## Files Modified

| File | Change |
|------|--------|
| `config/continue/config.yaml` | Add `contextLength`, `maxTokens` to all models; add `mcpServers` block |
| `docs/USAGE.md` | Qwen3 Thinking Mode section; function-calling section; fabric section; MCP note in Continue section |
| `scripts/setup-clients.ps1` | Extension presence checks; remove redundant "install extensions" line |
| `scripts/setup-fabric.ps1` | **New** — install + configure fabric to use local endpoint |
| `scripts/llm.ps1` | Add `fabric-setup` dispatch case |

## Estimated Effort

- E1: 15 min
- E2: 30 min (writing, no code)
- E3: 30 min (writing + testing snippet)
- E4: **DONE** — skip
- E5: 30 min
- E6: 30 min (config + prereq docs)
- E7: **DONE** — skip (implemented, moved from F4)

Total: ~2 hours
