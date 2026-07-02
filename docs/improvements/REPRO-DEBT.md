# Reproducibility debt (single-source-of-truth audit)

> **✅ RESOLVED (2026-07-02).** All CONFIRMED items (1–6) and both ACKNOWLEDGED items (7 NB7, 8 continue
> generator) are fixed — see the per-item status below and the "Resolution" section at the end. Ports
> and the runtime default layer are now single-sourced through `config/defaults.json`; `litellm.yaml`
> and `continue/config.yaml` are generated + gitignored; the litellm key/timeout and CUDA path route
> through their seams. Permanent gates: `test_defaults_parity` (no shadow ports, runtime parity),
> `test-dry-run.ps1 [15]` (generators single-source). The external-client `sk-local` single-sourcing
> remains an explicit non-goal (LAN-hardening follow-up).

Status: recorded by Module ND (2026-07-02), **now fixed** (see Resolution). ND establishes the "generate + gate"
discipline (`versions.lock` ← `bob lock`, gated by `check.ps1`; the existing `verbs.json` ← `registry.py`
is the reference pattern). This audit found pre-existing violations of that same discipline in committed
modules A–NC. Per the contracts rule — *a contract change lands in
[ARCHITECTURE-CONTRACTS.md](ARCHITECTURE-CONTRACTS.md) / the owning module first, never silently in
another module* — ND records them for their owners rather than fixing them out-of-band.

The anti-pattern: **data duplicated or hand-maintained that should live in one source (or be generated
from it), with no `--check` gate to catch drift.** Ranked CONFIRMED (real drift risk, no guard) /
ACKNOWLEDGED (already has a TODO) / FALSE-ALARM (actually reads the seam).

## CONFIRMED — real drift risk, no guard

