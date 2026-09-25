# Bob: Security Review

Scope: the agent tool surface, the `bob agent serve` HTTP server, the MCP server, and the network
services Bob starts. Bob is a local-first, single-operator assistant. The threat model is (1) a
prompt-injected or misbehaving LLM abusing its tools, and (2) exposure to other machines when a service
binds to `0.0.0.0`. Each claim
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
| Network bind | Every service binds top-level `bindHost` (default `127.0.0.1`), except the voice servers, which have no auth and bind `voiceBindHost` (default `127.0.0.1`); llama-swap always stays on loopback; compose ports publish on `${BIND_HOST}` | `test_stack.test_litellm_binds_loopback_by_default`, `test_bind_host_opts_into_lan`, `test_llama_swap_stays_on_loopback_even_with_lan`, `test_voice_servers_stay_on_loopback_when_bind_host_opens_the_lan`, `test_voice_bind_host_opts_them_in`, `test_no_literal_secrets_and_every_port_on_bind_host` |
| Generated secrets | The LiteLLM key and every service secret are generated on first use into `data/secrets.json` (0600); no fixed default key exists | `test_osenv.test_generated_once_then_stable`, `test_core_routing.test_generated_when_unset_and_stable`, `test_generate.test_no_output_carries_the_old_fixed_key` |
| Auth | Every endpoint except `/health` requires a valid bearer token (401 otherwise) | `test_server.test_auth_rejects_bad_token`, `test_completion_requires_auth`, `test_stream_requires_auth` |
| One approval gate | The loop, the shell, skill steps, `bob --run` and MCP all dispatch through `bob_permissions.dispatch_with_approval`; no approver means deny | `test_permissions.*`, `test_mcp.test_approval_required_tool_refused` |
| MCP | Approval-required and state-changing tools, and `spawn_agent`, refused unless in `agent.mcpAllowTools`; a sub-run is held to the same list; HTTP uses the agent API token auth (scopes, rate limit, revocable store tokens) | `test_mcp.test_mutating_tool_refused`, `test_allowlisted_tool_runs`, `test_revoked_store_token_is_refused`, `test_rate_limit`, `test_scope_filter_narrows_tools`, `test_subagents.test_spawn_agent_is_refused_unless_listed`, `test_a_sub_run_cannot_run_unlisted_gated_tools` |
| Ownership | A token only sees/modifies sessions its owner created; others 404 (no existence leak) | `test_server.test_owner_cannot_read_others_session_404`, `..._delete_...`, `..._complete_...`, `..._stream_...`, `test_unknown_and_unowned_are_indistinguishable` |
| `file_read`/`file_write` | Refuse paths outside `allowedReadPaths`/`allowedWritePaths` | `test_file.test_denies_outside_allowed_root` |
| Secrets denylist | `config.json`, `*.psd1`, `*.db`, `logs/`, `.env*` unreadable even inside an allowed root; the litellm key never leaks | `test_file.test_denies_config_json_and_hides_secret`, `..._psd1`, `..._db`, `..._env`, `..._logs_dir`, `test_write_refuses_secret_even_when_allowed` |
| Key-bearing files | `file_read` and `search_code` refuse the generated configs that embed or wire the LiteLLM key (`config/litellm.yaml`, `config/continue/config.yaml`, `config/aider/*`, `config/dsh/*`), any `config` under `tools/n8n-data/`, `.webui_secret_key`, dsh's `.credentials.yaml` and `secrets.json` | `test_fsguard.test_every_key_bearing_config_is_denied`, `test_n8n_config_and_webui_secret_key_are_denied`, `test_denies_secret_basenames`, `test_file_read_refuses_a_key_bearing_config` |
| `git_*` | Restricted to allow-listed repos (repo root + `gitAllowedRoots`); any other path refused; arguments can never become git options | `test_git.test_outside_repo_denied`, `test_default_repo_allowed`, `test_extra_root_allowed`, `test_diff_file_cannot_inject_git_options`, `test_repo_path_shaped_like_option_refused` |
| `search_code` | Query can never become a search-tool option; obeys the same read allowlist and secrets denylist as `file_read` | `test_search_plugin.test_pre_flag_query_executes_nothing`, `test_path_outside_allowlist_refused`, `test_matches_in_denied_files_withheld` |
| Tool names | A tool name another module already registered is refused (no shadowing) | `test_registry.test_plugin_cannot_shadow_system_tool` |
| `spawn_agent` | Sub-runs use local roles only unless `agent.subAgentAllowPro` | `test_subagents.test_model_chosen_pro_role_refused_by_default` |
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
- **State-changing.** `file_write` is declared mutating, so the permission policy's `mutating` class,
  checkpointing and MCP's refusal all apply to it.
