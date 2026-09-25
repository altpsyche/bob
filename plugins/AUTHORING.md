# Plugin and Tool Authoring Guide

## Three-Layer Model

```
Layer 1  scripts/tools/<name>.py        infrastructure / pure plumbing
Layer 2  plugins/<name>/tool.py         agent-facing interface for a CLI plugin
Layer 3  plugins/<name>/invoke.py       the plugin's logic + its `bob <name>` CLI (main(argv))
```

**Layer 1: Infrastructure tools** (`scripts/tools/`)
Pure agent-internal capabilities with no meaningful standalone CLI. Examples: `git`, `file`, `memory`, `web`, `shell`. Rule: if a human would never type `bob <name>` directly, it lives here.

**Layer 2: Plugin tool** (`plugins/<name>/tool.py`)
The agent interface for a plugin that also has a CLI command. Imports and calls the same core function as the CLI, with no duplicated logic. Rule: if `bob <name>` exists AND the agent should be able to call it too, create this file.

**Layer 3: Plugin CLI** (`plugins/<name>/invoke.py`)
`bob <plugin-dir-name> ...` dispatches to `main(argv)` in `plugins/<name>/invoke.py`, passing the
remaining arguments (so `bob summarise README.md --length short` calls `main(["README.md", "--length",
"short"])`). The plugin's logic lives in `invoke.py` as importable functions; `main(argv)` handles
argparse, stdin, flags and output formatting around them and returns an exit code. Plugins are Python
only: there are no PowerShell plugins.

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

The strict rule above is "one directory per capability." The lifecycle and provisioning capabilities
**bend it deliberately**: several closely-related capabilities share one `scripts/tools/<group>.py`
module (e.g. `budget.py`, `stack.py`, `models.py`, `schedule.py`),
each exposing multiple tool fns. Rationale: ~45 verbs would otherwise mean ~40 near-empty plugin dirs.
The **core-logic rule still holds**: the fn is defined once in the module; the agent tool
(`DISPATCH`), the `bob <verb>` handler (`scripts/bob/cli.py`), and `bob --run <cap>` are all thin
adapters over it. Group by domain, not per verb, when the capabilities are lifecycle/provisioning kin.

---

## Core Logic Rule

Every plugin that has both a CLI and a tool MUST extract its core logic into a shared function in `invoke.py`. The tool imports and calls it. The CLI calls it too. Logic lives in exactly one place.

```
plugins/<name>/
  invoke.py       # logic: core_fn(); CLI: main(argv) -> argparse -> core_fn() -> prints, returns exit code
  tool.py         # Agent: TOOL_DEFS + DISPATCH -> imports core_fn() from invoke.py
  description.txt # one line shown in `bob help`
```

Example from `summarise`:
```python
# invoke.py
def summarise(content: str, length: str = "medium", config: dict = None) -> str:
    """Core logic. Returns the summary string."""
    ...

def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="bob summarise")
    ...
    args = p.parse_args(argv)
    config = load_config()
    content = read_input(args)
    print(summarise(content, args.length, config))
    return 0


if __name__ == "__main__":
    sys.exit(main())
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
TOOL_DEFS   # list[dict]: OpenAI function-calling schemas
DISPATCH    # dict[str, callable]: tool_name -> function
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

- `scripts/tools/<name>.py`: Layer 1 system tools
- `plugins/<name>/tool.py`: Layer 2 plugin tools

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

This is the only way to disable a plugin; the name is the plugin's directory name (`play`), and
renaming or deleting `invoke.py` is not a switch. For plugin tools (Layer 2), the tool name exposed to
the agent (e.g. `music_play`) is set inside `TOOL_DEFS`; it is independent of the directory name.

Tool function names are global. System tools load first, then plugins in directory order; if a module
declares a name another module already registered, the duplicate is refused (the first owner keeps it)
and the loader records a `collision` load error, reported in the startup summary.

### State-changing tools

A tool that changes state (writes a file, starts or stops a process, edits config) declares it, so the
permission policy (`mutating` class), checkpoint/rewind and parallel dispatch never treat it as a read:

```python
MUTATING_TOOLS = {"tool_name"}                    # names from this module's TOOL_DEFS
AFFECTS = {"tool_name": lambda args: [path, ...]} # files a call will touch (snapshotted before the step)
REQUIRES_APPROVAL = True                          # every tool in the module always asks first
```

These markers only apply to names the module itself registers.

---

## Testing

```sh
# List all discoverable tools and their status
bob tools list

# Run a tool's test() function
bob tools test play
bob tools test summarise

bob tools info summarise

# Run the plugin CLI
bob summarise README.md --length short
```

---

## Anti-patterns

| Anti-pattern | Fix |
|---|---|
| Core logic only in `invoke.py`'s `main()` | Extract to a named function, import from tool.py |
| Logic in `tool.py`, `invoke.py` shelling out to it | Invert it: logic in `invoke.py`, tool.py imports |
| Logic copied into both `invoke.py` and `tool.py` | One function, two callers |
| `main()` reading `sys.argv` directly | `main(argv=None)` + `parse_args(argv)`, return an exit code |
| A model-supplied value placed in a subprocess argv as-is | Put it behind `--` / `-e`, reject leading `-` |
| TOOL_DEFS name doesn't match DISPATCH key | Names must be identical; the loader refuses the tool at startup |
| Layer 2 capability placed in `scripts/tools/` | Use decision rule: does `bob <name>` exist? |
