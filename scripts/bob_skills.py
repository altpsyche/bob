"""NE4 (contract C6) — the skills registry: discover, validate, list, and (simply) run named skills.

A *skill* is a higher-level, named workflow — distinct from an atomic *tool*. Manifest lives at
`skills/<name>/skill.yaml` with the frontier-standard shape `{name, description}` (name defaults to
the directory; only `description` is required — matching Claude Code SKILL.md / gemini-cli TOML, which
carry NO typed `params`), plus an optional `group` and an optional Bob extension `steps` — a list of
`{tool, arguments}` run in order for simple tool-sequence skills.

A skill WITHOUT `steps` is a prompt / sub-agent skill; its execution is Module O's job (O1 sub-agents).
O11 wires that execution in here: `run`/`run_events` drive an isolated `run_agent_events` sub-run whose
prompt is the manifest `description` (+ any user args), inheriting the O6 policy + O8 scopes carried by
the tool registry and O9 tracing from config. NE still owns the registry + catalog; O owns EXECUTION.
Mirrors
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

    def run(self, name: str, registry, config=None, context=None, args: str = "") -> str:
        """Blocking wrapper over `run_events` — returns the final synthesized string (O11).

        A `steps` skill runs its tool sequence exactly as before (config unused → byte-identical
        output). A no-`steps` (sub-agent) skill now runs as an isolated `run_agent_events` sub-run; it
        needs `config` and a reachable model (degrades to a clear message otherwise)."""
        result = ""
        for ev in self.run_events(name, registry, config=config, context=context, args=args):
            t = ev.get("type")
            if t == "final":
                result = ev.get("result") or ""
            elif t == "error":
                result = ev.get("message") or ""
        return result

    def run_events(self, name: str, registry, config=None, context=None, args: str = "",
                   cancel=None, approve=None, owner=None, scope=None, role=None):
        """Generator form (O11): yields event dicts so an event consumer (the NE shell, the server)
        renders a skill run live. A sub-agent skill re-yields `run_agent_events` events directly — so
        skill execution surfaces through the SAME event stream as any agent turn, never bespoke shell
        code (C6). The terminal event is always a `final` (or `error`)."""
        s = self.skills.get(name)
        if s is None:
            yield {"type": "final", "result": f"Unknown skill: {name}", "skill": name}
            return
        if s["steps"]:
            yield from self._run_steps_events(name, s, registry, context)
        else:
            yield from self._run_sub_agent_events(
                name, s, registry, config, args,
                cancel=cancel, approve=approve, owner=owner, scope=scope, role=role)

    def _run_steps_events(self, name: str, s: dict, registry, context):
        """Simple tool-sequence skill (NE4): dispatch each step in order. The assembled text in the
        terminal `final` is byte-identical to the pre-O11 `run` return value."""
        yield {"type": "skill_start", "skill": name, "mode": "steps", "steps": len(s["steps"])}
        out = [f"# skill: {name}"]
        for i, step in enumerate(s["steps"], 1):
            tool = step.get("tool")
            if not tool:
                out.append(f"[step {i}] skipped — step has no 'tool'")
                yield {"type": "skill_step", "index": i, "tool": None, "result": "no 'tool'"}
                continue
            args_json = json.dumps(step.get("arguments", {}))
            yield {"type": "tool_call", "call_id": f"{name}:{i}", "name": tool, "arguments": args_json}
            result = registry.dispatch_call(tool, args_json, context=context)
            out.append(f"[step {i}] {tool}:\n{result}")
            yield {"type": "tool_result", "call_id": f"{name}:{i}", "name": tool, "result": result}
        yield {"type": "final", "result": "\n\n".join(out), "skill": name}

    def _run_sub_agent_events(self, name: str, s: dict, registry, config, args: str, *,
                              cancel=None, approve=None, owner=None, scope=None, role=None):
        """Sub-agent skill (O11): the skill's prompt is its `description` (+ any user args). Runs a
        fresh, ISOLATED `run_agent_events` at depth 0 — so it can itself spawn sub-agents (O1),
        enforces the O6 policy + O8 scopes carried by `registry`, and traces via O9 — then the
        synthesized answer flows through as the terminal `final`. `run_agent_events` does its own
        `check_litellm` preflight, so an unreachable model degrades to an `error` event, never a crash."""
        yield {"type": "skill_start", "skill": name, "mode": "sub_agent"}
        if not config:
            # No agent runtime (e.g. a plain `run(name, registry)` with no config) — can't drive a
            # sub-run. Report clearly rather than pretend; the CLI/shell always pass config.
            yield {"type": "final", "skill": name,
                   "result": (f"Skill '{name}' is a sub-agent skill — run it through the agent "
                              f"runtime (e.g. `bob skill {name}`), which supplies the model config "
                              f"it needs.")}
            return
        task = self._compose_task(s, args)
        from bob_loop import run_agent_events
        agent_cfg = config.get("agent", {})
        yield from run_agent_events(
            task, config, role=role, agency=agent_cfg.get("agency", "show"),
            registry=registry, stream=False, history=None,
            cancel=cancel, approve=approve, owner=owner, scope=scope,
            agent_depth=0, run_id=f"skill:{name}",
        )

    @staticmethod
    def _compose_task(s: dict, args: str) -> str:
        """The sub-agent's task prompt: the manifest `description` (the skill's intent) plus any user
        input. There is NO prompt-body file (matches the NE4 contract) — the description IS the prompt."""
        task = s["description"]
        extra = (args or "").strip()
        if extra:
            task = f"{task}\n\nInput: {extra}"
        return task
