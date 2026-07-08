"""Render the unified catalog (commands, tools, skills) from the registries, so
help and the shell splash show "everything Bob can do" without a hand-maintained menu.

Returns plain strings: the CLI prints them directly; the interactive shell wraps them with rich. Honest
about load state — tools/skills that failed to load are shown as [FAILED], not hidden, keeping the
observability posture in the UI. Counts come straight from the registries so they can't drift."""
from bob import registry

_GROUP_ORDER = ["Talk", "Act", "Make", "Know", "Run", "Config"]


def render_commands() -> str:
    """Grouped command catalog (hidden verbs excluded), in the six mental-model buckets."""
    groups: dict = {}
    for c in registry.commands(include_hidden=False):
        groups.setdefault(c["group"], []).append(c)
    order = [g for g in _GROUP_ORDER if g in groups] + [g for g in groups if g not in _GROUP_ORDER]
    lines: list = []
    for g in order:
        lines.append(f"{g}:")
        for c in groups[g]:
            usage = f"{c['name']} {c['args']}".strip()
            lines.append(f"  {usage:<34} {c['summary']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def render_tools(tool_registry) -> str:
    """Discovered tools by schema name + short description; failed loads shown as [FAILED]."""
    loaded = getattr(tool_registry, "_loaded_names", set())
    lines = [f"Tools ({len(loaded)} loaded):"]
    for s in getattr(tool_registry, "tool_schemas", []):
        fn = s.get("function", {})
        lines.append(f"  {fn.get('name', ''):<24} {(fn.get('description', '') or '')[:60]}")
    for name, phase, msg in getattr(tool_registry, "errors", []):
        lines.append(f"  [FAILED] {name} ({phase}): {msg}")
    return "\n".join(lines)


def render_skills(skill_registry) -> str:
    """Registered skills; sub-agent skills (no steps) marked [sub-agent] — runnable as isolated sub-runs; failures shown."""
    skills = skill_registry.list()
    lines = [f"Skills ({len(skills)}):"]
    if not skills and not skill_registry.errors:
        lines.append("  (none — add skills/<name>/skill.yaml)")
    for s in sorted(skills, key=lambda x: x["name"]):
        tag = "" if s["steps"] else "   [sub-agent]"
        lines.append(f"  {s['name']:<20} {(s['description'] or '')[:56]}{tag}")
    for name, phase, msg in getattr(skill_registry, "errors", []):
        lines.append(f"  [FAILED] {name} ({phase}): {msg}")
    return "\n".join(lines)


def counts(tool_registry=None, skill_registry=None) -> str:
    """One-line "N tools · M commands · K skills" for the splash header. Missing registries omitted."""
    parts: list = []
    if tool_registry is not None:
        parts.append(f"{len(getattr(tool_registry, '_loaded_names', []))} tools")
    parts.append(f"{len(registry.commands(include_hidden=False))} commands")
    if skill_registry is not None:
        parts.append(f"{len(skill_registry.list())} skills")
    return " · ".join(parts)
