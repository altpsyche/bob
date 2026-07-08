"""Rich views of the catalog (commands / tools / skills) for the shell, themed by the active
`Theme`. Kept separate from `catalog.py` (which stays plain-string for the CLI front door) and from
`shell.py` (the REPL): these are pure `data + theme -> rich renderable` functions, so `/help`, `/tools`,
and `/skills` match the splash and are unit-testable. New surfaces (e.g. a voice/status view) slot in as
another function here.
"""
from pathlib import Path

from rich.console import Group
from rich.table import Table
from rich.text import Text

from bob import registry

_GROUP_ORDER = ["Talk", "Act", "Make", "Know", "Run", "Config"]
_PLUGINS_DIR = Path(__file__).resolve().parent.parent.parent / "plugins"  # repo/plugins


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
            right.append("  [sub-agent]", style=theme.warn)
        tbl.add_row(s["name"], right)
    for name, phase, msg in getattr(reg, "errors", []):
        tbl.add_row(Text(f"{name}", style=theme.error), Text(f"[FAILED] {phase}: {msg}", style=theme.error))
    return Group(Text(f"Skills ({len(skills)})", style=f"bold {theme.accent}"), tbl)


def _diff_line_style(line: str, theme) -> str:
    """The style for one unified-diff line: added green, removed red, hunk headers accent, file headers
    and context muted. File-header prefixes (+++/---) are checked before the +/- content prefixes."""
    if line.startswith(("+++", "---")):
        return theme.muted
    if line.startswith("@@"):
        return theme.accent
    if line.startswith("+"):
        return theme.success
    if line.startswith("-"):
        return theme.error
    return theme.muted


def diff_view(diff_text: str, theme, path: str = None, max_lines: int = 40):
    """Render a unified diff: +/- lines coloured success/error (the leading sign IS the gutter), hunk
    headers in the accent, context/file-headers muted. A diff longer than `max_lines` collapses to the
    first `max_lines` with a '... (N more)' footer. One renderer for tool results now and a future
    coding-agent file-edit result later, so there is never a second diff path."""
    lines = (diff_text or "").splitlines()
    shown = lines[:max_lines]
    body = Text()
    for i, ln in enumerate(shown):
        if i:
            body.append("\n")
        body.append(ln, style=_diff_line_style(ln, theme))
    parts = []
    if path:
        parts.append(Text(path, style=f"bold {theme.accent}"))
    parts.append(body)
    hidden = len(lines) - len(shown)
    if hidden > 0:
        parts.append(Text(f"... ({hidden} more)", style=theme.muted))
    return Group(*parts)


def plugins_view(theme, plugins_dir: Path = None):
    """Drop-in plugins (plugins/<name>/) with their one-line description.txt — the `bob <name>` verbs
    that live outside the command registry. Returns None if there are none."""
    d = Path(plugins_dir) if plugins_dir else _PLUGINS_DIR
    if not d.exists():
        return None
    rows = []
    for sub in sorted(d.iterdir()):
        if not sub.is_dir():
            continue
        if not ((sub / "invoke.py").exists() or (sub / "invoke.ps1").exists()):
            continue
        desc_file = sub / "description.txt"
        desc = desc_file.read_text(encoding="utf-8").strip().splitlines()[0] if desc_file.exists() else ""
        rows.append((sub.name, desc))
    if not rows:
        return None
    tbl = _two_col(theme, theme.accent)
    for name, desc in rows:
        tbl.add_row(name, desc)
    return Group(Text(f"Plugins ({len(rows)})", style=f"bold {theme.accent}"), tbl)
