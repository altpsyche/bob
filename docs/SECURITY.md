# Bob: Security Review

Scope: the agent tool surface and the `bob agent serve` HTTP server. Bob is a local-first,
single-operator assistant; the threat model is (1) a prompt-injected or misbehaving LLM abusing
its tools, and (2) exposure to other machines when the server is bound to `0.0.0.0`. Each claim
below names the test that backs it. Run the suite from the litellm venv:

```bash
# Linux
tools/venv-litellm/bin/python -m unittest discover -s tests
```
```bat
:: Windows
tools\venv-litellm\Scripts\python.exe -m unittest discover -s tests
```

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
| Permission policy | Per-tool `allow\|ask\|deny` + per-owner / per-agent-depth overrides at the single dispatch choke point; empty policy == pre-policy behavior | `test_permissions.*` |
| Tool sandbox | `shell_run` runs under an OS backend when `agent.sandbox='on'` (deny-by-default FS on Linux; resource caps both OSes); fails closed if no backend | `test_sandbox.*` |
| Session store | Concurrent access is safe; no lost turns | `test_session_concurrency.*` |
| Cancellation | Client disconnect / Ctrl-C aborts an in-flight run; no bogus turn recorded | `test_server.test_stream_disconnect_stops_and_records_no_turn`, `test_agent_loop.test_cancel_*` |

## Tool-by-tool

### `file_read` / `file_write` ([scripts/tools/file.py](../scripts/tools/file.py))
- **Allowlist.** `file_read` returns `Access denied` for any path outside `agent.allowedReadPaths`
  (defaults to the repo root at runtime, resolved from `config/defaults.json`). `file_write` is
  **disabled** unless `agent.allowedWritePaths` is set.
- **Secrets denylist (OS-aware).** Even inside an allowed root, `_is_denied_secret`
  refuses `config.json` (holds `litellmKey` + `apiTokens`), any `*.psd1` (config), any `*.db` (session
  / memory stores), anything under a `logs/` directory, and `.env*`. The osenv seam makes it
  OS-aware: it also denies the resolved secrets file (`data/secrets.json`) and the platform secret
  dirs (`~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.config/bob`) on every OS. This closes the gap where the
  default repo-root allowlist would otherwise expose the proxy key and session DB to a prompt-injected
  read. Secrets themselves resolve through the seam `osenv.secret()`:
  env → OS keychain → `data/secrets.json` → config default; never a git-tracked file. To
  read a legitimately-named-but-safe file that collides with the denylist, place it outside those
  patterns.

### `git_status` / `git_log` / `git_diff` ([scripts/tools/git.py](../scripts/tools/git.py))
- Read-only git subcommands. **Path allow-list:** `_is_allowed_repo` restricts them to the
  Bob repo root plus `agent.gitAllowedRoots`; any other path returns `Access denied`. Without this
  restriction a `path` argument could point at any repo on disk (info disclosure of unrelated history).

### `shell_run` ([scripts/tools/shell.py](../scripts/tools/shell.py))
- **Approval-gated.** `shell_run` sets `REQUIRES_APPROVAL=True`, so the agent loop asks its
  injected `approve` callback *before* dispatch (event-driven, not a blocking `input()`; it works
  under the TUI/server). The callback is **fail-closed**: no approver wired (server, cron, non-TTY)
  → the call is denied and runs nothing. A configurable permission policy layers `allow|ask|deny` on
  top (see "Permission policy" below). 30s timeout; process killed on timeout. It is therefore **not** a
  remote-code-execution vector from the server.
- **Optional OS sandbox.** When `agent.sandbox='on'`, the command runs under an OS confinement
  backend (see "OS sandbox" below) instead of directly. Default `off` reproduces the direct in-process
  run byte-for-byte.

### `web_search` / `web_fetch` ([scripts/tools/web.py](../scripts/tools/web.py))
- `web_fetch` allowlists the `http`/`https` schemes (blocks `file://`, `gopher://`, etc.) and
  blocks hosts that resolve to loopback / RFC-1918 private / link-local / reserved / multicast
  addresses (SSRF), unless `agent.allowPrivateFetch` is `true`. `web_search` hits only the local
  SearXNG instance. Backed by [tests/test_web.py](../tests/test_web.py).