| # | Finding | Evidence (file:line) | Single source | Owner |
|---|---|---|---|---|
| 1 | The whole **ports block is re-declared** in `models.psd1` (`port/webuiPort/litellmPort/langfusePort/searxngPort/n8nPort`). On Windows `Get-BobConfig` reads these first, so `models.psd1` is *authoritative* and `defaults.json` is only the fallback; Python reads `defaults.json`. Nothing asserts the two agree → silent Windows/Linux port drift. `test_defaults_parity.py` only proves both langs read `defaults.json`, never that `models.psd1` matches it. | [config/models.psd1:81-86](../../config/models.psd1#L81) vs [config/defaults.json:4-14](../../config/defaults.json#L4) | `config/defaults.json` `ports` (contract C2) | **NB1/NB7** |
| 2 | `sttPort`/`ttsPort`/`agentPort` **hand-duplicated** in `bob.psd1` (voice/agent sections) — a second authoritative copy of ports also in `defaults.json`. | [config/bob.psd1:41-42](../../config/bob.psd1#L41), [config/bob.psd1:78](../../config/bob.psd1#L78) | `config/defaults.json` `ports` | **NB1/NB7** |
| 3 | **`litellm.yaml` is generated-but-committed with no gate — and has already drifted.** Committed yaml has `max_budget: 15` / `budget_duration: "30d"` but the committed `models.psd1` deepseek peer defines no `budget`, and the generator only emits `max_budget` when a peer has `budget > 0`. So the committed artifact was regenerated from an *uncommitted* `user.psd1`. (Contrast: `llama-swap.yaml` is generated **and gitignored**, so it can't drift.) | [config/litellm.yaml:69-70](../../config/litellm.yaml#L69), generator [scripts/gen-litellm.ps1:79](../../scripts/gen-litellm.ps1#L79) | `config/models.psd1` (via `gen-litellm.ps1`) | **Module E/M** |
| 4 | `master_key: sk-local` / `api_key: sk-local` **hardcoded** in the litellm generator instead of derived from `litellmKey` (secret seam). Contradicts the documented LAN-hardening flow ("change `litellmKey` and update every client"). | [scripts/gen-litellm.ps1:30](../../scripts/gen-litellm.ps1#L30), [scripts/gen-litellm.ps1:100](../../scripts/gen-litellm.ps1#L100) | `litellmKey` via `Get-Secret`/`osenv.secret` (contract C3) | **secrets/C3** |
| 5 | `request_timeout: 600` **re-inlined** in the litellm generator, duplicating `agent.requestTimeout = 600` (CONTRIBUTING §6 couples them, yet the litellm value is a literal). | [scripts/gen-litellm.ps1:76](../../scripts/gen-litellm.ps1#L76) vs [config/defaults.json:52](../../config/defaults.json#L52) | `defaults.json` `agent.requestTimeout` | **Module E/M** |
| 6 | CUDA base path `C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA[\v12.8]` **re-inlined**, bypassing the NC1 seam `Get-CudaRoot`. | [scripts/diagnose.ps1:56](../../scripts/diagnose.ps1#L56), [scripts/install-prereqs.ps1:155](../../scripts/install-prereqs.ps1#L155) | `_platform.ps1 Get-CudaRoot` (NC1) | **NC** |

## ACKNOWLEDGED — already has a TODO / sync comment

| # | Finding | Evidence | Note |
|---|---|---|---|
| 7 | `bob.psd1` **duplicates the `defaults.json` `runtime` layer** (persona.systemPrompt, memory{}, vision{}, agent{} — ~25 keys). `dbPath` even diverges by separator (`data/bob.db` vs `data\bob.db`). | [config/defaults.json:2](../../config/defaults.json#L2) (comment) + [MODULE-NB-portability-foundation.md](MODULE-NB-portability-foundation.md) | The **NB7** unification target; explicitly deferred. |
| 8 | `continue/config.yaml` **hand-mirrors** prompts + `contextLength` from `models.psd1` (already stale: coder `32768` vs active profile `16384`). | [config/models.psd1:94](../../config/models.psd1#L94) (comment) | Client config, "not auto-generated" per the comment. |

## FALSE-ALARM — actually reads the seam (no action)

- **Role routing** — all via `Get-RoleForTask` / `get_role`; literals only in test fixtures.
- **Model repo/filename/size** — all via `Get-Models`; no repo URL/filename/size retyped in `.ps1`/`.py`.
- SearXNG's `0.0.0.0:8080` — the container-internal port, not Bob's llama-swap `:8080`.

## Resolution (2026-07-02)

Fixed in one pass (contract-first: C2/NB7 doc updates landed before the code). Each item cites the change.

| # | Fix | Where |
|---|-----|-------|
| 1, 2 | **Ports single-sourced to `defaults.json.ports`.** Deleted the 6 literals from `models.psd1.defaults` and the 3 from `bob.psd1` (`sttPort`/`ttsPort`/`agentPort`); every reader now resolves via `?? (Get-BobPortDefault …)`. Permanent gate: `test_defaults_parity.test_no_shadow_port_literals_in_psd1` fails on any reintroduced shadow copy (incl. `bob.psd1.voice`). | `config/models.psd1`, `config/bob.psd1`, ~20 `scripts/*.ps1` readers, `tests/test_defaults_parity.py` |
| 3 | **`litellm.yaml` generated + gitignored** (like `llama-swap.yaml`) — `git rm --cached` + `/config/litellm.yaml` in `.gitignore`; regenerated on `bob gen`. Drift now impossible. | `.gitignore`, `scripts/gen-litellm.ps1` |
| 4 | **`api_key`/`master_key` derive from the `litellmKey` seam** (`Get-Secret … -Default ((Get-BobConfig).litellmKey ?? 'sk-local')`); default unchanged, no client breaks. | `scripts/gen-litellm.ps1:16,32,102` |
| 5 | **`request_timeout` derives from `agent.requestTimeout`** (no re-inlined `600`). | `scripts/gen-litellm.ps1:76` |
| 6 | **CUDA path routes through the NC1 seam** — `Resolve-CudaRootCandidates.Base` for enumeration, `Get-CudaRoot -CudaArch 120` for the pin check. | `scripts/diagnose.ps1:56`, `scripts/install-prereqs.ps1:155` |
| 7 | **NB7 Option A: runtime defaults unified.** `Get-BobConfig` seeds `$base` from `defaults.json.runtime` and deep-merges `bob.psd1` (a new `Merge-BobHashtable` mirrors the Python `_deep_merge`); ~25 duplicated keys removed from `bob.psd1` (now only `persona.name/style`, `routing`, `voice`, `agent.toastAppId`). Path separators normalize to `/` from the neutral file. Gates: `test_defaults_parity` runtime-parity + persona-deep-merge. | `scripts/_models.ps1`, `config/bob.psd1`, `docs/improvements/ARCHITECTURE-CONTRACTS.md` (C2), `docs/improvements/MODULE-NB-portability-foundation.md` (NB7) |
| 8 | **`continue/config.yaml` generated + gitignored.** New `gen-continue.ps1` emits `models:` from `Get-Models` (role→model, profile `ctx`→`contextLength`, prompts→`systemMessage`, apiBase from litellmPort, apiKey from the seam) + a **templated** `mcpServers` block (repo root + `$HOME/dev`, `${GITHUB_TOKEN}`) — no committed personal path. Wired into `bob gen` + `setup-clients.ps1`. | `scripts/gen-continue.ps1`, `scripts/bob.ps1`, `scripts/setup-clients.ps1`, `.gitignore`, `config/models.psd1` (stale sync comment removed) |

**Non-goal (unchanged):** full external-client `sk-local` single-sourcing (continue/aider/n8n/up.ps1) — the
documented LAN-hardening follow-up; and the neutral TOML authoring format (NB7 Option B, still deferred).
