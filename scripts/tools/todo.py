"""Bob tool: todo — a living task list the model maintains across steps (O16, Manus/TodoWrite style).

Gated on agent.todoTool (default false) so the default toolset is byte-identical to pre-O16. State is
RUN-LOCAL (RunContext.todos, reached via get_run_context()) — it is NOT persisted as memory and does
NOT mutate any store, so it's deliberately kept out of MUTATING_TOOLS (O2 can run it concurrently, O6
never gates it). The O16 recitation hook in run_agent_events re-emits the open items at the context tail
each step; O4's plan phase can seed the list.
"""

_cfg: dict = {}


def enabled(config: dict) -> bool:
    """Feature gate (read by ToolRegistry): only offered when agent.todoTool is on, so with it off the
    default toolset — and every prompt — is unchanged."""
    return bool(config.get("agent", {}).get("todoTool", False))


def configure(config: dict) -> None:
    global _cfg
    _cfg = config


def _ctx_todos():
    """The current run's TODO list (RunContext.todos), or None outside a dispatched agent call."""
    from tool_registry import get_run_context
    ctx = get_run_context()
    if ctx is None:
        return None
    todos = getattr(ctx, "todos", None)
    if todos is None:
        try:
            ctx.todos = []
            todos = ctx.todos
        except Exception:
            return None
    return todos


_STATUSES = ("pending", "in_progress", "done")


def _render(todos) -> str:
    if not todos:
        return "(todo list is empty)"
    return "\n".join(f"{i}. [{t.get('status', 'pending')}] {t.get('task', '')}"
                     for i, t in enumerate(todos, 1))


def _todo_write(items) -> str:
    """Replace the whole list. `items` is a list of task strings, or of {task, status} objects."""
    todos = _ctx_todos()
    if todos is None:
        return "todo is unavailable outside an agent run."
    new = []
    for it in (items or []):
        if isinstance(it, dict):
            task = str(it.get("task", "")).strip()
            status = it.get("status", "pending")
            status = status if status in _STATUSES else "pending"
        else:
            task, status = str(it).strip(), "pending"
        if task:
            new.append({"task": task, "status": status})
    todos[:] = new   # mutate in place so the RunContext's list object stays the one the loop recites
    return "TODO list set:\n" + _render(todos)


def _todo_update(task: str, status: str = "done") -> str:
    """Set the status of the item matching `task` (exact or substring); append it if it's new."""
    todos = _ctx_todos()
    if todos is None:
        return "todo is unavailable outside an agent run."
    status = status if status in _STATUSES else "done"
    key = str(task).strip().lower()
    for t in todos:
        if t["task"].lower() == key or (key and key in t["task"].lower()):
            t["status"] = status
            break
    else:
        todos.append({"task": str(task).strip(), "status": status})
    return "TODO updated:\n" + _render(todos)


def test() -> str:
    return _todo_write(["sanity check"])   # no RunContext in `bob test` -> graceful message


TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "todo_write",
            "description": ("Set your working task list for this job (replaces the whole list). Use it to "
                            "break a multi-step task into steps up front and keep it current — it is "
                            "re-shown to you each step so you don't lose track."),
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "description": "The full task list, in order.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "task": {"type": "string"},
                                "status": {"type": "string", "enum": list(_STATUSES)},
                            },
                            "required": ["task"],
                        },
                    },
                },
                "required": ["items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo_update",
            "description": "Update one task's status (e.g. mark it done or in_progress) without rewriting the list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "The task text (exact or a substring)."},
                    "status": {"type": "string", "enum": list(_STATUSES),
                               "description": "New status (default 'done')."},
                },
                "required": ["task"],
            },
        },
    },
]

DISPATCH = {"todo_write": _todo_write, "todo_update": _todo_update}
