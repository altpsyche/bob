# Plan: Configurable API Pro-Model Peers ✓ IMPLEMENTED

> **Status: implemented.** See [USAGE.md](../USAGE.md#pro-models-api-backed-no-platform-fee) for usage.
> Architecture revised from this spec: pro models route **litellm → API provider directly**
> (not through llama-swap — llama-swap does not translate model names). Default providers
> changed to DeepSeek + Zhipu (no platform fee) instead of OpenRouter (5.5% fee).
> `langfuseEnabled` flag added to `defaults` so Langfuse can be enabled without editing
> the generated `config/litellm.yaml`.

**Goal:** Add `chat-pro`, `planner-pro`, `coder-pro` model names that route through
litellm directly to an API provider (DeepSeek / Zhipu GLM). Fully configurable from one place.
Users swap providers or models by editing only `config/user.psd1`. No generated files
touched by hand.

---

## Implemented Architecture (inside → out)

```
config/models.psd1           ← peers block: provider URLs, apiKeyEnv, litellmPrefix, pro roles
config/user.psd1             ← machine overrides (gitignored), merged at read time
scripts/_models.ps1          ← Get-ModelsConfig() merges peers; Get-EnabledPeers() helper
scripts/gen-llama-swap.ps1   ← unchanged (no peers section — llama-swap only serves local models)
scripts/gen-litellm.ps1      ← NEW: reads models.psd1 → emits config/litellm.yaml
config/litellm.yaml          ← GENERATED (was hand-maintained)
scripts/llm.ps1              ← 'bob gen' calls both generators
scripts/start.ps1            ← also calls gen-litellm.ps1 before starting stack
```

Runtime call path:

```
clients → litellm :8081 → llama-swap :8080 → llama-server (local)
                        ↘ DeepSeek API  (chat-pro, planner-pro — direct, no fee)
                        ↘ Zhipu API     (coder-pro — direct, no fee)
```

---

## Original Spec (for reference)

```
config/models.psd1           ← single source of truth (PSData, no code)
config/user.psd1             ← machine overrides (gitignored), merged at read time
scripts/_models.ps1          ← Get-ModelsConfig(): loads + merges base+user
scripts/gen-llama-swap.ps1   ← reads models.psd1 → emits config/llama-swap.yaml
config/llama-swap.yaml       ← GENERATED, read by llama-swap :8080
config/litellm.yaml          ← MANUAL today (fragile: model names hardcoded)
scripts/llm.ps1              ← 'bob gen' calls gen-llama-swap.ps1 only
```

Runtime call path:

```
clients → litellm :8081 → llama-swap :8080 → llama-server (local)
                                            ↘ OpenRouter (pro)
```

---

## What Changes (inside → out)

### Step 1 — Data schema: `config/models.psd1`

Add a `peers` top-level key after `group`, before `profiles`:

```powershell
peers = @{
  openrouter = @{
    enabled   = $true
    proxy     = 'https://openrouter.ai/api'
    apiKeyEnv = 'OPENROUTER_API_KEY'      # env var NAME — never the key value
    # Keys here become model IDs: "{key}-pro" (e.g. chat-pro, planner-pro, coder-pro).
    # Override any of these in user.psd1 to switch providers or models.
    pro = @{
      chat    = 'deepseek/deepseek-v3.2'
      planner = 'deepseek/deepseek-v3.2'
      coder   = 'z-ai/glm-5.2'
    }
  }
}
```

Design rules:
- `enabled = $false` → peer omitted from all generated files; no API key needed.
- `apiKeyEnv` holds the env var **name** only. Value stays outside git.
- `pro` keys are arbitrary role names → emitted as `{key}-pro` model IDs.
- Multiple peer blocks allowed (add `anthropic`, `groq`, etc. alongside `openrouter`).
- Peers with no `pro` entries are silently skipped in generators.

---

### Step 2 — Merge layer: `scripts/_models.ps1`

`Get-ModelsConfig` already merges `defaults` and `profiles` from `user.psd1`.
Extend it to also merge `peers`. Add this block after the existing `profiles`
merge section:

```powershell
if ($user.peers) {
    if (-not $base.peers) { $base.peers = @{} }
    foreach ($peerName in $user.peers.Keys) {
        if (-not $base.peers.Contains($peerName)) {
            # New peer defined only in user.psd1 — add wholesale.
            $base.peers[$peerName] = $user.peers[$peerName]
            continue
        }
        # Existing peer — merge key by key.
        foreach ($k in $user.peers[$peerName].Keys) {
            if ($k -eq 'pro') {
                # Deep merge: individual role overrides within pro{}.
                if (-not $base.peers[$peerName].pro) { $base.peers[$peerName].pro = @{} }
                foreach ($role in $user.peers[$peerName].pro.Keys) {
                    $base.peers[$peerName].pro[$role] = $user.peers[$peerName].pro[$role]
                }
            } else {
                $base.peers[$peerName][$k] = $user.peers[$peerName][$k]
            }
        }
    }
}
```

Also add a helper used by both generators:

```powershell
function Get-EnabledPeers {
    param($Config)
    if (-not $Config) { $Config = Get-ModelsConfig }
    if (-not $Config.peers) { return @() }
    return @(
        $Config.peers.Keys | Where-Object { $Config.peers[$_].enabled -ne $false } |
        ForEach-Object {
            $p = $Config.peers[$_].Clone()
            $p['name'] = $_
            [pscustomobject]$p
        }
    )
}
```

---

### Step 3 — Generator A: `scripts/gen-llama-swap.ps1`

After the existing `groups:` emission block, append a `peers:` section for each
enabled peer that has at least one `pro` entry.

Target output shape:

```yaml
peers:
  openrouter:
    proxy: https://openrouter.ai/api
    apiKey: ${env.OPENROUTER_API_KEY}
    models:
      - chat-pro
      - planner-pro
      - coder-pro
```

Implementation notes:
- Call `Get-EnabledPeers` (dot-sourced from `_models.ps1`).
- Skip peers with empty or missing `pro` hashtable.
- Model name = `"$role-pro"` for each key in `$peer.pro`.
- `apiKey` line uses llama-swap's `${env.VAR}` macro, not the key value.
- If `peers` block has content, emit a blank line before it (matches existing style).
- Warn (but do not fail) if `[System.Environment]::GetEnvironmentVariable($peer.apiKeyEnv)` is
  null at generation time — the key may be set in the shell that starts llama-swap.
- If no enabled peers with pro entries: omit the `peers:` block entirely.

---

### Step 4 — Generator B: `scripts/gen-litellm.ps1` (new file)

Create this script. It replaces the hand-maintained `config/litellm.yaml`.

Inputs: `Get-Models` (local role names) + `Get-EnabledPeers` (pro model names).

Target output shape (all routes point to llama-swap; litellm is proxy/retry only):

```yaml
# GENERATED - DO NOT EDIT.  Source: config/models.psd1
# Regenerate: scripts/gen-litellm.ps1  (also runs on `bob gen` and `bob serve`)

model_list:
  - model_name: coder
    litellm_params:
      model: openai/coder
      api_base: http://localhost:8080/v1
      api_key: sk-local
  - model_name: planner
    litellm_params:
      model: openai/planner
      api_base: http://localhost:8080/v1
      api_key: sk-local
  - model_name: chat
    litellm_params:
      model: openai/chat
      api_base: http://localhost:8080/v1
      api_key: sk-local
  - model_name: fim
    litellm_params:
      model: openai/fim
      api_base: http://localhost:8080/v1
      api_key: sk-local
  - model_name: embed
    litellm_params:
      model: openai/embed
      api_base: http://localhost:8080/v1
      api_key: sk-local
  - model_name: coder-pro
    litellm_params:
      model: openai/coder-pro
      api_base: http://localhost:8080/v1
      api_key: sk-local
  - model_name: planner-pro
    litellm_params:
      model: openai/planner-pro
      api_base: http://localhost:8080/v1
      api_key: sk-local
  - model_name: chat-pro
    litellm_params:
      model: openai/chat-pro
      api_base: http://localhost:8080/v1
      api_key: sk-local

litellm_settings:
  num_retries: 3
  request_timeout: 120

general_settings:
  master_key: sk-local
```

Key points:
- `api_base` port comes from `$cfg.defaults.port` (8080 = llama-swap), NOT litellmPort.
- Pro entries only emitted when at least one enabled peer with `pro` entries exists.
- Local model order: stable (`planner`, `coder`, `chat`, `fim`, `embed` then any extras).
- Pro model order: alphabetical by role name within each peer, peers in definition order.
- Output file: `config/litellm.yaml`. Overwrite unconditionally (idempotent).

---

### Step 5 — CLI hookup: `scripts/llm.ps1`

Two changes:

1. `bob gen` currently calls only `gen-llama-swap.ps1`. Update to call both:

```powershell
'gen' {
    & "$repo\scripts\gen-llama-swap.ps1" @rest
    & "$repo\scripts\gen-litellm.ps1"
}
```

2. `scripts/start.ps1` already calls `gen-llama-swap.ps1` before starting llama-swap.
   Add a call to `gen-litellm.ps1` immediately after — so `bob serve` also regenerates
   both files. Locate the existing gen call in `start.ps1` and append the second call.

---

### Step 6 — User override story: `config/user.psd1.example`

Add a `peers` block to the example file so users know how to customize:

```powershell
# ── API Pro Models (optional) ────────────────────────────────────────────────
# Pro models (chat-pro / coder-pro / planner-pro) route via llama-swap to an
# API provider. Override model choices or disable entirely without touching
# the tracked models.psd1.
# Re-run `bob gen` after editing. No restart needed if llama-swap is already up
# with the new config — send a request to the pro model name to trigger load.
#
# peers = @{
#   openrouter = @{
#     # Disable all pro models (no API key required):
#     # enabled = $false
#
#     pro = @{
#       # Override individual roles — leave unset roles at their defaults:
#       # coder   = 'anthropic/claude-sonnet-4-6'
#       # planner = 'deepseek/deepseek-r1'
#       # chat    = 'openai/gpt-4o'
#     }
#   }
# }
```

---

## Invariants — Must Not Break

1. **No peers section in models.psd1** → behavior identical to today; no `peers:` block
   in llama-swap.yaml, litellm.yaml contains only the five local model entries.
2. **`enabled = $false`** → same as above; peer is fully absent from both generated files.
3. **`user.psd1` without `peers` key** → `Get-ModelsConfig` returns base peers unchanged.
4. **Generators are idempotent** — running twice produces byte-identical output.
5. **Missing `OPENROUTER_API_KEY` at gen time** → warn, do not fail. llama-swap will
   error on first pro-model request (explicit failure is better than silent wrong state).
6. **`bob gen` with `12gb` arg** → `gen-llama-swap.ps1` receives the profile arg as
   today; `gen-litellm.ps1` uses the active profile (pro model list is profile-independent).
7. **`config/litellm.yaml` content** — after implementation, running `bob gen` with no
   peers configured must produce output matching the current hand-written file exactly
   (same model names, same ports, same settings). Verify before committing.

---

## Validation Checklist

```
[ ] bob gen                        → both config files updated, no errors
[ ] grep 'peers:' config/llama-swap.yaml   → section present with correct models
[ ] grep 'pro' config/litellm.yaml         → pro entries present
[ ] OPENROUTER_API_KEY=sk-test bob serve   → llama-swap starts without error
[ ] curl :8080/v1/models           → chat-pro, planner-pro, coder-pro in list
[ ] curl :8081/v1/models           → same via litellm
[ ] user.psd1: peers.openrouter.pro.coder = 'anthropic/claude-test'
    bob gen → coder-pro in llama-swap.yaml points to new model
[ ] user.psd1: peers.openrouter.enabled = $false
    bob gen → no peers: block in llama-swap.yaml, no pro entries in litellm.yaml
[ ] bob gen 12gb → llama-swap.yaml has 12gb models, litellm.yaml unchanged (pro is profile-agnostic)
[ ] diff current litellm.yaml vs generated (no-peers run) → identical
```

---

## File Manifest

| File | Change |
|---|---|
| `config/models.psd1` | Add `peers` block |
| `scripts/_models.ps1` | Add peers merge in `Get-ModelsConfig`; add `Get-EnabledPeers` |
| `scripts/gen-llama-swap.ps1` | Append `peers:` YAML block when peers exist |
| `scripts/gen-litellm.ps1` | **NEW** — generates `config/litellm.yaml` |
| `config/litellm.yaml` | Header updated to GENERATED; content regenerated |
| `scripts/llm.ps1` | `bob gen` calls both generators |
| `scripts/start.ps1` | Also calls `gen-litellm.ps1` before starting stack |
| `config/user.psd1.example` | Add `peers` override example |