- **Secrets denylist (OS-aware).** Even inside an allowed root, `_is_denied_secret`
  refuses `config.json` (holds `litellmKey` + `apiTokens`), any `*.psd1` (config), any `*.db` (session
  / memory stores), anything under a `logs/` directory, and `.env*`. The osenv seam also denies the
  resolved secrets file (`data/secrets.json`) and the platform secret
  dirs (`~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.config/bob`) on every OS. This closes the gap where the
  default repo-root allowlist would otherwise expose the proxy key and session DB to a prompt-injected
  read. Secrets resolve through the seam `osenv.secret()`:
  env → OS keychain → `data/secrets.json` → config default; never a git-tracked file. To
  read a safe file whose name collides with the denylist, place it outside those patterns.
- **Key-bearing files.** `bob_fsguard.KEY_BEARING` lists the configs Bob generates that embed the
  LiteLLM key or wire a client to it: `config/litellm.yaml`, `config/continue/config.yaml`, `config/aider/.aider.conf.yml`,
  `config/aider/model-metadata.json`, `config/dsh/settings.yaml` and `config/dsh/cordis.patch.yml`.
  `file_read` and `search_code` refuse them, along with any `config` file under `tools/n8n-data/` (n8n's
  encryption key), `.webui_secret_key` (Open WebUI's session key), dsh's `.credentials.yaml` and
  `secrets.json`. `config/litellm.yaml` itself holds no key: it reads
  `master_key: os.environ/LITELLM_MASTER_KEY`, which Bob sets when it starts the proxy.

### `git_status` / `git_log` / `git_diff` ([scripts/tools/git.py](../scripts/tools/git.py))
- Read-only git subcommands. **Path allow-list:** `_is_allowed_repo` restricts them to the
  Bob repo root plus `agent.gitAllowedRoots`; any other path returns `Access denied`. Without this
  restriction a `path` argument could point at any repo on disk (disclosing unrelated history).
- **No option injection.** A repo path that starts with `-` is refused, `git_log`'s count must be an
  integer, and `git_diff` puts its file after `--` and runs with `--no-ext-diff --no-textconv`, so a
  value such as `--output=<path>` is read as a path and no configured external diff program runs.

### `search_code` ([plugins/search/invoke.py](../plugins/search/invoke.py))
- The query always sits behind `-e` (ripgrep, grep) or `/c:` (findstr, called directly with no
  `cmd /c`), and the options end before the path, so a query such as `--pre=sh` is matched as text.
- The search path must be inside `agent.allowedReadPaths`, and matches inside files the secrets
  denylist covers are withheld.

### `shell_run` ([scripts/tools/shell.py](../scripts/tools/shell.py))
- **Approval-gated.** `shell_run` sets `REQUIRES_APPROVAL=True`, so the shared approval gate asks its
  injected `approve` callback *before* dispatch (event-driven, not a blocking `input()`; it works
  under the TUI/server). The callback is **fail-closed**: with no approver wired (server, cron, non-TTY)
  the call is denied and runs nothing. A configurable permission policy layers `allow|ask|deny` on
  top (see "Permission policy" below). 30s timeout; process killed on timeout. It is therefore **not** a
  remote-code-execution vector from the server, and over MCP it is refused outright unless listed in
  `agent.mcpAllowTools`. In the shell, **a** approves only this exact call (same arguments) for the
  session and **t** approves the tool for any arguments, a separate, explicitly labeled choice.
- **Optional OS sandbox.** When `agent.sandbox='on'`, the command runs under an OS confinement
  backend (see "OS sandbox" below) instead of directly. Default `off` reproduces the direct in-process
  run byte-for-byte.

### `web_search` / `web_fetch` ([scripts/tools/web.py](../scripts/tools/web.py))
- `web_fetch` allowlists the `http`/`https` schemes (blocks `file://`, `gopher://`, etc.) and
  blocks hosts that resolve to loopback / RFC-1918 private / link-local / reserved / multicast
  addresses (SSRF), unless `agent.allowPrivateFetch` is `true`. `web_search` uses the in-process
  `ddgs` metasearch by default (DuckDuckGo, Bing and others, reached directly from this machine);
  `agent.searchProvider` can select Brave, Tavily (with an API key) or the opt-in local SearXNG, and every
  provider falls back to `ddgs`. Backed by [tests/test_web.py](../tests/test_web.py).

### `fabric_run` ([scripts/tools/fabric.py](../scripts/tools/fabric.py))
- Runs a **named** fabric pattern (`fabric --pattern <name> --vendor LiteLLM --model coder`) on piped
  input, 120s timeout, so it always reaches Bob's LiteLLM, whatever the user's fabric defaults are. Fabric
  resolves and validates the pattern name against its installed pattern set; there
  is no path/argument passthrough from the model, so there is no traversal or injection surface
  here beyond whatever patterns the operator installed. Documented as accepted.

### `memory_recall` / `memory_store` ([scripts/tools/memory.py](../scripts/tools/memory.py))
- Operate only on the local `bob.db` via the embed server; no external egress. Disabled unless
  `memory.enabled`.

## Permission policy ([scripts/bob_permissions.py](../scripts/bob_permissions.py))
Authorization happens at the **single dispatch choke point** (`dispatch_with_approval` in
`bob_permissions.py`, or its blocking wrapper `run_gated`). Every front door calls it: the agent loop,
the shell, skill `steps`, `bob --run`, and the MCP server (stdio and HTTP). A surface with no operator to
ask passes no approver, which fails closed. `PermissionPolicy.resolve(tool, owner, agent_depth, mutating)`
returns `allow | ask | deny`:
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
tool output as untrusted model input (prompt-injection posture) and keep mutating tools behind `ask`.

## OS sandbox ([scripts/sandbox.py](../scripts/sandbox.py))
When `agent.sandbox='on'`, exec surfaces (`shell_run` today) run under an OS-native confinement backend
selected via `osenv`. Read-only tools stay in-process. **Default `off` reproduces today's behavior;**
when `on` with no usable backend, `run_sandboxed` **fails closed** (`SandboxUnavailable` → the tool
refuses). A loud unsandboxed fallback is only ever chosen under `off`.

Config: `agent.sandbox` (`off|on`), `agent.sandboxLimits` = `{cpuSeconds, memoryMB, allowRoots:[],
network:false}`. `allowRoots` is the writable set (empty ⇒ only a tmpfs `/tmp` is writable, maximally
locked). `$HOME` is never in the bind set, so `~/.ssh`/secrets are absent from the sandbox namespace on
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
  `shell_run` on Windows is resource-confined but not FS-jailed. Do not yet rely on the Windows sandbox
  for filesystem isolation.

## Auth + ownership ([scripts/bob_agent_server.py](../scripts/bob_agent_server.py))
- **Auth.** `_authed_owner` accepts a bearer token iff it is the litellm key (unless
  `agent.acceptLitellmKey` is `false`), an `agent.apiTokens` entry, or (with `agent.authStore` on) a
  scoped, rate-limited, revocable store token, else **401**. The MCP HTTP transport uses the same token
  map. `/health` is intentionally unauthenticated (returns only tool counts).
- **The litellm key** is generated per machine (`sk-bob-...`, kept in `data/secrets.json`), not a
  shared default. An explicit `litellmKey` in `config/user.json` or `BOB_LITELLMKEY` in the environment
  wins.
- **The litellm key is a wide credential.** Every generated client config, n8n's credential, fabric's
  `.env` and Open WebUI hold it. With `agent.acceptLitellmKey` on (the default), anyone holding it gets an
  unscoped `agent.defaultOwner` identity on the agent API and MCP: every tool and role, and the default owner's
  sessions. The hardened setup is scoped `agent.apiTokens` per client plus `agent.acceptLitellmKey = false`.
- **Start the proxy through Bob.** `config/litellm.yaml` reads `master_key: os.environ/LITELLM_MASTER_KEY`.
  `bob up` always passes that variable, and refuses to start the proxy when there is no key to pass. A
  hand run (`litellm --config config/litellm.yaml`) without `LITELLM_MASTER_KEY` exported serves every
  request **unauthenticated**: LiteLLM only logs `LITELLM_MASTER_KEY is not set!`. The generated file's
  header says so too. Export the key first, or use `bob up`.
- **Upstream failures** return **503** (model backend unreachable) or **502** (backend error), not a
  generic 500, so a client can retry correctly.
- **Ownership.** Each token maps to an owner id (`agent.apiTokens` records `@{token;owner}`;
  the litellm key → `agent.defaultOwner`). Sessions are stamped with the creating owner, and every
  session route resolves through `get_owned`/`delete_owned`, so another owner's `session_id`
  returns **404**, indistinguishable from an unknown id (no existence leak). To revoke a token, remove
  it from config and restart `bob agent serve`.

## Exposing on `0.0.0.0`: checklist
`bindHost` (every service Bob starts except the voice servers), `voiceBindHost` (faster-whisper and
piper) and `agent.serveHost` / `agent.mcpHost` all default to `127.0.0.1`. Before setting any of them to
`0.0.0.0` (LAN/other machines):
1. Set strong, per-client `agent.apiTokens` with distinct owners and scopes, and set
   `agent.acceptLitellmKey = false` so the shared LiteLLM key, which every client config holds, does not
   open the agent API and MCP as the unscoped default owner. With
   `bindHost` open, the LiteLLM key is the only guard on the proxy, so keep it private. (Auth: 401
   without a valid token; ownership: 404 across owners.)
2. Confirm the `file_read` secrets denylist is in force; the default repo-root allowlist
   would otherwise expose `config.json`. Narrow `allowedReadPaths` further if desired.
3. Leave `allowPrivateFetch` at `false` so `web_fetch` can't SSRF the host's private
   network from a LAN client.
4. Leave `allowedWritePaths` empty (or tightly scoped); `file_write` is off by default.
5. Keep `gitAllowedRoots` empty unless a specific extra repo must be exposed.
6. Remember `shell_run` is inert on the server (no approver) and refused over MCP unless allowlisted;
   keep `agent.mcpAllowTools` empty for a LAN-reachable MCP server. `spawn_agent` is refused over MCP
   unless listed too, and a sub-run it starts is held to the same list.
7. Leave `voiceBindHost` at `127.0.0.1` unless another machine needs speech. faster-whisper and piper
   have no authentication, so `bindHost` alone never opens them; setting `voiceBindHost` to `0.0.0.0`
   exposes them to anyone on the network.
8. Watch `logs/bob-agent.log`: every run carries a run-id, so concurrent clients are
   distinguishable and any single run is greppable end-to-end.

## Autonomy dial

Bob grants autonomy in escalating steps, each a separate, explicit opt-in. A bigger grant is never
implied by a smaller one:

1. **One-shot** (`bob agent "..."`): a single bounded run, foreground, no persistence.
2. **`--deep`**: plan/verify/self-repair phases and a larger step budget; still foreground.
3. **Durable run** (`agent.checkpoint`): run state persists so a run can resume across a restart. Off by
   default. Resume restores reasoning state, not world state (filesystem changes are not undone).
4. **Detached task** (`bob task start`): runs in a background worker that survives client
   disconnection. Owner-scoped and still governed by the permission policy; a detached run is fail-closed on
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
can. Two surfaces:
- **Screenshot as an injection surface.** A captured frame is fed back to the model through the
  `{"__images__": [...]}` tool-result contract and routed to the vision role. On-screen text must never
  silently expand the task, widen any allowlist, or relax the approval posture.
- **Screenshot as an exfiltration surface.** A screenshot can capture secrets on screen (tokens, private
  messages), so capture is approval-gated even though it is a read.

**Gating chain** (every computer-use action passes through all of it):
default-off (`agent.computerUse.enabled`) -> the tool is not even offered unless enabled -> every action
requires approval (`REQUIRES_APPROVAL`, so `_resolve_approval` is fail-closed: no approver means deny) ->
the permission policy still applies -> every decision is audited (args hashed) -> a kill switch can halt
all computer-use out of band -> a per-minute rate limit bounds action volume -> a detached/unattended run
cannot use computer-use without an explicit opt-in.

**Isolation posture.** Frontier computer-use references run in a disposable VM or container with a
*virtual* display, a network allowlist, and no logged-in accounts. Bob's default target is a virtual
display (`agent.computerUse.display: "virtual"`, an Xvfb or nested X display), and it is enforced: every
action runs against the display named by `BOB_VIRTUAL_DISPLAY` with the Wayland socket dropped, through
X-only backends (`xdotool` for input, `scrot` or ImageMagick `import` for capture). With no virtual
display, no `xdotool`, or on Windows or macOS, every action refuses rather than touching the real
desktop. Driving the real logged-in desktop (`display: "host"`, required on Windows and macOS) is the
highest-risk configuration and is an explicit, louder opt-in. Human confirmation on every action is the load-bearing defense: Bob has no server-side screenshot
injection classifier unless a run routes through a hosted computer-use API.

**Accepted limitation.** GUI input needs the host display and input bus, which the deny-by-default OS
sandbox (no `$HOME`, unshared network) cannot provide, so computer-use input does not route through the
sandbox. Its controls are the approval floor, the audit log, the kill switch, and (preferably) a virtual
display, not the OS sandbox.

**Detached-task interaction.** A scheduled or detached run is already fail-closed on approval. The
`--allow-computer` opt-in (`agent.computerUse.allowUnattended`) is required before computer-use is even
loaded in such a run: defense-in-depth so an unattended agent cannot drive the desktop by default.

**Test-backed guarantees.** Each control is pinned by a hermetic behavior test (no real input is
injected and no live model is used):
- Default-off (not offered unless enabled): `test_screenshot_tool_absent_when_computer_use_off`
  ([tests/test_computer_use.py](../tests/test_computer_use.py)).
- Virtual display enforced: `test_default_is_virtual_and_refuses_without_virtual_display`,
  `test_virtual_display_drives_only_that_display_via_xdotool`, `test_virtual_refused_off_linux`,
  `test_unknown_display_mode_refused`.
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
- Revoking a static `agent.apiTokens` entry requires a server restart (config is read once at
  startup); store tokens (`agent.authStore`) revoke immediately. Acceptable for a single-operator local
  harness; documented, not a bug.
- `fabric_run` executes whatever patterns the operator installed; treat the fabric pattern library
  as trusted operator config.
- The secrets denylist is deliberately broad (all `*.psd1`/`*.db`, any `logs/`); to read such a file
  via the agent, place it outside those patterns.
- **Denylist is name/path-based** (`Path.resolve()`: it follows symlinks/junctions and expands 8.3
  short names, but does *not* dereference NTFS **hardlinks**). An attacker who can create a hardlink
  to `config.json` under an allowed root with an innocuous name/suffix could read it via `file_read`.
  Reachability is low: `file_write` refuses the same secret patterns, and `shell_run` is
  confirmation-gated (inert on the server), so the agent has no built-in way to create such a link.
  Treat write access to an allowed root as trusted; do not expose the server on `0.0.0.0` while
  granting untrusted callers any file-creation capability inside an allowed root.
