"""NE2 — rich views of the catalog (commands / tools / skills) for the shell, themed by the active
`Theme`. Kept separate from `catalog.py` (which stays plain-string for the CLI front door) and from
`shell.py` (the REPL): these are pure `data + theme -> rich renderable` functions, so `/help`, `/tools`,
and `/skills` match the splash and are unit-testable. New surfaces (e.g. a voice/status view) slot in as
another function here.
"""
from rich.console import Group
from rich.table import Table
from rich.text import Text

from bob import registry

_GROUP_ORDER = ["Talk", "Act", "Make", "Know", "Run", "Config"]


def _two_col(theme, left_style: str):
    """A borderless two-column table (left = name/accent, right = summary/muted) with a small gutter."""
    tbl = Table(show_header=False, box=None, pad_edge=False, padding=(0, 3, 0, 0))
    tbl.add_column(style=left_style, no_wrap=True)
    tbl.add_column(style=theme.muted, overflow="fold")
    return tbl


def commands_view(theme):
    """Commands grouped into the six mental-model buckets, each with an accent heading."""
    groups: dict = {}
    for c in registry.commands(include_hidden=False):
        groups.setdefault(c["group"], []).append(c)
    order = [g for g in _GROUP_ORDER if g in groups] + [g for g in groups if g not in _GROUP_ORDER]
    blocks = []
    for g in order:
        tbl = _two_col(theme, theme.accent)
        for c in groups[g]:
            tbl.add_row(f"{c['name']} {c['args']}".strip(), c["summary"])
        blocks.append(Group(Text(g, style=f"bold {theme.accent}"), tbl, Text()))
    return Group(*blocks)


def tools_view(reg, theme):
    """Discovered tools by name + description; failed loads shown in the error colour, not hidden."""
    loaded = getattr(reg, "_loaded_names", set())
    tbl = _two_col(theme, theme.tool)
    for s in getattr(reg, "tool_schemas", []):
        fn = s.get("function", {})
        tbl.add_row(fn.get("name", ""), (fn.get("description", "") or "")[:72])
    for name, phase, msg in getattr(reg, "errors", []):
        tbl.add_row(Text(f"{name}", style=theme.error), Text(f"[FAILED] {phase}: {msg}", style=theme.error))
    return Group(Text(f"Tools ({len(loaded)})", style=f"bold {theme.accent}"), tbl)


def skills_view(reg, theme):
    """Registered skills; sub-agent skills (no steps) tagged, failures shown in the error colour."""
    skills = reg.list()
    tbl = _two_col(theme, theme.accent)
    if not skills and not getattr(reg, "errors", []):
        tbl.add_row("(none)", "add skills/<name>/skill.yaml")
    for s in sorted(skills, key=lambda x: x["name"]):
        desc = (s["description"] or "")[:64]
        right = Text(desc)
        if not s["steps"]:
            right.append("  [needs Module O]", style=theme.warn)
        tbl.add_row(s["name"], right)
    for name, phase, msg in getattr(reg, "errors", []):
        tbl.add_row(Text(f"{name}", style=theme.error), Text(f"[FAILED] {phase}: {msg}", style=theme.error))
    return Group(Text(f"Skills ({len(skills)})", style=f"bold {theme.accent}"), tbl)
