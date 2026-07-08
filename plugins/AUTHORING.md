# Plugin and Tool Authoring Guide

## Three-Layer Model

```
Layer 1  scripts/tools/<name>.py        infrastructure / pure plumbing
Layer 2  plugins/<name>/tool.py         agent-facing interface for a CLI plugin
Layer 3  plugins/<name>/invoke.py       human-facing CLI wrapper
```

**Layer 1 — Infrastructure tools** (`scripts/tools/`)
Pure agent-internal capabilities with no meaningful standalone CLI. Examples: `git`, `file`, `memory`, `web`, `shell`. Rule: if a human would never type `bob <name>` directly, it lives here.

**Layer 2 — Plugin tool** (`plugins/<name>/tool.py`)
The agent interface for a plugin that also has a CLI command. Imports and calls the same core function as the CLI — no duplicated logic. Rule: if `bob <name>` exists AND the agent should be able to call it too, create this file.

**Layer 3 — Plugin CLI** (`plugins/<name>/invoke.py` or `invoke.ps1`)
The human-facing entry point. Handles argparse, stdin, flags, output formatting. Calls the shared core function. Contains no core logic itself.

### Decision Rule

```
New capability?
├── No meaningful `bob <name>` CLI?
│   └── scripts/tools/<name>.py   (Layer 1)
├── Has a `bob <name>` CLI?
│   ├── Agent should call it too?
│   │   └── plugins/<name>/invoke.py + plugins/<name>/tool.py   (Layers 2+3)
│   └── CLI-only, agent use unlikely?
│       └── plugins/<name>/invoke.py only   (Layer 3 only)
```

### Functional grouping

The strict rule above is "one directory per capability." The capability ports (former PowerShell
verbs → Python) **bend it deliberately**: several closely-related capabilities share one
`scripts/tools/<group>.py` module (e.g. `budget.py`, and later `stack.py`/`models.py`/`schedule.py`),
each exposing multiple tool fns. Rationale: ~45 verbs would otherwise mean ~40 near-empty plugin dirs.
The **core-logic rule still holds** — the fn is defined once in the module; the agent tool
(`DISPATCH`), the `bob <verb>` handler (`scripts/bob/cli.py`), and `bob --run <cap>` are all thin
adapters over it. Group by domain, not per verb, when the capabilities are lifecycle/provisioning kin.

---

## Core Logic Rule

Every plugin that has both a CLI and a tool MUST extract its core logic into a shared function in `invoke.py`. The tool imports and calls it. The CLI calls it too. Logic lives in exactly one place.

```
plugins/<name>/
  invoke.py       # CLI: argparse → calls _core_fn() → formats output
  tool.py         # Agent: TOOL_DEFS + DISPATCH → calls _core_fn()
  description.txt
```

Example from `summarise`:
```python
# invoke.py
def summarise(content: str, length: str = "medium", config: dict = None) -> str:
    """Core logic. Returns the summary string."""
    ...

def main():
    args = parse()
    config = load_config()
    content = read_input(args)
    print(summarise(content, args.length, config))
```

```python
# tool.py
from plugins.summarise.invoke import summarise
_cfg = {}
def configure(config): global _cfg; _cfg = config
TOOL_DEFS = [...]
DISPATCH = {"summarise_text": lambda content, length="medium": summarise(content, length, _cfg)}
```

---

## Required Exports

Every tool file (Layer 1 or Layer 2) must export:

```python
TOOL_DEFS   # list[dict]  — OpenAI function-calling schemas
DISPATCH    # dict[str, callable]  — tool_name → function
configure(config: dict)  # called once at startup with full config
```

Optionally:
```python
test() -> str  # called by `bob tools test <name>`; return a status string
```

### TOOL_DEFS format

```python
TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "tool_name",
            "description": "What this does. When to call it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "param": {"type": "string", "description": "..."},
                },
                "required": ["param"],
            },
        },
    }
]
```

---

## Registration Rule

**No manual registration required.** The agent auto-discovers all tool files on startup:

- `scripts/tools/<name>.py` — Layer 1 system tools
- `plugins/<name>/tool.py` — Layer 2 plugin tools

Creating the file is the only step. The agent will include it automatically on next start and print a startup summary:

```
[bob] tools: draft fabric file git memory play search shell summarise web (10)
```

To **exclude** a tool without deleting it, add its directory/stem name to `agent.disabledTools` in `config/user.json`:

```json
{
  "agent": {
    "disabledTools": ["play"]
  }
}
```

For plugin tools (Layer 2), the tool name exposed to the agent (e.g. `music_play`) is set inside `TOOL_DEFS` — it's independent of the directory name (`play`).

---

## Testing

```powershell
# List all discoverable tools and their status
bob tools list

# Run a tool's test() function
bob tools test play
bob tools test summarise

# Quick schema inspection
python scripts/tools/tool_loader.py --info summarise
```

---

## Anti-patterns

| Anti-pattern | Fix |
|---|---|
| Core logic only in `invoke.py`'s `main()` | Extract to a named function, import from tool.py |
| Logic copied into both `invoke.py` and `tool.py` | One function, two callers |
| TOOL_DEFS name doesn't match DISPATCH key | Names must be identical — loader warns at startup |
| Layer 2 capability placed in `scripts/tools/` | Use decision rule: does `bob <name>` exist? |
