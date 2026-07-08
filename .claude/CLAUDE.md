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

**Registration rule:** No manual registration. Tools auto-discover from `scripts/tools/*.py` (Layer 1) and `plugins/<name>/tool.py` (Layer 2) — creating the file is the only step. To exclude one without deleting it, add its stem/dir name to `agent.disabledTools` in `config/user.json`. The loader prints a startup summary and tracks load errors; there is no `agent.tools` allowlist.

Full authoring guide: [plugins/AUTHORING.md](../plugins/AUTHORING.md)

## Project Layout

```
scripts/tools/        Layer 1 — infrastructure (git, file, memory, web, shell, fabric)
plugins/<name>/       Layer 2+3 — plugin tools with CLI
  invoke.py           CLI entry point + shared core functions
  tool.py             Agent-facing interface (imports from invoke.py)
  description.txt     One-line description shown in `bob help`
scripts/bob_core.py   Config loading (+ neutral-source loaders), LLM client, shared utilities
scripts/bob_config.py Python runtime-config resolver (boot without PowerShell)
scripts/osenv.py      the OS seam: shell / data-dir / secrets / notify
scripts/bob/          the `python -m bob` runtime package (cli, registry, run_agent_events API)
config/defaults.json  single source of truth: persona, routing, ports, role table (deep-merged with config/user.json)
```

## Key Patterns

- Tool files export `TOOL_DEFS`, `DISPATCH`, `configure(config)`, optionally `test() -> str`
- `tool_loader.py` discovers both `scripts/tools/*.py` and `plugins/*/tool.py` automatically
- For plugin tools, the loader key is the **directory name** (e.g. `play`), not the tool function name (e.g. `music_play`)
- Non-streaming LLM calls in tool.py (`stream=False`) — streaming is a CLI UX concern only
- **Shared ports/roles live only in `config/defaults.json`** — never re-inline a literal in `.py`
- **OS-specific behavior goes through the osenv seam (`scripts/osenv.py`)**; secrets via `osenv.secret()`, never a tracked file
- **New `bob` commands are registered in `scripts/bob/registry.py`** — `registry.COMMANDS` is the sole dispatch + help source, so adding a verb is one entry + one handler. Front door: `bob serve` = inference; `bob agent serve` = agent HTTP server
