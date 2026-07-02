"""NE4 (contract C6) — the skills registry: discover, validate, list, and (simply) run named skills.

A *skill* is a higher-level, named workflow — distinct from an atomic *tool*. Manifest lives at
`skills/<name>/skill.yaml` with the frontier-standard shape `{name, description}` (name defaults to
the directory; only `description` is required — matching Claude Code SKILL.md / gemini-cli TOML, which
carry NO typed `params`), plus an optional `group` and an optional Bob extension `steps` — a list of
`{tool, arguments}` run in order for simple tool-sequence skills.

A skill WITHOUT `steps` is a prompt / sub-agent skill whose execution is Module O's job (O1
sub-agents): NE lists it in the catalog and reports "requires Module O", it never runs it. Mirrors
ToolRegistry: build once, collect `(name, phase, message)` errors, and stay context-cheap via
progressive disclosure (only name + description are loaded for the catalog; the body/steps load on
run). A malformed manifest is a hard contract error (the tool convention), not a silent skip.
"""
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent  # scripts/bob_skills.py -> repo
SKILLS_DIR = REPO / "skills"


class SkillRegistry:
    def __init__(self):
        self.skills: dict = {}          # name -> manifest dict
        self.errors: list = []          # (name, phase, message) — phase: import|parse|contract

    @classmethod
    def build(cls, skills_dir: Path = None) -> "SkillRegistry":
        reg = cls()
        d = Path(skills_dir) if skills_dir else SKILLS_DIR
        if not d.exists():
            return reg
        for sub in sorted(d.iterdir()):
            manifest = sub / "skill.yaml"
            if sub.is_dir() and manifest.exists():
                reg._load_one(sub.name, manifest)
        return reg

    def _load_one(self, dirname: str, path: Path) -> None:
        try:
            import yaml
        except ModuleNotFoundError:
            self.errors.append((dirname, "import", "PyYAML not available to parse skill.yaml"))
            return
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as e:
            self.errors.append((dirname, "parse", f"invalid YAML: {e}"))
            return
        if not isinstance(data, dict):
            self.errors.append((dirname, "contract", "manifest is not a mapping"))
            return
        name = data.get("name") or dirname
        desc = data.get("description")
        if not desc:
            self.errors.append((name, "contract", "missing required 'description'"))
            return
        steps = data.get("steps")
        if steps is not None and not isinstance(steps, list):
            self.errors.append((name, "contract", "'steps' must be a list of {tool, arguments}"))
            return
        self.skills[name] = {
            "name": name,
            "description": desc,
            "group": data.get("group", "Skills"),
            "argument_hint": data.get("argument-hint", ""),
            "steps": steps or [],
            "dir": str(path.parent),
        }

    def list(self) -> list:
        """Skill manifests (copies). Progressive disclosure: only name/description/group are needed
        for the catalog; steps/body load on run."""
        return [dict(s) for s in self.skills.values()]

    def run(self, name: str, registry, context=None) -> str:
        """Run a simple tool-sequence skill via the tool registry. A skill with no `steps` is a
        sub-agent/prompt skill and cleanly reports that it requires Module O (execution is O's job)."""
        s = self.skills.get(name)
        if s is None:
            return f"Unknown skill: {name}"
        if not s["steps"]:
            return (f"Skill '{name}' is a sub-agent/prompt skill — execution requires Module O "
                    f"(sub-agents). Description: {s['description']}")
        out = [f"# skill: {name}"]
        for i, step in enumerate(s["steps"], 1):
            tool = step.get("tool")
            if not tool:
                out.append(f"[step {i}] skipped — step has no 'tool'")
                continue
            result = registry.dispatch_call(tool, json.dumps(step.get("arguments", {})), context=context)
            out.append(f"[step {i}] {tool}:\n{result}")
        return "\n\n".join(out)
