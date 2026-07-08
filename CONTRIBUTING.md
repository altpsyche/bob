# Contributing to Bob

Bob is a personal, local-first AI assistant — a single Python CLI + agent harness (`python -m bob`)
that runs cross-platform on Linux and Windows (see
[docs/PORTABILITY.md](docs/PORTABILITY.md)). This note captures the **conventions** the codebase
already follows so new code stays consistent. Most of it is enforced by the architecture, not by tooling.

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

5. **Writes to shared files are atomic:** write an `os.getpid()`-suffixed temp file then `os.replace`.
   Applies to `data/schedules.json`, `.last-agent-result.txt`, and any generated config cache. Never
   overwrite in place a file other code reads concurrently.

6. **Every network/LLM call has an explicit client-side timeout** (`agent.requestTimeout`, ≥ the litellm
   `request_timeout` so thinking models aren't cut off). One transient retry at most; log it.

7. **Observability:** route agent/tool events through the `bob.agent` logger to `logs/bob-agent.log` with
   a per-run id; keep the coloured `stderr` previews for interactive use
   ([scripts/bob_loop.py](scripts/bob_loop.py) `_agent_logger`).

8. **Single source of truth for defaults.** Shared constants (service ports and the role
   table) live only in [config/defaults.json](config/defaults.json), read by
   `bob_core.load_defaults()` (→ `_PORT_DEFAULTS` / `get_role`). Never re-inline a port number or
   role literal; add it to `defaults.json`. The runtime config resolves live from
   `defaults.json` + `config/user.json` via [scripts/bob_config.py](scripts/bob_config.py)
   `resolve_runtime_config()` — the same way on every OS.

9. **Portability seams.** OS-specific behavior goes through one seam, not scattered
   branches: [scripts/osenv.py](scripts/osenv.py) for shell / data-dir / secrets / notify;
   secrets resolve via `osenv.secret()` (env → keychain → `data/secrets.json`), never a git-tracked
   file. New `bob` commands are added to the command registry
   ([scripts/bob/registry.py](scripts/bob/registry.py)) — `registry.COMMANDS` is the sole source for
   dispatch + help, so adding a verb is one entry + one handler with no generated table to keep in sync.

## Tests

`tests/` is a stdlib-`unittest` suite (also runnable under `pytest` if installed):

Linux:
```bash
tools/venv-litellm/bin/python -m unittest discover -s tests
# or, if pytest is installed:
tools/venv-litellm/bin/python -m pytest tests -q
```

Windows:
```bat
tools\venv-litellm\Scripts\python.exe -m unittest discover -s tests
:: or, if pytest is installed:
tools\venv-litellm\Scripts\python.exe -m pytest tests -q
```

The suite also runs as step 4 of the `scripts/check.py` gate (see below).
Add a test when you add a tool, a routing task, a config default, or a new failure mode. The registry's
validated-contract + injected-config design makes tools easy to test against a fake config (see
[tests/_common.py](tests/_common.py)). Cover new public surfaces (routes, auth/ownership, streaming,
cancellation, concurrency); see the Module N tests for the pattern.

## Verifying a change

- One gate for everything: `python scripts/check.py` — `py_compile` over `scripts/`/`plugins/`/`tests/`,
  a `versions.lock`↔sources sync check, the git exec-bits on the shell entrypoints, and the unittest
  suite; exits non-zero on the first failing category. Run it with the project interpreter:

  Linux:
  ```bash
  tools/venv-litellm/bin/python scripts/check.py          # add --no-tests for static checks only
  ```
  Windows:
  ```bat
  tools\venv-litellm\Scripts\python.exe scripts\check.py
  ```

  Install it as a pre-commit hook once per clone with `python scripts/install_hooks.py`. In CI it runs
  on Linux + Windows ([.github/workflows/ci.yml](.github/workflows/ci.yml)) via a `BOB_PYTHON` override.
- End-to-end: `bob doctor` (full pre-flight) and the cross-OS smoke `python scripts/smoke.py`.
