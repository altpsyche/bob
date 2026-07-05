# Contributing to Bob

Bob is a personal, local-first AI assistant (PowerShell CLI + Python agent harness) that runs
cross-platform on Windows and Linux under PowerShell 7 (see [docs/PORTABILITY.md](docs/PORTABILITY.md)).
This note captures the **conventions** the codebase already follows so new code stays consistent.
Most of it is enforced by the architecture, not by tooling.

## Plugin & tool placement

See [plugins/AUTHORING.md](plugins/AUTHORING.md) for the three-layer capability model. Logic lives in one importable place; tools auto-discover (no manual registration); exclude a tool via `agent.disabledTools`, never an allowlist.

## Error-handling convention

1. **Fail loudly at the edges, degrade gracefully only where there's a real fallback.**
   - A malformed tool call is surfaced to the model as a `__parse_error__` so it can self-correct;
     it is *not* silently dropped ([scripts/bob_loop.py](scripts/bob_loop.py) `_parse_hermes_tool_calls`).
   - A `TOOL_DEFS` name with no `DISPATCH` entry is a **hard contract error**: the tool is skipped and
     recorded, never half-loaded ([scripts/tools/tool_registry.py](scripts/tools/tool_registry.py)).
   - `bob search`/`summarise` fall back to raw output when the LLM is down, a genuine fallback, so it
     degrades quietly. Prefer this only when the degraded result is still useful.

2. **CLI entry points catch and print one coherent line, never a raw traceback.**
   Boundary functions (`embed`, `store`, `recall`, HTTP calls) raise a `RuntimeError` with context;
   the `cmd_*` / `main()` layer catches it and prints a human message + returns/exits
   (see [scripts/bob_memory.py](scripts/bob_memory.py)). Internal helpers let exceptions propagate to
   that boundary rather than swallowing them.

3. **Tool dispatch never raises to the agent.** `registry.dispatch_call()` always returns a string
   (it catches `JSONDecodeError` and `Exception`). Tools may raise internally; the registry converts
   it to a message the model can read.

4. **No unexplained empty `catch {}` / `except: pass`.** If a swallow is intentional (e.g. per-chunk
   SSE JSON that is expected to be partial, or a best-effort probe), add a one-line comment saying so.
   A silent swallow with no fallback and no comment is a bug.

5. **Writes to shared files are atomic:** write a `.$PID.tmp` (PowerShell) / `os.getpid()`-suffixed temp
   (Python) then `Move-Item -Force` / `os.replace`. Applies to `data/config.json`, `data/schedules.json`,
   `.last-agent-result.txt`. Never `Set-Content` a file other code reads concurrently.

6. **Every network/LLM call has an explicit client-side timeout** (`agent.requestTimeout`, ≥ the litellm
   `request_timeout` so thinking models aren't cut off). One transient retry at most; log it.

7. **Observability:** route agent/tool events through the `bob.agent` logger to `logs/bob-agent.log` with
   a per-run id; keep the coloured `stderr` previews for interactive use
   ([scripts/bob_loop.py](scripts/bob_loop.py) `_agent_logger`).

8. **Single source of truth for defaults (NB1).** Shared constants (service ports and the role
   table) live only in [config/defaults.json](config/defaults.json), read by both Python
   (`bob_core.load_defaults()` → `_PORT_DEFAULTS` / `get_role`) and PowerShell
   (`_models.ps1 Get-BobDefaults` → `$BobPortDefaults` / `Get-RoleForTask`). Never re-inline a port
   number or role literal; add it to `defaults.json`. A `bob gen` / config change flows through
   `Get-BobConfig`; a *neutral* (no-PowerShell) runtime config comes from
   [scripts/bob_config.py](scripts/bob_config.py) `resolve_runtime_config()`.

9. **Portability seams (NB3/NB4).** OS-specific behavior goes through one seam, not scattered
   branches: [scripts/osenv.py](scripts/osenv.py) for shell / data-dir (C4) / secrets (C3) / notify;
   secrets resolve via `osenv.secret()` (env → keychain → `data/secrets.json`), never a git-tracked
   file. New `bob` commands are added to the command registry
   ([scripts/bob/registry.py](scripts/bob/registry.py)); `config/verbs.json` is *generated* from it
   (`python -m bob.registry`) and its sync is enforced by `check.ps1`; do not hand-edit `verbs.json`.

## Tests

`tests/` is a stdlib-`unittest` suite (also runnable under `pytest` if installed):

```powershell
tools\venv-litellm\Scripts\python.exe -m unittest discover -s tests
# or, if pytest is installed:
tools\venv-litellm\Scripts\python.exe -m pytest tests -q
```

It also runs as section **[11]** of `.\scripts\test-dry-run.ps1` (the PowerShell regression suite),
and `scripts\check.ps1` runs it alongside `py_compile` + a PowerShell AST parse as one gate.
Add a test when you add a tool, a routing task, a config default, or a new failure mode. The registry's
validated-contract + injected-config design makes tools easy to test against a fake config (see
[tests/_common.py](tests/_common.py)). Cover new public surfaces (routes, auth/ownership, streaming,
cancellation, concurrency); see the Module N tests for the pattern.

## Verifying a change

- One gate for all four: `pwsh -File scripts\check.ps1` (py_compile + PowerShell AST parse +
  `config/verbs.json`↔registry sync + the unittest suite; exits non-zero on any failure). Install it
  as a pre-commit hook once per clone with `pwsh -File scripts\install-hooks.ps1`. In CI it runs on
  Linux + Windows ([.github/workflows/ci.yml](.github/workflows/ci.yml)) via a `BOB_PYTHON` override.
- Individually, Python: `tools\venv-litellm\Scripts\python.exe -m py_compile <files>` then the suite
  above. PowerShell: `[System.Management.Automation.Language.Parser]::ParseFile(...)` (AST parse).
- End-to-end: `bob doctor` (full pre-flight) and `.\scripts\test-dry-run.ps1`.
