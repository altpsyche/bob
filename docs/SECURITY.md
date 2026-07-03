# Bob — Security Review (Module N / N9)

Scope: the agent tool surface and the `bob agent serve` HTTP server. Bob is a local-first,
single-operator assistant; the threat model is (1) a prompt-injected or misbehaving LLM abusing
its tools, and (2) exposure to other machines when the server is bound to `0.0.0.0`. Each claim
below names the test that backs it — run `tools\venv-litellm\Scripts\python.exe -m unittest discover -s tests`.

## Summary of guarantees

| Surface | Guarantee | Backed by |
|---------|-----------|-----------|
| Auth | Every endpoint except `/health` requires a valid bearer token (401 otherwise) | `test_server.test_auth_rejects_bad_token`, `test_completion_requires_auth`, `test_stream_requires_auth` |
| Ownership | A token only sees/modifies sessions its owner created; others 404 (no existence leak) | `test_server.test_owner_cannot_read_others_session_404`, `..._delete_...`, `..._complete_...`, `..._stream_...`, `test_unknown_and_unowned_are_indistinguishable` |
| `file_read`/`file_write` | Refuse paths outside `allowedReadPaths`/`allowedWritePaths` | `test_file.test_denies_outside_allowed_root` |
| Secrets denylist | `config.json`, `*.psd1`, `*.db`, `logs/`, `.env*` unreadable even inside an allowed root; the litellm key never leaks | `test_file.test_denies_config_json_and_hides_secret`, `..._psd1`, `..._db`, `..._env`, `..._logs_dir`, `test_write_refuses_secret_even_when_allowed` |
| `git_*` | Restricted to allow-listed repos (repo root + `gitAllowedRoots`); any other path refused | `test_git.test_outside_repo_denied`, `test_default_repo_allowed`, `test_extra_root_allowed` |
| `web_fetch` | http/https only; loopback/private/link-local blocked unless `allowPrivateFetch` (SSRF) | `test_web.*` |
| `shell_run` | Approval-gated at the loop choke point (fails closed with no approver); optional OS sandbox | `test_permissions.*`, `test_sandbox.*`, manual |
| Permission policy (O6) | Per-tool `allow\|ask\|deny` + per-owner / per-agent-depth overrides at the single dispatch choke point; empty policy == pre-O6 | `test_permissions.*` |
| Tool sandbox (O5) | `shell_run` runs under an OS backend when `agent.sandbox='on'` (deny-by-default FS on Linux; resource caps both OSes); fails closed if no backend | `test_sandbox.*` |
| Session store | Concurrent access is safe; no lost turns | `test_session_concurrency.*` |
| Cancellation | Client disconnect / Ctrl-C aborts an in-flight run; no bogus turn recorded | `test_server.test_stream_disconnect_stops_and_records_no_turn`, `test_agent_loop.test_cancel_*` |

## Tool-by-tool

### `file_read` / `file_write` ([scripts/tools/file.py](../scripts/tools/file.py))
- **Allowlist.** `file_read` returns `Access denied` for any path outside `agent.allowedReadPaths`
  (defaults to the repo root at runtime — [_models.ps1](../scripts/_models.ps1)). `file_write` is
  **disabled** unless `agent.allowedWritePaths` is set.
- **Secrets denylist (N9, OS-aware since NB3/C3).** Even inside an allowed root, `_is_denied_secret`
  refuses `config.json` (holds `litellmKey` + `apiTokens`), any `*.psd1` (config), any `*.db` (session
  / memory stores), anything under a `logs/` directory, and `.env*`. NB3 (contract C3) made it
  OS-aware: it also denies the resolved secrets file (`data/secrets.json`) and the platform secret
  dirs (`~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.config/bob`) on every OS. This closes the pre-N9 gap
  where the default repo-root allowlist exposed the proxy key and session DB to a prompt-injected
  read. Secrets themselves resolve through the seam — `osenv.secret()` (Python) / `Get-Secret`
  (PowerShell, NC1): env → OS keychain → `data/secrets.json` → default; never a git-tracked file. To
  read a legitimately-named-but-safe file that collides with the denylist, place it outside those
  patterns.

### `git_status` / `git_log` / `git_diff` ([scripts/tools/git.py](../scripts/tools/git.py))
- Read-only git subcommands. **Path allow-list (N9):** `_is_allowed_repo` restricts them to the
  Bob repo root plus `agent.gitAllowedRoots`; any other path returns `Access denied`. Before N9 a
  `path` argument could point at any repo on disk (info disclosure of unrelated history).

