"""Bob agent scheduling capabilities (ONE-C Slice 5) — the agent-lifecycle scheduler.

Functional grouping (D6): one module, several related cores, each reached the standard three ways. Ports
the bob.ps1 `agent schedule|log|install|uninstall|status` cases + the `bob-agent.ps1` runner + the exact
`Test-CronDue` semantics (retired from _models.ps1). Two layers:

  Layer 1 (OS task)   osenv.register_agent_task / unregister / agent_task_status / crontab_available —
                      the every-minute task that fires scripts/bob_agent_runner.py.
  Layer 2 (this file) cron_due() (exact Test-CronDue port) + data/schedules.json CRUD + run_due_schedules()
                      (the runner core: evaluate cron, run_agent in-process, persist result, notify).

D3: cron_due is the EXACT Test-CronDue port — UTC, 5 fields, '*' / comma lists / 'a-b' ranges / integers
only (NO '*/n' steps, NO JAN/MON names), day-of-week Sunday=0, and a 60-second re-fire guard. D4: the
writable schedule store is data/schedules.json (data/-side state), atomic-written."""
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

_cfg: dict = {}

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO / "scripts"

MUTATING_TOOLS = {"schedule_add", "schedule_remove", "schedule_enable", "schedule_disable", "schedule_run"}


def configure(config: dict) -> None:
    global _cfg
    _cfg = config
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))


# --- cron evaluation (exact Test-CronDue port, D3) ------------------------------------------------

def cron_due(cron: str, now: datetime, last_run: datetime = None) -> bool:
    """True if a 5-field cron expression is due at `now` (UTC), given `last_run` (None = never). Exact
    port of Test-CronDue: '*' / comma lists / 'a-b' ranges / bare integers only — no '*/n', no month/day
    names. Day-of-week is Sunday=0 (.NET DayOfWeek). A 60-second guard prevents double-firing within a
    minute."""
    if last_run is not None and (now - last_run).total_seconds() < 60:
        return False
    fields = cron.split()
    if len(fields) != 5:
        print(f"cron_due: expected 5 fields, got {len(fields)} in '{cron}'", file=sys.stderr)
        return False

    def field_match(field: str, val: int) -> bool:
        if field == "*":
            return True
        for part in field.split(","):
            m = re.match(r"^(\d+)-(\d+)$", part)
            if m and int(m.group(1)) <= val <= int(m.group(2)):
                return True
            if re.match(r"^\d+$", part) and int(part) == val:
                return True
        return False

    return (field_match(fields[0], now.minute) and
            field_match(fields[1], now.hour) and
            field_match(fields[2], now.day) and
            field_match(fields[3], now.month) and
            field_match(fields[4], now.isoweekday() % 7))  # isoweekday: Mon=1..Sun=7 -> Sun=0..Sat=6


# --- schedule store (data/schedules.json, atomic; D4) ---------------------------------------------

def _sched_file(config: dict) -> Path:
    rel = config.get("agent", {}).get("scheduleFile", "data/schedules.json").replace("\\", "/")
    return REPO / rel


