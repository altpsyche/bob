# Bob — Claude Code Rules

## Committing

Do NOT run `git commit` unless the user explicitly asks. Make and iterate on changes in the working
tree; keep the gate green and let the user review the diff and commit when they're ready. Never commit
proactively "per fix" or to checkpoint — even during a multi-step task. Same for `git push`.

## Plugin and Tool Placement

This project has a three-layer capability model. Before creating any new tool or plugin, follow the decision rule:

```
New capability?
├── No meaningful `bob <name>` CLI?
│   └── scripts/tools/<name>.py          (infrastructure tool)
├── Has a `bob <name>` CLI, agent should call it too?
│   └── plugins/<name>/invoke.py + plugins/<name>/tool.py
└── CLI-only, agent use unlikely?
    └── plugins/<name>/invoke.py only
```

**Core logic rule:** Logic lives in `invoke.py` as an importable function. `tool.py` imports and calls it. The CLI calls it too. Never duplicate logic between files.

**Registration rule:** No manual registration. Tools auto-discover from `scripts/tools/*.py` (Layer 1) and `plugins/<name>/tool.py` (Layer 2) — creating the file is the only step. To exclude one without deleting it, add its stem/dir name to `agent.disabledTools` in `config/bob.psd1`. The loader prints a startup summary and tracks load errors; there is no `agent.tools` allowlist.

Full authoring guide: [plugins/AUTHORING.md](../plugins/AUTHORING.md)

## Project Layout

```
scripts/tools/        Layer 1 — infrastructure (git, file, memory, web, shell, fabric)
plugins/<name>/       Layer 2+3 — plugin tools with CLI
  invoke.py           CLI entry point + shared core functions
  tool.py             Agent-facing interface (imports from invoke.py)
  description.txt     One-line description shown in `bob help`
scripts/bob_core.py   Config loading (+ neutral-source loaders), LLM client, shared utilities
scripts/bob_config.py NB2 — Python runtime-config resolver (boot without PowerShell)
scripts/osenv.py      NB3 — OS seam: shell / data-dir / secrets / notify
scripts/bob/          NB4 — the `python -m bob` runtime package (cli, registry, run_agent_events API)
config/defaults.json  NB1 — neutral single source of truth: ports + role table (both langs read it)
config/verbs.json     NB4 — command→runtime routing (GENERATED from scripts/bob/registry.py)
config/bob.psd1       Windows authoring source: persona, routing, agent.disabledTools, ports (→ data/config.json)
```

## Key Patterns

- Tool files export `TOOL_DEFS`, `DISPATCH`, `configure(config)`, optionally `test() -> str`
- `tool_loader.py` discovers both `scripts/tools/*.py` and `plugins/*/tool.py` automatically
- For plugin tools, the loader key is the **directory name** (e.g. `play`), not the tool function name (e.g. `music_play`)
- Non-streaming LLM calls in tool.py (`stream=False`) — streaming is a CLI UX concern only
- **Shared ports/roles live only in `config/defaults.json`** (NB1) — never re-inline a literal in `.py` or `.ps1`
- **OS-specific behavior goes through `scripts/osenv.py`** (NB3); secrets via `osenv.secret()`, never a tracked file
- **New `bob` commands are registered in `scripts/bob/registry.py`** (NB4); regenerate `config/verbs.json` with `python -m bob.registry` (the `check.ps1` gate enforces sync). Front door: `bob serve` = inference (pwsh); `bob agent serve` = agent HTTP server (python)
