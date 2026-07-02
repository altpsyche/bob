# Module J — Low-VRAM Inference Maximization

**Target:** RTX 5080 (16 GB VRAM), Ryzen 9 7950X3D, 64 GB RAM running Qwen3-30B-A3B (17.3 GB Q4_K_M).

**Problem:** The 30B-A3B planner overflows VRAM by ~1.3 GB, requiring CPU layer offload. No RAM-preload or memory-locking strategy was in place. Several performance-relevant llama.cpp flags were not exposed in the config.

**Goal:** Maximize context capacity and inference speed through smarter inference flags and a more controllable configuration surface. No model changes, no recompilation.

**Note on KV quantization defaults (J1):** The original J1 design used asymmetric q5_1/q4_0. Benchmarking on Blackwell (sm_120, RTX 50 series) revealed a 15x prompt-processing regression when sub-q8_0 types combine with flash attention. Default changed to q8_0/q8_0 — safe on all GPU generations, ~50% KV VRAM savings vs f16. Pre-Blackwell GPUs (sm_75–89, RTX 20/30/40) can use q5_1/q4_0 for greater savings; see user.psd1.example.

---

## Flag Audit

| Requested | Correct Flag | Outcome |
|-----------|-------------|---------|
| `--turbo-key` | `--cache-type-k` | Already emitted; J1 adds asymmetric K/V control |
| `--turbo-val` | `--cache-type-v` | Same |
| `-n-cpu-moe` | **Does not exist** | MoE offloading via `-ngl`; see J6 |
| `--no-mmap` | `--no-mmap` | Valid, Windows-compatible. Added in J2 |
| `--mlock` | `--mlock` | Valid. Was pinned-models-only. Extended in J3 |
| `--defrag-thold` | ~~`--defrag-thold`~~ | **Deprecated in current build — no effect. Omitted.** |

---

## Sub-modules

| ID | Name | Files changed |
|----|------|--------------|
| J1 | Asymmetric KV cache quantization | `models.psd1`, `gen-llama-swap.ps1`, `TUNING.md` |
| J2 | `--no-mmap` per-model RAM preload | `models.psd1`, `gen-llama-swap.ps1`, `TUNING.md` |
| J3 | Extended `--mlock` for swap models | `models.psd1`, `gen-llama-swap.ps1`, `TUNING.md`, `grant-mlock.ps1` (new), `llm.ps1`, `diagnose.ps1` |
| J4 | Physical batch size (`-ub`) exposure | `models.psd1`, `gen-llama-swap.ps1`, `TUNING.md` |
| J5 | NUMA strategy for 7950X3D | `models.psd1`, `gen-llama-swap.ps1`, `TUNING.md` |
| J6 | MoE layer offloading documentation | `TUNING.md` |
| J7 | `user.psd1.example` with all J knobs | `user.psd1.example`, `TUNING.md` |

---

## J1 — Asymmetric KV Cache Quantization

### What changed

Replaced single `kvQuant = 'q8_0'` with independent K and V controls:
- `kvQuantK = 'q8_0'` — key cache quant (default; safe on all GPU generations)
- `kvQuantV = 'q8_0'` — value cache quant (default; ~50% VRAM savings vs f16)
- `kvQuant = ''` — legacy override; when non-empty, overrides both K and V

`gen-llama-swap.ps1` now resolves K and V independently, falling back to `kvQuant` for backward compatibility.

### Impact

At ctx=16384 for Qwen2.5-Coder-14B: KV VRAM drops from ~2.7 GB (f16) to ~1.3 GB (q8_0) — ~50% savings, allowing either longer context or more planner layers on GPU.

Pre-Blackwell GPUs (sm_75–89, RTX 20/30/40) can use q5_1/q4_0 for greater savings (~75% vs f16, ~0.75 GB at ctx=16384). Override in user.psd1:

```powershell
@{ defaults = @{ kvQuantK = 'q5_1'; kvQuantV = 'q4_0' } }
```

On Blackwell (sm_120+, RTX 50 series), q5_1/q4_0 combined with flash attention causes a 15× prompt-processing regression. Keep the q8_0 default on these GPUs.

Valid KV types: `f16`, `bf16`, `q8_0`, `q5_1`, `q5_0`, `q4_1`, `q4_0`, `iq4_nl`.

---

## J2 — `--no-mmap` per-Model RAM Preload

### What changed

- `defaults.noMmap = $false` added (global opt-in)
- 16 GB profile planner has `noMmap = $true` (overrides global default)
- Generator resolves per-model `noMmap`, falls back to global; emits `--no-mmap` when true

### Impact