def _read_schedules(config: dict) -> list:
    f = _sched_file(config)
    if not f.exists():
        return []
    import json
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _write_schedules(config: dict, data: list) -> None:
    import json
    f = _sched_file(config)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(f".{_pid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(f)  # atomic on the same filesystem


def _pid() -> int:
    import os
    return os.getpid()


def _parse_ts(value) -> datetime:
    """Parse a stored ISO timestamp to an aware UTC datetime, or None. Accepts both Python's '+00:00'
    and PowerShell's trailing 'Z'; a naive stamp is assumed UTC."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _role_for(entry: dict, config: dict) -> str:
    return (entry.get("action", {}).get("role")
            or config.get("routing", {}).get("agentRole")
            or "chat")


def _runner_path() -> Path:
    return SCRIPTS / "bob_agent_runner.py"


def _log_file(config: dict) -> Path:
    rel = config.get("agent", {}).get("logFile", "logs/bob-agent.log").replace("\\", "/")
    return REPO / rel


def _run_goal(goal: str, role: str, config: dict) -> str:
    """Run one agent goal in-process (silent) and return the trimmed final answer. In-process (not a
    subprocess) now that the loop is Python — bob_loop.run_agent returns (result, exit_requested)."""
    import bob_loop
    result, _ = bob_loop.run_agent(goal, config, role=role, agency="silent")
    return (result or "").strip()


# --- CRUD cores -----------------------------------------------------------------------------------

def schedule_list(config: dict) -> str:
    """Tabular view of all schedules. Read-only. Port of the `schedule list` case."""
    s = _read_schedules(config)
    if not s:
        return "No schedules. Add: bob agent schedule add <name> --cron <expr> --goal <text>"
    lines = [f"{'Name':<20} {'Cron':<16} {'On':<5} {'LastRun':<14} Result", "-" * 80]
    for e in s:
        last = _parse_ts(e.get("lastRun"))
        last_s = last.astimezone().strftime("%m-%d %H:%M") if last else "-"
        res = e.get("lastRunResult") or "-"
        res = res[:50].replace("\n", " ")
        lines.append(f"{e.get('name', ''):<20} {e.get('cron', ''):<16} "
                     f"{str(bool(e.get('enabled'))):<5} {last_s:<14} {res}")
    return "\n".join(lines)


def schedule_add(config: dict, name: str, cron: str = "0 9 * * 1-5", goal: str = None,
                 role: str = "agent", notify: bool = False, title: str = None) -> str:
    """Add a schedule and auto-register the OS task if not already registered. Port of `schedule add`."""
    if not name:
        return "Usage: bob agent schedule add <name> --cron <expr> --goal <text>"
    s = _read_schedules(config)
    if any(e.get("name") == name for e in s):
        return f"Schedule '{name}' already exists."
    s.append({
        "name": name, "cron": cron,
        "action": {"type": "agent", "goal": goal or name, "role": role},
        "notify": bool(notify), "notifyTitle": title or name, "enabled": True,
        "lastRun": None, "lastRunResult": None, "createdAt": _now().isoformat(),
    })
    _write_schedules(config, s)
    lines = [f"Added '{name}'  cron: {cron}"]
    import osenv
    if not osenv.agent_task_status()["registered"]:
        try:
            osenv.register_agent_task(str(osenv.venv_exe("venv-litellm", "python")), str(_runner_path()))
            lines.append("BobAgent task auto-registered.")
        except RuntimeError as e:
            lines.append(f"(schedule saved, but auto-register failed: {e})")
    return "\n".join(lines)


def schedule_remove(config: dict, name: str) -> str:
    """Remove a schedule by name. Port of `schedule remove`."""
    if not name:
        return "Usage: bob agent schedule remove <name>"
    s = _read_schedules(config)
    kept = [e for e in s if e.get("name") != name]
    if len(kept) == len(s):
        return f"Not found: {name}"
    _write_schedules(config, kept)
    return f"Removed '{name}'."


def _set_enabled(config: dict, name: str, on: bool) -> str:
    if not name:
        return f"Usage: bob agent schedule {'enable' if on else 'disable'} <name>"
    s = _read_schedules(config)
    found = False
    for e in s:
        if e.get("name") == name:
            e["enabled"] = on
            found = True
    if not found:
        return f"Not found: {name}"
    _write_schedules(config, s)
    return f"{'Enabled' if on else 'Disabled'}: {name}"


def schedule_enable(config: dict, name: str) -> str:
    """Enable a schedule. Port of `schedule enable`."""
    return _set_enabled(config, name, True)


def schedule_disable(config: dict, name: str) -> str:
    """Disable a schedule (keeps it, stops firing). Port of `schedule disable`."""
    return _set_enabled(config, name, False)


def schedule_run(config: dict, name: str) -> str:
    """Run one schedule NOW regardless of its cron, persist lastRun/lastRunResult, notify if configured.
    Port of `schedule run`. Mutating (fires the agent + writes the store)."""
    if not name:
        return "Usage: bob agent schedule run <name>"
    entry = next((e for e in _read_schedules(config) if e.get("name") == name), None)
    if not entry:
        return f"Schedule not found: {name}"
    role = _role_for(entry, config)
    result = _run_goal(entry.get("action", {}).get("goal", name), role, config)
    if entry.get("notify") and result:
        import osenv
        osenv.notify(entry.get("notifyTitle") or entry.get("name"), result)
    max_chars = int(config.get("agent", {}).get("maxResultChars", 500) or 500)
    s = _read_schedules(config)
    for e in s:
        if e.get("name") == name:
            e["lastRun"] = _now().isoformat()
            e["lastRunResult"] = result[:max_chars]
    _write_schedules(config, s)
    return f"Ran '{name}':\n{result}" if result else f"Ran '{name}' (no output)."


# --- the runner core (fired by scripts/bob_agent_runner.py every minute) --------------------------

def run_due_schedules(config: dict) -> str:
    """Evaluate every enabled schedule against the current UTC minute and run those due. Persists
    lastRun/lastRunResult (atomic, only if something ran), logs, and notifies. Returns a one-line
    summary. Port of the bob-agent.ps1 runner body."""
    if not config.get("agent", {}).get("enabled"):
        return "agent disabled (set agent.enabled = true)"
    schedules = _read_schedules(config)
    if not schedules:
        return "no schedules"
    now = _now()
    max_chars = int(config.get("agent", {}).get("maxResultChars", 500) or 500)
    log = _log_file(config)
    log.parent.mkdir(parents=True, exist_ok=True)
    ran = []
    changed = False
    for entry in schedules:
        if not entry.get("enabled"):
            continue
        if not cron_due(entry.get("cron", ""), now, _parse_ts(entry.get("lastRun"))):
            continue
        name = entry.get("name", "?")
        role = _role_for(entry, config)
        _append_log(log, f"[{now.isoformat()}] Running: {name}")
        try:
            result = _run_goal(entry.get("action", {}).get("goal", name), role, config)
        except Exception as e:  # a single bad goal must not abort the whole tick
            result = f"Agent error: {e}"
            _append_log(log, f"ERROR: {result}")
        entry["lastRun"] = now.isoformat()
        entry["lastRunResult"] = result[:max_chars]
        changed = True
        if entry.get("notify") and result:
            import osenv
            osenv.notify(entry.get("notifyTitle") or name, result)
        _append_log(log, f"[{now.isoformat()}] Done: {name}")
        ran.append(name)
    if changed:
        _write_schedules(config, schedules)
    return f"ran {len(ran)}: {', '.join(ran)}" if ran else "nothing due"


def _append_log(log: Path, line: str) -> None:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# --- agent OS-task lifecycle (install/uninstall CLI-only; status/log also agent tools) ------------

def agent_install(config: dict) -> str:
    """Register the every-minute BobAgent OS task (fires the Python runner). Port of `agent install`."""
    import osenv
    try:
        osenv.register_agent_task(str(osenv.venv_exe("venv-litellm", "python")), str(_runner_path()))
    except RuntimeError as e:
        return str(e)
    return ("BobAgent task registered (runs every minute).\n"
            "Enable proactive mode: set agent.enabled = true in config/user.json")


def agent_uninstall(config: dict) -> str:
    """Remove the BobAgent OS task. Port of `agent uninstall`."""
    import osenv
    osenv.unregister_agent_task()
    return "BobAgent task removed."


def agent_status(config: dict) -> str:
    """BobAgent task registration/state + a short tail of the runner log. Port of `agent status`.
    Read-only."""
    import osenv
    st = osenv.agent_task_status()
    if not st["registered"]:
        return "BobAgent: not registered. Run: bob agent install"
    lines = [f"BobAgent: {st['state']}"]
    if st.get("next_run"):
        lines.append(f"Next run: {st['next_run']}")
    tail = _tail(_log_file(config), 5)
    if tail:
        lines += ["", "Recent log:"] + tail
    return "\n".join(lines)


def agent_log(config: dict, n: int = 50) -> str:
    """The last `n` lines of the runner log (bounded read; the `-Wait` follow stays a CLI concern).
    Port of `agent log`."""
    log = _log_file(config)
    if not log.exists():
        return f"No log yet: {log}"
    return "\n".join(_tail(log, n)) or f"(empty) {log}"


def _tail(path: Path, n: int) -> list:
    if not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        return []


# --- agent tool adapters --------------------------------------------------------------------------

def _schedule_list() -> str:
    return schedule_list(_cfg)


def _schedule_add(name: str, cron: str = "0 9 * * 1-5", goal: str = None, role: str = "agent",
                  notify: bool = False, title: str = None) -> str:
    return schedule_add(_cfg, name, cron=cron, goal=goal, role=role, notify=notify, title=title)


def _schedule_remove(name: str) -> str:
    return schedule_remove(_cfg, name)


def _schedule_enable(name: str) -> str:
    return schedule_enable(_cfg, name)


def _schedule_disable(name: str) -> str:
    return schedule_disable(_cfg, name)


def _schedule_run(name: str) -> str:
    return schedule_run(_cfg, name)


def _agent_task_status() -> str:
    return agent_status(_cfg)


def _agent_log(n: int = 50) -> str:
    return agent_log(_cfg, n=n)


TOOL_DEFS = [
    {"type": "function", "function": {
        "name": "schedule_list",
        "description": "List all scheduled agent goals with cron, enabled state, last run, and last result. Read-only.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "schedule_add",
        "description": ("Add a recurring agent goal. cron is a 5-field UTC expression (min hour day month "
                        "dow, Sunday=0; '*'/comma/'a-b' ranges only). Auto-registers the OS task."),
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Unique schedule name."},
            "cron": {"type": "string", "description": "5-field UTC cron (default '0 9 * * 1-5')."},
            "goal": {"type": "string", "description": "The agent goal to run (defaults to the name)."},
            "role": {"type": "string", "description": "Model role (default 'agent')."},
            "notify": {"type": "boolean", "description": "Desktop-notify on completion."},
            "title": {"type": "string", "description": "Notification title (defaults to the name)."}},
            "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "schedule_remove",
        "description": "Remove a schedule by name.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Schedule name to remove."}}, "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "schedule_enable",
        "description": "Enable a schedule so it fires on its cron.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Schedule name to enable."}}, "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "schedule_disable",
        "description": "Disable a schedule (kept, but stops firing).",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Schedule name to disable."}}, "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "schedule_run",
        "description": "Run one schedule's goal NOW regardless of its cron, and record the result. Fires the agent.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Schedule name to run now."}}, "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "agent_task_status",
        "description": "Report whether the every-minute BobAgent OS task is registered, plus a short tail of the runner log. Read-only.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "agent_log",
        "description": "Return the last N lines of the scheduled-agent runner log. Read-only.",
        "parameters": {"type": "object", "properties": {
            "n": {"type": "integer", "description": "How many trailing lines (default 50)."}}}}},
]

DISPATCH = {
    "schedule_list": _schedule_list, "schedule_add": _schedule_add, "schedule_remove": _schedule_remove,
    "schedule_enable": _schedule_enable, "schedule_disable": _schedule_disable, "schedule_run": _schedule_run,
    "agent_task_status": _agent_task_status, "agent_log": _agent_log,
}