### `fabric_run` ([scripts/tools/fabric.py](../scripts/tools/fabric.py))
- Runs a **named** fabric pattern (`fabric --pattern <name>`) on piped input, 120s timeout. The
  pattern name is resolved and validated by fabric itself against its installed pattern set; there
  is no path/argument passthrough from the model, so there is no traversal or injection surface
  here beyond whatever patterns the operator installed. No change here; documented as
  accepted.

### `memory_recall` / `memory_store` ([scripts/tools/memory.py](../scripts/tools/memory.py))
- Operate only on the local `bob.db` via the embed server; no external egress. Disabled unless
  `memory.enabled`.

## Permission policy ([scripts/bob_permissions.py](../scripts/bob_permissions.py))
Authorization at the **single dispatch choke point** (`_dispatch_with_approval` in `bob_loop.py`, which
every tool call passes through). `PermissionPolicy.resolve(tool, owner, agent_depth, mutating)` returns
`allow | ask | deny`:
- **deny**: the call never dispatches; the model receives a clean refusal string it can react to.
- **ask**: emits an `approval_required` event and consults the fail-closed `approve` callback; also
  triggered by the approval floor (`agency='confirm'` or a tool's `REQUIRES_APPROVAL`), which the policy can
  tighten but never loosen.
- **allow**: dispatches.

Config (`config/defaults.json` → `runtime.agent.permissions`): `{read, mutating, tools:{}, perOwner:{},
perDepth:{}}`, each value `allow|ask|deny`; precedence per-depth → per-owner → top-level, per-tool over
class default. **An absent/empty `permissions` reproduces the pre-policy behavior exactly** (everything `allow`,
only the approval floor prompts, nothing denied). Every decision is written to an append-only audit line
(`[rid] AUDIT tool=… decision=… owner=… args_sha1=…`) on the `bob.agent` logger; arguments are
**hashed, never logged raw**, so secrets in args don't leak. Backed by `test_permissions.*`. Treat all
tool output as untrusted model input (prompt-injection posture); keep mutating tools behind `ask`.

## OS sandbox ([scripts/sandbox.py](../scripts/sandbox.py))
When `agent.sandbox='on'`, exec surfaces (`shell_run` today) run under an OS-native confinement backend
selected via `osenv`. Read-only tools stay in-process. **Default `off` reproduces today's behavior;**
when `on` with no usable backend, `run_sandboxed` **fails closed** (`SandboxUnavailable` → the tool
refuses); a loud unsandboxed fallback is only ever chosen under `off`.

Config: `agent.sandbox` (`off|on`), `agent.sandboxLimits` = `{cpuSeconds, memoryMB, allowRoots:[],
network:false}`. `allowRoots` is the writable set (empty ⇒ only a tmpfs `/tmp` is writable, maximally
locked); `$HOME` is never in the bind set, so `~/.ssh`/secrets are absent from the sandbox namespace on
Linux even with a filesystem view.

**Per-OS backend matrix:**

| OS | Backend (preference order) | Filesystem | Resources | Network |
|----|----------------------------|-----------|-----------|---------|
| Linux | `bwrap` › `nsjail` | deny-by-default: RO system dirs, RW `allowRoots`, tmpfs `/tmp`, no `$HOME` | `RLIMIT_CPU` + `RLIMIT_AS` (preexec) | dropped unless `network:true` |
| Linux (fallback) | `unshare` + rlimit | **not confined** (rlimits + pid/net ns only) | rlimits | empty net ns |
| Windows | restricted token *(follow-up)* + **Job Object** | see caveat below | per-process memory cap, active-process cap, kill-on-close | (host) |
| macOS | *deferred* (`sandbox-exec`) | n/a | n/a | n/a |

Backed by `test_sandbox.*`: policy resolvers, backend selection (mocked), argv-builder shape (cross-OS),
and shell wiring run everywhere; real-confinement tests (write-outside-root denied, `~/.ssh` absent) are
`skipUnless(bwrap present)` and run where a backend exists (not gated in the per-PR CPU smoke).

**Residual / honest caveats:**
- The `unshare` fallback tier provides **resource limits and pid/net isolation only**: it does *not*
  confine the filesystem. The write-denial guarantee holds under `bwrap`/`nsjail`, not `unshare`.
- **Windows filesystem confinement is a tracked follow-up.** The current Windows backend delivers the
  *resource* guarantee (Job Object: memory/process caps + reliable process-tree teardown) but not full
  deny-by-default *filesystem* jailing: that needs a restricted token with restricting SIDs
  (Chromium-style) or an AppContainer, which must be validated live on Windows before it can be trusted.
  Until then the secrets denylist remains the filesystem floor for `file_*` tools, and a sandboxed
  `shell_run` on Windows is resource-confined but not FS-jailed. Do not rely on the Windows sandbox for
  filesystem isolation yet.

## Auth + ownership ([scripts/bob_agent_server.py](../scripts/bob_agent_server.py))
- **Auth.** `_authed_owner` accepts a bearer token iff it is the litellm key or an `agent.apiTokens`
  entry, else **401**. `/health` is intentionally unauthenticated (returns only tool counts).
- **Ownership.** Each token maps to an owner id (`agent.apiTokens` records `@{token;owner}`;
  the litellm key → `agent.defaultOwner`). Sessions are stamped with the creating owner; every
  session route resolves through `get_owned`/`delete_owned`, so another owner's `session_id`
  returns **404**, indistinguishable from an unknown id (no existence leak). Revocation = remove
  the token from config and restart `bob agent serve`.

## Exposing on `0.0.0.0`: checklist
`agent.serveHost` defaults to `127.0.0.1`. Before setting `0.0.0.0` (LAN/other machines):
1. Set strong, per-client `agent.apiTokens` with distinct owners; do **not** rely on the default
   `sk-local` litellm key. (Auth: 401 without a valid token; ownership: 404 across owners.)
2. Confirm the `file_read` secrets denylist is in force; the default repo-root allowlist
   would otherwise expose `config.json`. Narrow `allowedReadPaths` further if desired.
3. Leave `allowPrivateFetch` at `false` so `web_fetch` can't be used to SSRF the host's private
   network from a LAN client.
4. Leave `allowedWritePaths` empty (or tightly scoped); `file_write` is off by default.
5. Keep `gitAllowedRoots` empty unless a specific extra repo must be exposed.
6. Remember `shell_run` is inert on the server (no stdin); no action needed.
7. Watch `logs/bob-agent.log`: every run carries a run-id so concurrent clients are
   distinguishable and any single run is greppable end-to-end.

## Autonomy dial

Bob grants autonomy in escalating steps, each a separate, explicit opt-in. A bigger grant is never
implied by a smaller one:

1. **One-shot** (`bob agent "..."`): a single bounded run, foreground, no persistence.
2. **`--deep`**: plan/verify/self-repair phases and a larger step budget; still foreground.
3. **Durable run** (`agent.checkpoint`): run state persists so a run can resume across a restart. Off by
   default. Resume restores reasoning state, not world state (filesystem changes are not undone).
4. **Detached task** (`bob task start`): runs in a background worker that survives the client
   disconnecting. Owner-scoped; still governed by the permission policy; a detached run is fail-closed on
   approval (no interactive approver -> approval-gated tools are denied).
5. **Computer-use** (`agent.computerUse`): drives the screen and input devices. The largest grant, off by
   default, always approval-gated, and never available in an unattended/detached run without an explicit
   opt-in (see below).

Each rung requires its own configuration or flag. Nothing above one-shot is on by default.

## Computer-use ([scripts/tools/computer.py](../scripts/tools/computer.py))

Computer-use lets the agent take a screenshot and drive mouse/keyboard input. It is the most powerful and
most dangerous capability in Bob, so it is gated hardest.

**Threat model.** A screenshot is untrusted, model-controlled input: text rendered on screen (a web page,
a chat message, a crafted image) can carry instructions that attempt to redirect the agent (prompt
injection into GUI actions). A successful injection can click, type, and read whatever the logged-in user
can. Two specific surfaces:
- **Screenshot as an injection surface.** A captured frame is fed back to the model through the
  `{"__images__": [...]}` tool-result contract and routed to the vision role. On-screen text must never be
  allowed to silently expand the task, widen any allowlist, or relax the approval posture.
- **Screenshot as an exfiltration surface.** A screenshot can capture secrets on screen (tokens, private
  messages). Capture is therefore approval-gated even though it is a read.

**Gating chain** (every computer-use action passes through all of it):
default-off (`agent.computerUse.enabled`) -> the tool is not even offered unless enabled -> every action
requires approval (`REQUIRES_APPROVAL`, so `_resolve_approval` is fail-closed: no approver means deny) ->
the permission policy still applies -> every decision is audited (args hashed) -> a kill switch can halt
all computer-use out of band -> a per-minute rate limit bounds action volume -> a detached/unattended run
cannot use computer-use without an explicit opt-in.

**Isolation posture.** Frontier computer-use references run in a disposable VM or container with a
*virtual* display, a network allowlist, and no logged-in accounts. Bob supports a virtual-display target
(`agent.computerUse.display: "virtual"`, an Xvfb/nested display) as the recommended default; it shrinks
the blast radius of a successful injection and sidesteps the Wayland synthetic-input block. Driving the
real logged-in desktop (`display: "host"`) is the highest-risk configuration and is an explicit, louder
opt-in. Human confirmation on every action is the load-bearing defense: Bob has no server-side screenshot
injection classifier unless a run is routed through a hosted computer-use API.

**Accepted limitation.** GUI input needs the host display and input bus, which the deny-by-default OS
sandbox (no `$HOME`, unshared network) cannot provide, so computer-use input does not route through the
sandbox. Its controls are the approval floor, the audit log, the kill switch, and (preferably) a virtual
display, not the OS sandbox.

**Detached-task interaction.** A scheduled or detached run is already fail-closed on approval. The
`--allow-computer` opt-in (`agent.computerUse.allowUnattended`) is required before computer-use is even
loaded in such a run: defense-in-depth so an unattended agent cannot drive the desktop by default.

**Test-backed guarantees.** Each control is pinned by a behavior test (all hermetic; no real input is
injected and no live model is used):
- Default-off (not offered unless enabled): `test_screenshot_tool_absent_when_computer_use_off`
  ([tests/test_computer_use.py](../tests/test_computer_use.py)).
- Every action approval-gated (`REQUIRES_APPROVAL`) and inputs declared mutating:
  `test_screenshot_requires_approval`, `test_click_declared_mutating`.
- Denial at the prompt blocks the action; approval runs it:
  `test_computer_use_gating_fixtures_pass` ([tests/test_safety_eval.py](../tests/test_safety_eval.py),
  over the `computer_use_*` eval fixtures).
- Coordinate mapping is correct (a click lands where the model intended after downscaling):
  `test_click_maps_model_coords_to_screen_before_backend`, `test_scale_roundtrip_within_rounding`.
- Kill switch halts all actions out of band: `test_halt_sentinel_blocks_action`.
- Per-minute rate limit: `test_rate_limit_refuses_past_budget`.
- Detached/unattended interlock: `test_computer_use_absent_in_unattended_run_without_optin`,
  `test_task_runner_marks_unattended`.
- Screenshot flows through the untrusted-image -> vision-role path via the `{"__images__": [...]}`
  contract: `test_screenshot_returns_image_contract_and_records_scale`, plus the loop's image-split
  coverage in [tests/test_vision.py](../tests/test_vision.py).
- Graceful degradation when no input/capture backend is present:
  `test_input_raises_when_no_backend`, `test_screenshot_degrades_when_no_capture_backend`,
  `test_input_unavailable_degrades_gracefully`.

## Known residual / accepted risks
- Token revocation requires a server restart (config is read once at startup), acceptable for a
  single-operator local harness; documented, not a bug.
- `fabric_run` executes whatever patterns the operator installed; treat the fabric pattern library
  as trusted operator config.
- The secrets denylist is deliberately broad (all `*.psd1`/`*.db`, any `logs/`); a user who needs
  to read such a file via the agent must place it outside those patterns.
- **Denylist is name/path-based** (`Path.resolve()`: it follows symlinks/junctions and expands 8.3
  short names, but does *not* dereference NTFS **hardlinks**). An attacker who can create a hardlink
  to `config.json` under an allowed root with an innocuous name/suffix could read it via `file_read`.
  Reachability is low: `file_write` refuses the same secret patterns, and `shell_run` is
  confirmation-gated (inert on the server), so the agent has no built-in way to create such a link.
  Treat write access to an allowed root as trusted; do not expose the server on `0.0.0.0` while
  granting untrusted callers any file-creation capability inside an allowed root.