Planner model (17.3 GB) is loaded fully into heap RAM at startup. CPU-offloaded layers are accessed from RAM not disk — no page faults during inference. Startup time increases ~15–20 s for the planner.

Verified: `--no-mmap` uses Windows file API (`SetFilePointerEx` + `ReadFile`), not POSIX mmap.

### Disable

```powershell
# config/user.psd1
@{ profiles = @{ '16gb' = @{ planner = @{ noMmap = $false } } } }
```

---

## J3 — Extended `--mlock` for Swap Models

### What changed

- `defaults.mlockBig = $false` added
- Generator: when `mlockBig = $true`, applies `--mlock` to all swap-group members (planner/coder/chat)
- Existing per-model `mlock = $true` (fim, embed) still works unchanged

### Impact

Combined with J2, fully pins model weights in physical RAM. Prevents Windows pagefile eviction of CPU-resident model pages under memory pressure.

Windows: `SeLockMemoryPrivilege` required. Without it: llama-server warns and continues without locking.

**Automated grant via `scripts/grant-mlock.ps1`:**
- `-Check` mode: exports `USER_RIGHTS` via `secedit /export`, inspects current user SID in `SeLockMemoryPrivilege` line; exits 0 (granted) or 1 (not granted). No admin required.
- Grant mode: self-elevates via `ProcessStartInfo { Verb='RunAs' }` if not admin; appends `*$sid` to the policy line (or inserts it if absent); applies with `secedit /configure /areas USER_RIGHTS`. Restart terminal after.

`bob mlock` in `scripts/llm.ps1` wraps this: checks status, explains the privilege, prompts `[y/N]`, then runs `grant-mlock.ps1`. `bob diagnose` also reports mlock privilege status and flags a warning when `mlockBig=true` but privilege is not granted.

### Enable

```powershell
# config/user.psd1
@{ defaults = @{ mlockBig = $true } }

# Then grant the Windows privilege (one-time, per machine):
bob mlock
# Restart terminal, then: bob serve
```

---

## J4 — Physical Batch Size (`-ub`)

### What changed

- `defaults.ubatch = 512` added (documents the llama.cpp default of 512)
- Generator emits `-ub $ubatch` when value != 512

### Impact

Setting `ubatch = 1024` or `2048` reduces GPU kernel-launch overhead during long-prompt prefill. Uses more peak VRAM during the kernel dispatch. No quality impact.

Note: `defaults.batch = 512` emits no `-b` flag (512 is the skip threshold, not the running value — llama.cpp then defaults to 2048). This pre-existing quirk is documented in the config comment.

---

## J5 — NUMA Strategy

### What changed

- `defaults.numa = ''` added (disabled by default)
- Generator emits `--numa $numa` when non-empty, placed in the srv macro

### Impact

On the 7950X3D (2 NUMA nodes: CCD0=V-Cache 96 MB, CCD1=32 MB), `--numa isolate` pins inference threads to the starting NUMA node. When the process starts on CCD0, this keeps CPU-offloaded layer computations on the V-Cache CCD, improving data locality.

Benchmark with `bob bench` to compare `'isolate'` vs `'distribute'` vs `''`.

### Enable

```powershell
# config/user.psd1
@{ defaults = @{ numa = 'isolate' } }
```

---

## J7 — `user.psd1.example` and TUNING.md

`config/user.psd1.example` fully rewritten with all J-module knobs grouped by section:
- **KV cache (J1):** `kvQuantK`, `kvQuantV`, `kvQuant` (legacy) with inline type options and revert guidance
- **RAM preload (J2):** `noMmap` with trade-off note (startup time vs inference consistency)
- **Memory lock (J3):** `mlockBig` with Windows privilege note and `bob mlock` reference
- **Physical batch (J4):** `ubatch` with VRAM cost note
- **NUMA (J5):** `numa` with `isolate`/`distribute` options and 7950X3D guidance
- **MoE ngl tuning (J6):** commented note pointing to per-model `flags` override in profiles block

`TUNING.md` defaults table updated: `kvQuant` row split into `kvQuantK` / `kvQuantV` / `kvQuant`, with the legacy pattern documented.

---

## J6 — MoE Layer Offloading (Documentation Only)

`-n-cpu-moe` does not exist as a server flag. MoE CPU offloading is controlled via `-ngl`.

At 16 GB VRAM with fim (3.4 GB) + embed (0.6 GB) + KV (~0.75 GB after J1), ~11 GB remains for planner weights. At 17.3 GB / 48 layers ≈ 0.36 GB/layer, ~30–31 layers fit on GPU.

Explicit split via `user.psd1`:

```powershell
@{
    profiles = @{
        '16gb' = @{
            planner = @{ flags = @('--temp', '0.3', '-ngl', '31') }
        }
    }
}
```