### `shell_run` ([scripts/tools/shell.py](../scripts/tools/shell.py))
- **Approval-gated (NE0 → O6).** `shell_run` sets `REQUIRES_APPROVAL=True`, so the agent loop asks its
  injected `approve` callback *before* dispatch (event-driven, not a blocking `input()` — it works
  under the TUI/server). The callback is **fail-closed**: no approver wired (server, cron, non-TTY)
  → the call is denied and runs nothing. O6 layers a config `allow|ask|deny` policy on top (see
  "Permission policy" below). 30s timeout; process killed on timeout. It is therefore **not** a
  remote-code-execution vector from the server.
- **Optional OS sandbox (O5).** When `agent.sandbox='on'`, the command runs under an OS confinement
  backend (see "OS sandbox" below) instead of directly. Default `off` reproduces the pre-O5 in-process
  run byte-for-byte.

### `web_search` / `web_fetch` ([scripts/tools/web.py](../scripts/tools/web.py))
- `web_fetch` allowlists the `http`/`https` schemes (blocks `file://`, `gopher://`, etc.) and
  blocks hosts that resolve to loopback / RFC-1918 private / link-local / reserved / multicast
  addresses (SSRF), unless `agent.allowPrivateFetch = $true`. `web_search` hits only the local
  SearXNG instance. Backed by [tests/test_web.py](../tests/test_web.py).

### `fabric_run` ([scripts/tools/fabric.py](../scripts/tools/fabric.py))
- Runs a **named** fabric pattern (`fabric --pattern <name>`) on piped input, 120s timeout. The
  pattern name is resolved and validated by fabric itself against its installed pattern set; there
  is no path/argument passthrough from the model, so there is no traversal or injection surface
  here beyond whatever patterns the operator installed. No code change in N9 — documented as
  accepted.

### `memory_recall` / `memory_store` ([scripts/tools/memory.py](../scripts/tools/memory.py))
- Operate only on the local `bob.db` via the embed server; no external egress. Disabled unless
  `memory.enabled`.

