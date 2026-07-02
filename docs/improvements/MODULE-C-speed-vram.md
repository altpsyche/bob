# Module C — Speed / VRAM Optimization

**Depends on:** Module A (config tuneability) for C2 and C3.
C1 (new profiles) is independent.

## Overview

| Sub | Name | Benefit |
|-----|------|---------|
| C1 | 24 GB + 32 GB profiles | Better quants for large-VRAM cards; no-swap tier at 32 GB |
| C2 | `--mlock` for pinned models | Prevents OS paging fim/embed weights under memory pressure |
| C3 | Speculative decoding | ~20–40% generation speedup for coder via draft model |

---

## C1 — 24 GB and 32 GB VRAM Profiles

### Context

Current profiles: 8 GB, 12 GB, 16 GB. RTX 3090 (24 GB), RTX 4090 (24 GB), RTX 5090 (32 GB),
and A6000 (48 GB) users all fall back to the 16 GB profile — correct VRAM budget but wrong quant
quality. With more headroom available:

- **24 GB**: use Q5_K_M / Q6_K quants for better output quality on same model sizes
- **32 GB**: all 5 models resident simultaneously — zero swap latency on role switches

`Get-SuggestedProfile` in `_models.ps1` already handles any `<N>gb` profile name
(largest N that fits detected VRAM). No changes needed to that function.

### `config/models.psd1` additions

Add inside the `profiles` hashtable, after the `'16gb'` block:

```powershell
'24gb' = @{
    _targetVRAM = '24gb'
    _notes      = 'RTX 3090 / 4090 / 4080 Super. Better quants than 16gb; still swaps planner.'

    # Planner: Q5_K_M — ~0.70 GB/B × 30B = ~21 GB. Meaningfully better than Q4 for reasoning.
    planner = @{
        repo   = 'bartowski/Qwen3-30B-A3B-GGUF'
        path   = 'Qwen3-30B-A3B-Q5_K_M.gguf'
        gguf   = 'qwen3-30b-a3b-q5_k_m.gguf'
        ctx    = 16384
        kv     = $true
        sizeGB = 21.3
        flags  = @('--temp', '0.3')
    }

    # Coder: Q6_K — ~0.75 GB/B × 14B = ~10.5 GB. Near-lossless for code.
    coder = @{
        repo   = 'bartowski/Qwen2.5-Coder-14B-Instruct-GGUF'
        path   = 'Qwen2.5-Coder-14B-Instruct-Q6_K.gguf'
        gguf   = 'qwen-coder-14b-q6_k.gguf'
        ctx    = 16384
        kv     = $true
        sizeGB = 10.7
    }

    # Chat: Q6_K — ~0.75 GB/B × 14B = ~10.5 GB.
    chat = @{
        repo   = 'bartowski/Qwen3-14B-GGUF'
        path   = 'Qwen3-14B-Q6_K.gguf'
        gguf   = 'qwen3-14b-q6_k.gguf'
        ctx    = 16384
        kv     = $true
        sizeGB = 10.7
        setParams = @{ temperature = 0.7; top_p = 0.9 }
    }

    # FIM: same as 16gb (already optimal at Q8_0 for 3B model)
    fim = @{
        repo   = 'bartowski/Qwen2.5-Coder-3B-Instruct-GGUF'
        path   = 'Qwen2.5-Coder-3B-Instruct-Q8_0.gguf'
        gguf   = 'qwen-coder-3b-q8_0.gguf'
        ctx    = 8192
        sizeGB = 3.4
        ttl    = 0
        pinned = $true
        mlock  = $true
    }

    # Embed: same as 16gb
    embed = @{
        repo      = 'gpustack/bge-m3-GGUF'
        path      = 'bge-m3-Q8_0.gguf'
        gguf      = 'bge-m3-q8_0.gguf'
        sizeGB    = 0.6
        ttl       = 0
        pinned    = $true
        mlock     = $true
        embedding = $true
    }
}

'32gb' = @{
    _targetVRAM = '32gb'
    _notes      = 'RTX 5090 / A6000 / RTX 3090 Ti. No swap — all models resident. Zero-latency switching.'

    # Planner: Q6_K — ~24 GB. Best quality with 32 GB headroom.
    planner = @{
        repo   = 'bartowski/Qwen3-30B-A3B-GGUF'
        path   = 'Qwen3-30B-A3B-Q6_K.gguf'
        gguf   = 'qwen3-30b-a3b-q6_k.gguf'
        ctx    = 32768           # Extended context — 32 GB allows larger KV cache
        kv     = $true
        sizeGB = 24.2
        flags  = @('--temp', '0.3')
    }

    # Coder: Q8_0 — near-lossless, ~15 GB
    coder = @{
        repo      = 'bartowski/Qwen2.5-Coder-14B-Instruct-GGUF'
        path      = 'Qwen2.5-Coder-14B-Instruct-Q8_0.gguf'
        gguf      = 'qwen-coder-14b-q8_0.gguf'
        ctx       = 32768
        kv        = $true
        sizeGB    = 15.0
        draftRole = 'fim'        # Speculative decoding (see C3). Same Qwen2.5 tokenizer.
    }

    # Chat: Q8_0 — near-lossless, ~15 GB
    chat = @{
        repo      = 'bartowski/Qwen3-14B-GGUF'
        path      = 'Qwen3-14B-Q8_0.gguf'
        gguf      = 'qwen3-14b-q8_0.gguf'
        ctx       = 32768
        kv        = $true
        sizeGB    = 15.0
        setParams = @{ temperature = 0.7; top_p = 0.9 }
    }

    # FIM: Q8_0 — pinned
    fim = @{
        repo   = 'bartowski/Qwen2.5-Coder-3B-Instruct-GGUF'
        path   = 'Qwen2.5-Coder-3B-Instruct-Q8_0.gguf'
        gguf   = 'qwen-coder-3b-q8_0.gguf'
        ctx    = 8192
        sizeGB = 3.4
        ttl    = 0
        pinned = $true
        mlock  = $true
    }

    embed = @{
        repo      = 'gpustack/bge-m3-GGUF'
        path      = 'bge-m3-Q8_0.gguf'
        gguf      = 'bge-m3-q8_0.gguf'
        sizeGB    = 0.6
        ttl       = 0
        pinned    = $true
        mlock     = $true
        embedding = $true
    }
}
```