See TUNING.md for full MoE offloading guidance.

---

## Verification

```powershell
# Regenerate and inspect
bob gen
cat config\llama-swap.yaml

# Verify asymmetric KV in kv macro
Select-String 'cache-type' config\llama-swap.yaml
# Expected: --cache-type-k q8_0 --cache-type-v q8_0

# Verify --no-mmap on planner only
Select-String 'no-mmap' config\llama-swap.yaml
# Expected: planner cmd contains --no-mmap; coder, chat, fim, embed do not

# mlock on fim and embed; NOT on swap models (mlockBig = $false by default)
Select-String '\-\-mlock' config\llama-swap.yaml

# Dry-run (no model download needed)
.\scripts\test-dry-run.ps1

# Benchmark before/after enabling J2+J3+J5
bob serve
bob bench   # compare pp512 and tg128 vs baseline
```

## Files Modified

| File | Change |
|------|--------|
| `config/models.psd1` | Add `kvQuantK`, `kvQuantV`, `kvQuant=''`, `ubatch`, `noMmap`, `mlockBig`, `numa` to defaults; `noMmap = $true` on 16gb planner; updated `batch` comment |
| `scripts/gen-llama-swap.ps1` | Asymmetric KV resolution; `-ub` and `--numa` in srv macro; extended mlock logic; `--no-mmap` per-model |
| `config/user.psd1.example` | All J-module knobs with section headings and examples |
| `docs/TUNING.md` | Updated defaults table; KV quant, RAM preload, mlock, NUMA, MoE sections |
| `scripts/grant-mlock.ps1` | New script: `-Check` reports privilege status; grant mode self-elevates, modifies security policy via `secedit`, applies `SeLockMemoryPrivilege` |
| `scripts/llm.ps1` | Added `bob mlock` command: checks status, prompts, runs `grant-mlock.ps1` |
| `scripts/diagnose.ps1` | Added mlock privilege check row; yellow warning when `mlockBig=true` but privilege not granted |
| `docs/SETUP.md` | Added `bob mlock` to verification steps; documented `SeLockMemoryPrivilege` requirement |
| `docs/USAGE.md` | Added `bob mlock` to Tools section; expanded mlock paragraph with `mlockBig` and `bob mlock` reference |

---

## Outcome

**Status: ✓ Closed**

Implemented, benchmarked, and corrected on RTX 5080 / Blackwell (sm_120).

### Measured results (Qwen2.5-Coder-14B Q4_K_M, RTX 5080)

Flash-attn and q8_0/q8_0 KV quant were already active before MODULE J on this machine. The relevant comparison is against the broken J1 config that shipped:

| Config | pp512 t/s | tg128 t/s |
|--------|-----------|-----------|
| Pre-J baseline (fa=1, KV q8_0/q8_0) | ~4600 | ~89 |
| J as-shipped (fa=1, KV q5_1/q4_0) | **307** | **59** |
| **J corrected (fa=1, KV q8_0/q8_0)** | **~4600** | **~89** |

J1's q5_1/q4_0 KV quant caused a **15× prompt-processing regression on Blackwell** — flash-attn combined with sub-q8_0 types has no optimized kernel on sm_120. Correcting the default back to q8_0/q8_0 restored performance to the pre-J baseline.

**Speed contribution of MODULE J on RTX 5080: zero.** Flash-attn and q8_0 were pre-existing. J's value on this hardware is reliability and correctness, not throughput.

### What landed

- **mlockBig now works** — Bug 1 caused `mlockBig` to silently never apply `--mlock` to swap models since initial implementation. Fixed.
- **noMmap on planner** — new. Loads 17 GB into heap RAM at startup; CPU-offloaded layers have zero disk I/O during inference.
- **SeLockMemoryPrivilege** automated end-to-end via `bob mlock` + `grant-mlock.ps1`
- **9 bugs fixed** — UAC crash, temp file leaks, profile merge silently ignored, dead conditions, stale docs
- **2× context for coder and chat** — 32768 tokens (was 16384). The VRAM headroom existed before J; we applied it. Straightforward `ctx` change.
- **Blackwell KV quant regression documented** — RTX 20/30/40 can use q5_1/q4_0 for ~75% KV savings; RTX 50 (sm_120) must keep q8_0 with flash-attn.

### What was tested and found neutral / reverted

- **ubatch=1024**: neutral at pp512, ~3% slower at pp2048 on RTX 5080. Default (512) retained. Knob is exposed for users to benchmark on their own hardware.
- **NUMA isolate**: AM5 / Windows exposes a single NUMA node; `--numa` is a no-op on this platform.