## Permission policy (O6) ([scripts/bob_permissions.py](../scripts/bob_permissions.py))
Authorization at the **single dispatch choke point** (`_dispatch_with_approval` in `bob_loop.py`, which
every tool call passes through). `PermissionPolicy.resolve(tool, owner, agent_depth, mutating)` returns
`allow | ask | deny`:
- **deny** — the call never dispatches; the model receives a clean refusal string it can react to.
- **ask** — emits an `approval_required` event and consults the fail-closed `approve` callback; also
  triggered by the NE0 floor (`agency='confirm'` or a tool's `REQUIRES_APPROVAL`), which the policy can
  tighten but never loosen.
- **allow** — dispatches.

Config (`config/defaults.json` → `runtime.agent.permissions`): `{read, mutating, tools:{}, perOwner:{},
perDepth:{}}`, each value `allow|ask|deny`; precedence per-depth → per-owner → top-level, per-tool over
class default. **An absent/empty `permissions` reproduces pre-O6 behavior exactly** (everything `allow`,
only the NE0 floor prompts, nothing denied). Every decision is written to an append-only audit line
(`[rid] AUDIT tool=… decision=… owner=… args_sha1=…`) on the `bob.agent` logger — arguments are
**hashed, never logged raw**, so secrets in args don't leak. Backed by `test_permissions.*`. Treat all
tool output as untrusted model input (prompt-injection posture); keep mutating tools behind `ask`.

## OS sandbox (O5) ([scripts/sandbox.py](../scripts/sandbox.py))
When `agent.sandbox='on'`, exec surfaces (`shell_run` today) run under an OS-native confinement backend
selected via `osenv`. Read-only tools stay in-process. **Default `off` reproduces today's behavior;**
when `on` with no usable backend, `run_sandboxed` **fails closed** (`SandboxUnavailable` → the tool
refuses) — a loud unsandboxed fallback is only ever chosen under `off`.

Config: `agent.sandbox` (`off|on`), `agent.sandboxLimits` = `{cpuSeconds, memoryMB, allowRoots:[],
network:false}`. `allowRoots` is the writable set (empty ⇒ only a tmpfs `/tmp` is writable — maximally
locked); `$HOME` is never in the bind set, so `~/.ssh`/secrets are absent from the sandbox namespace on
Linux even with a filesystem view.

**Per-OS backend matrix:**

| OS | Backend (preference order) | Filesystem | Resources | Network |
|----|----------------------------|-----------|-----------|---------|
| Linux | `bwrap` › `nsjail` | deny-by-default: RO system dirs, RW `allowRoots`, tmpfs `/tmp`, no `$HOME` | `RLIMIT_CPU` + `RLIMIT_AS` (preexec) | dropped unless `network:true` |
| Linux (fallback) | `unshare` + rlimit | **not confined** (rlimits + pid/net ns only) | rlimits | empty net ns |
| Windows | restricted token *(follow-up)* + **Job Object** | see caveat below | per-process memory cap, active-process cap, kill-on-close | (host) |
| macOS | *deferred* (`sandbox-exec`) | — | — | — |

Backed by `test_sandbox.*`: policy resolvers, backend selection (mocked), argv-builder shape (cross-OS),
and shell wiring run everywhere; real-confinement tests (write-outside-root denied, `~/.ssh` absent) are
`skipUnless(bwrap present)` and run where a backend exists (not gated in the per-PR CPU smoke).

**Residual / honest caveats:**
- The `unshare` fallback tier provides **resource limits and pid/net isolation only** — it does *not*
  confine the filesystem. The write-denial guarantee holds under `bwrap`/`nsjail`, not `unshare`.
- **Windows filesystem confinement is a tracked follow-up.** The current Windows backend delivers the
  *resource* guarantee (Job Object: memory/process caps + reliable process-tree teardown) but not full
  deny-by-default *filesystem* jailing — that needs a restricted token with restricting SIDs
  (Chromium-style) or an AppContainer, which must be validated live on Windows before it can be trusted.
  Until then the N9 secrets denylist remains the filesystem floor for `file_*` tools, and a sandboxed
  `shell_run` on Windows is resource-confined but not FS-jailed. Do not rely on the Windows sandbox for
  filesystem isolation yet.

## Auth + ownership ([scripts/bob_agent_server.py](../scripts/bob_agent_server.py))
- **Auth.** `_authed_owner` accepts a bearer token iff it is the litellm key or an `agent.apiTokens`
  entry, else **401**. `/health` is intentionally unauthenticated (returns only tool counts).
- **Ownership (N1).** Each token maps to an owner id (`agent.apiTokens` records `@{token;owner}`;
  the litellm key → `agent.defaultOwner`). Sessions are stamped with the creating owner; every
  session route resolves through `get_owned`/`delete_owned`, so another owner's `session_id`
  returns **404** — indistinguishable from an unknown id (no existence leak). Revocation = remove
  the token from config and restart `bob agent serve`.

## Exposing on `0.0.0.0` — checklist
`agent.serveHost` defaults to `127.0.0.1`. Before setting `0.0.0.0` (LAN/other machines):
1. Set strong, per-client `agent.apiTokens` with distinct owners — do **not** rely on the default
   `sk-local` litellm key. (Auth: 401 without a valid token; ownership: 404 across owners.)
2. Confirm the `file_read` secrets denylist is in force (N9) — the default repo-root allowlist
   would otherwise expose `config.json`. Narrow `allowedReadPaths` further if desired.
3. Leave `allowPrivateFetch = $false` so `web_fetch` can't be used to SSRF the host's private
   network from a LAN client.
4. Leave `allowedWritePaths` empty (or tightly scoped) — `file_write` is off by default.
5. Keep `gitAllowedRoots` empty unless a specific extra repo must be exposed.
6. Remember `shell_run` is inert on the server (no stdin) — no action needed.
7. Watch `logs/bob-agent.log`: every run carries a run-id (N5) so concurrent clients are
   distinguishable and any single run is greppable end-to-end.

## Known residual / accepted risks
- Token revocation requires a server restart (config is read once at startup) — acceptable for a
  single-operator local harness; documented, not a bug.
- `fabric_run` executes whatever patterns the operator installed; treat the fabric pattern library
  as trusted operator config.
- The secrets denylist is deliberately broad (all `*.psd1`/`*.db`, any `logs/`); a user who needs
  to read such a file via the agent must place it outside those patterns.
- **Denylist is name/path-based** (`Path.resolve()` — it follows symlinks/junctions and expands 8.3
  short names, but does *not* dereference NTFS **hardlinks**). An attacker who can create a hardlink
  to `config.json` under an allowed root with an innocuous name/suffix could read it via `file_read`.
  Reachability is low: `file_write` refuses the same secret patterns, and `shell_run` is
  confirmation-gated (inert on the server), so the agent has no built-in way to create such a link.
  Treat write access to an allowed root as trusted; do not expose the server on `0.0.0.0` while
  granting untrusted callers any file-creation capability inside an allowed root.