**No-swap group for 32 GB profile:**

The `group` block currently defines a single global swap group. To support the 32 GB "no swap"
behavior, the group must be profile-aware. Options:

**Option A (simple):** Move `group` inside each profile block. The generator reads `$profile.group`
instead of top-level `$cfg.group`. The 32 GB profile omits `members` or sets `swap = $false`.

```powershell
# Inside '32gb' profile:
group = @{
    name = 'ondemand'
    swap = $false
    members = @()   # No swap group — all models always resident
}
```

**Option B (current schema, minimal change):** Keep global group; the 32 GB profile marks
all models as `pinned = $true`, excluding them from the swap group. The group exists but has
no members, which is valid per the current generator validation (empty members = no swap group).

**Recommendation: Option B** — smallest diff to existing generator logic.
In 32 GB profile, set all swap-group models (`planner`, `coder`, `chat`) to have `ttl = 0`
and `pinned = $true`. The generator's group-assertion code already validates "pinned models
cannot be group members" — with all models pinned, the group has zero members.

Update `config/models.psd1` `group` block comment:
```powershell
# Swap group applies to 8gb/12gb/16gb/24gb profiles.
# 32gb profile marks all models pinned, so group.members is effectively empty for that profile.
```

### VRAM budget verification

| Profile | planner | coder | chat | fim | embed | Total (worst case, all loaded) |
|---------|---------|-------|------|-----|-------|-------------------------------|
| 16gb | 17.3 | 8.4 | 8.4 | 3.4 | 0.6 | 38.1 GB (swaps; only fim+embed+1 big model active) |
| 24gb | 21.3 | 10.7 | 10.7 | 3.4 | 0.6 | 46.7 GB (swaps; only fim+embed+1 big model active) |
| 32gb | 24.2 | 15.0 | 15.0 | 3.4 | 0.6 | 58.2 GB (all pinned — only for 48+ GB cards) |

**Correction for 32gb:** Q8_0 coder+chat at 15 GB each plus planner at 24 GB = 54 GB just for
big models. This exceeds 32 GB VRAM. The 32 GB profile therefore **still swaps** the big models;
the benefit is Q8_0 quality, not zero-swap.

**Revised 32gb no-swap approach for 32 GB VRAM:** Use Q4_K_M planner (17.3 GB) + Q6_K coder (10.7 GB)
+ Q6_K chat (10.7 GB) + fim (3.4 GB) + embed (0.6 GB) = 42.7 GB — still too large.

**True no-swap is only possible at 48+ GB VRAM.** Revise the 32gb profile to be a
high-quality-quant profile (Q6_K throughout), not a no-swap profile. Document clearly.

```
32gb profile: planner Q5_K_M (21.3) + coder Q8_0 (15.0) + chat Q8_0 (15.0) + fim+embed (4.0)
= 55.3 GB total weights, ~20 GB active at any time (1 big + fim + embed + KV cache)
Swap group: planner/coder/chat swap as usual. Benefit: near-lossless quants.
```

---

## C2 — `--mlock` for Pinned Models

### Context
fim (3.4 GB) and embed (0.6 GB) are always resident. Under sustained load from VS Code autocomplete,
chat, and Open WebUI simultaneously, Windows may page these models to the pagefile — causing
multi-second "spikes" on autocomplete requests.

`--mlock` tells llama-server to pin model weights in physical RAM, preventing paging.

**Trade-off:** `--mlock` consumes 4 GB of physical RAM permanently for fim+embed.
On 32 GB+ system RAM, this is negligible. On 16 GB RAM, it locks 25% of RAM — document clearly.

### `config/models.psd1`

Add `mlock = $true` to fim and embed in all profiles:

```powershell
# In every profile's fim entry:
fim = @{
    ...
    mlock = $true    # Pin weights in physical RAM. Prevents paging spikes on autocomplete.
}

# In every profile's embed entry:
embed = @{
    ...
    mlock = $true    # Pin weights in physical RAM. 0.6 GB always locked.
}
```

### `scripts/gen-llama-swap.ps1`

Add inside the cmd-building loop, after `flags` are appended:

```powershell
if ($m.mlock -eq $true) {
    $parts += '--mlock'
}
```

### `docs/USAGE.md`

Add under the "Model Loading" section:

```
**Note on mlock:** fim and embed models are pinned with `--mlock` to prevent OS memory pressure
from causing latency spikes on autocomplete. This consumes 4 GB of physical RAM permanently.
On systems with less than 32 GB RAM, disable by setting `mlock = $false` in the model entries
(or set it per-profile in config/user.psd1 overrides — see Module A).
```

---

## C3 — Speculative Decoding (Experimental)

### Context
Speculative decoding uses a small "draft" model to propose N tokens at once, then verifies them
in parallel with the large model. When the draft is correct (which it is ~70-80% of the time for
coding tasks), the large model accepts all N tokens in one forward pass — effectively multiplying
tokens/second by 2-4× without quality loss.

**Critical constraint:** Draft and main model **must share the same tokenizer vocabulary**.
Tokens are proposed by the draft model and verified by the main model using their shared
vocabulary. If vocabularies differ, verification always rejects draft tokens → no speedup,
and results may be garbled.

**Safe pairings in this stack:**
| Main | Draft | Safe? | Reason |
|------|-------|-------|--------|
| Qwen2.5-Coder-14B (coder) | Qwen2.5-Coder-3B (fim) | ✓ YES | Same Qwen2.5 tokenizer |
| Qwen3-14B (chat) | Qwen2.5-Coder-3B (fim) | ✗ NO | Qwen3 has updated tokenizer |
| Qwen3-30B-A3B (planner) | Qwen2.5-Coder-3B (fim) | ✗ NO | Different tokenizer |

**Only coder → fim is safe** with the current model selection.

### `config/models.psd1`

Add `draftRole` to coder in all profiles that include both coder and fim:

```powershell
# In '16gb', '24gb', '32gb' coder entries:
coder = @{
    ...
    # Speculative decoding: Qwen2.5-Coder-3B (fim) shares the Qwen2.5 tokenizer.
    # Provides ~20-40% generation speedup with zero quality loss.
    # Only valid when fim is pinned in VRAM (it always is in this stack).
    draftRole = 'fim'
}
```

### `scripts/gen-llama-swap.ps1`

Add draft model resolution inside the cmd-building loop, after `flags` are processed:

```powershell
if ($m.draftRole) {
    # Find the draft model's gguf path
    $draftModel = $allModels | Where-Object { $_.role -eq $m.draftRole } | Select-Object -First 1
    if (-not $draftModel) {
        Write-Warning "[$($m.role)] draftRole '$($m.draftRole)' not found in profile — speculative decoding disabled."
    } elseif ($draftModel.pinned -ne $true) {
        Write-Warning "[$($m.role)] draftRole '$($m.draftRole)' is not pinned — speculative decoding requires draft model always in VRAM. Skipping."
    } else {
        $draftPath = '${env.LLAMA_LOCAL_ROOT}/models/' + $draftModel.gguf
        $parts += @("-md `"$draftPath`"", '-ngld 99')
    }
}
```

Note: `$allModels` = the full list of models from `Get-Models` (already available in generator scope).

### `docs/TUNING.md`

Add section:

```
## Speculative Decoding

Speculative decoding (`draftRole` in models.psd1) is enabled by default for the coder model.
It uses the always-resident fim model (Qwen2.5-Coder-3B) as a draft to propose tokens,
which the coder model (Qwen2.5-Coder-14B) verifies in parallel.

Expected speedup: 20–40% on generation-heavy tasks (autocomplete, code edits).
No quality change — only correctly predicted tokens are accepted.

**To disable:** Remove `draftRole` from the coder entry in models.psd1 and run `bob gen`.

**To verify it's active:** Run `bob bench`. Speculative decoding is active when the
`tg` (token generation) throughput is notably higher than your baseline.

**Tokenizer constraint:** Do NOT add `draftRole = 'fim'` to chat or planner — Qwen3 has
a different tokenizer from Qwen2.5. Mismatched tokenizers cause garbled output or no speedup.
```

---

## Verification

```powershell
# C1: Profile switch and model listing
bob profile 24gb
bob models          # Should show 24gb profile models
bob fetch --list    # Should list correct files for 24gb profile
bob profile 16gb    # Restore

# C2: mlock in generated YAML
bob gen
Select-String '--mlock' config/llama-swap.yaml
# Should show: fim and embed cmd strings contain --mlock

# C3: Speculative decoding in generated YAML
bob gen
Select-String '\-md ' config/llama-swap.yaml
# Should show: coder cmd contains -md .../qwen-coder-3b-q8_0.gguf -ngld 99
# planner and chat should NOT have -md

# Benchmark coder with spec decoding vs without:
bob bench
# Remove draftRole from coder, bob gen, bob bench again
# Compare tg128 numbers — expect 20-40% difference

# Full test suite
.\scripts\test-dry-run.ps1
```

## Files Modified

| File | Change |
|------|--------|
| `config/models.psd1` | Add 24gb and 32gb profiles; add `mlock` and `draftRole` fields |
| `scripts/gen-llama-swap.ps1` | Handle `mlock` flag, `draftRole` resolution |
| `docs/USAGE.md` | mlock trade-off note |
| `docs/TUNING.md` | Speculative decoding section |

## Estimated Effort

- C1 (profiles): 1 hour (mostly config writing + VRAM math verification)
- C2 (mlock): 30 min
- C3 (spec decoding): 2 hours (generator change + validation + docs)

Total: ~3.5 hours
