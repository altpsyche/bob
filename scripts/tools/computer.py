"""Bob tool: computer -- gated desktop automation (screen capture + input).

The most side-effecting capability in Bob, so it is gated hardest: it is offered ONLY when
agent.computerUse.enabled is set, every action REQUIRES_APPROVAL, and the input actions are declared
mutating. A screenshot is captured via the existing bob_vision core, downscaled with a single uniform
factor, and returned through the {"__images__": [...]} contract so the agent sees it in the vision role;
the same factor maps the model's click coordinates back to real screen pixels (bob_vision.to_screen).

Screenshots are treated as untrusted, model-controlled input (an on-screen prompt-injection surface) and
as an exfiltration surface, so capture is approval-gated even though it is a read. See docs/SECURITY.md.
"""
import json
import sys
import time
from pathlib import Path

_scripts_dir = str(Path(__file__).parent.parent)
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

import bob_vision  # noqa: E402
import osenv  # noqa: E402

_cfg: dict = {}
# The downscale factor of the most recent screenshot, so an input action can map the model's
# image-space coordinate back to real screen pixels. 1.0 until a screenshot has been taken.
_last_scale: float = 1.0
# Whether a screenshot has been taken this session: a click/move before one has no frame of reference.
_screenshotted: bool = False


_action_times: list = []   # monotonic timestamps of recent actions, for the per-minute rate limit


def _cu(config: dict) -> dict:
    return (config or {}).get("agent", {}).get("computerUse", {}) or {}


def halt_path() -> Path:
    """The kill-switch sentinel: while it exists, all computer-use actions refuse. A cross-process file is
    the only reliable out-of-band stop for a run that is holding the loop in-process."""
    return osenv.data_dir() / "computer_use.halt"


def is_halted() -> bool:
    return halt_path().exists()


def set_halt(on: bool) -> None:
    p = halt_path()
    if on:
        p.write_text("halted", encoding="utf-8")
    elif p.exists():
        p.unlink()


def _rate_ok() -> bool:
    limit = int(_cu(_cfg).get("maxActionsPerMinute", 60))
    if limit <= 0:
        return True
    now = time.monotonic()
    global _action_times
    _action_times = [t for t in _action_times if now - t < 60.0]
    if len(_action_times) >= limit:
        return False
    _action_times.append(now)
    return True


def _blocked():
    """A refusal message if computer-use must not act right now (kill switch or rate limit), else None."""
    if is_halted():
        return "computer-use is halted (kill switch active); run `bob computer status clear` to resume"
    if not _rate_ok():
        return "computer-use rate limit reached; raise agent.computerUse.maxActionsPerMinute to allow more"
    return None


def _notify(label: str) -> None:
    try:
        osenv.notify("Bob computer-use", label)   # on-screen indicator that the agent is acting
    except Exception:
        pass


def enabled(config: dict) -> bool:
    """Offered to the agent only when computer-use is explicitly enabled. Default off -> not loaded. In an
    unattended run (a detached task or a scheduled run, marked agent.unattended) it is additionally
    withheld unless allowUnattended is set -- so a background agent cannot drive the desktop by default,
    on top of the fail-closed approval floor."""
    cu = _cu(config)
    if not cu.get("enabled"):
        return False
    if (config or {}).get("agent", {}).get("unattended") and not cu.get("allowUnattended"):
        return False
    return True


def configure(config: dict) -> None:
    global _cfg
    _cfg = config or {}


REQUIRES_APPROVAL = True   # every computer-use action prompts through the permission model


def _computer_screenshot(**_kw) -> str:
    """Capture the screen and return it through the image contract for the agent to inspect. Records the
    downscale factor so subsequent input actions map coordinates correctly."""
    global _last_scale, _screenshotted
    blocked = _blocked()
    if blocked:
        return blocked
    _notify("screenshot")
    cu = _cu(_cfg)
    try:
        raw = bob_vision.capture_screen()
    except RuntimeError as e:
        return f"computer-use screenshot unavailable: {e}"
    path, scale, dims = bob_vision.resize_for_control(
        raw, max_long_edge=int(cu.get("maxLongEdge", 1280)),
        max_pixels=int(cu.get("maxPixels", 1_150_000)))
    _last_scale = scale
    _screenshotted = True
    where = f"{dims[0]}x{dims[1]} px" if dims else "the screen"
    return json.dumps({"__images__": [path], "text": f"screen captured; the view is {where}"})


def _to_screen(coordinate):
    """Map a [x, y] the model gave (in the last screenshot's pixel space) to real screen pixels."""
    return bob_vision.to_screen((int(coordinate[0]), int(coordinate[1])), _last_scale)


def _computer_click(**kw) -> str:
    if not _screenshotted:
        return "take a screenshot first: click coordinates are relative to the last captured view"
    blocked = _blocked()
    if blocked:
        return blocked
    x, y = _to_screen(kw["coordinate"])
    _notify(f"click ({x}, {y})")
    try:
        osenv.input_click(x, y, kw.get("button", "left"))
    except RuntimeError as e:
        return f"computer-use input unavailable: {e}"
    return f"clicked ({x}, {y})"


def _computer_move(**kw) -> str:
    if not _screenshotted:
        return "take a screenshot first: move coordinates are relative to the last captured view"
    blocked = _blocked()
    if blocked:
        return blocked
    x, y = _to_screen(kw["coordinate"])
    _notify(f"move ({x}, {y})")
    try:
        osenv.input_move(x, y)
    except RuntimeError as e:
        return f"computer-use input unavailable: {e}"
    return f"moved to ({x}, {y})"


def _computer_type(**kw) -> str:
    blocked = _blocked()
    if blocked:
        return blocked
    _notify("type")
    try:
        osenv.input_type(str(kw.get("text", "")))
    except RuntimeError as e:
        return f"computer-use input unavailable: {e}"
    return "typed"


def _computer_key(**kw) -> str:
    blocked = _blocked()
    if blocked:
        return blocked
    _notify(f"key {kw.get('keys', '')}")
    try:
        osenv.input_key(str(kw.get("keys", "")))
    except RuntimeError as e:
        return f"computer-use input unavailable: {e}"
    return "sent keys"


def _computer_scroll(**kw) -> str:
    blocked = _blocked()
    if blocked:
        return blocked
    _notify("scroll")
    try:
        osenv.input_scroll(int(kw.get("dx", 0)), int(kw.get("dy", 0)))
    except RuntimeError as e:
        return f"computer-use input unavailable: {e}"
    return "scrolled"


MUTATING_TOOLS = {"computer_click", "computer_type", "computer_key", "computer_scroll", "computer_move"}


def _preview_click(args):
    x, y = _to_screen(args.get("coordinate", [0, 0]))
    mx, my = args.get("coordinate", [0, 0])
    return f"click model({mx},{my}) -> screen({x},{y}) {args.get('button', 'left')} button"


def _preview_move(args):
    x, y = _to_screen(args.get("coordinate", [0, 0]))
    return f"move to screen({x},{y})"


def _preview_type(args):
    n = len(str(args.get("text", "")))
    return f"type {n} character(s)"   # never echo the literal text (may be a secret)


def _preview_key(args):
    return f"key {args.get('keys', '')}"


def _preview_scroll(args):
    return f"scroll dx={args.get('dx', 0)} dy={args.get('dy', 0)}"


PREVIEW = {
    "computer_click": _preview_click,
    "computer_move": _preview_move,
    "computer_type": _preview_type,
    "computer_key": _preview_key,
    "computer_scroll": _preview_scroll,
}


TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "computer_screenshot",
            "description": (
                "Capture the current screen and return it as an image to inspect. Requires the "
                "computer-use capability to be enabled and your confirmation. Coordinates you give for "
                "later clicks are in the pixel space of the image returned here."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "computer_click",
            "description": ("Click at a coordinate in the last screenshot's pixel space. Take a "
                            "screenshot first. Requires your confirmation."),
            "parameters": {
                "type": "object",
                "properties": {
                    "coordinate": {"type": "array", "items": {"type": "integer"},
                                   "description": "[x, y] in the last screenshot's pixels"},
                    "button": {"type": "string", "enum": ["left", "middle", "right"]},
                },
                "required": ["coordinate"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "computer_move",
            "description": ("Move the pointer to a coordinate in the last screenshot's pixel space. "
                            "Requires your confirmation."),
            "parameters": {
                "type": "object",
                "properties": {
                    "coordinate": {"type": "array", "items": {"type": "integer"},
                                   "description": "[x, y] in the last screenshot's pixels"},
                },
                "required": ["coordinate"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "computer_type",
            "description": "Type literal text at the current focus. Requires your confirmation.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string", "description": "The text to type"}},
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "computer_key",
            "description": ("Press a key or chord, for example 'Return' or 'ctrl+c'. Requires your "
                            "confirmation."),
            "parameters": {
                "type": "object",
                "properties": {"keys": {"type": "string", "description": "Key or chord to press"}},
                "required": ["keys"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "computer_scroll",
            "description": "Scroll by (dx, dy) steps; positive dy scrolls down. Requires your confirmation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "dx": {"type": "integer", "description": "Horizontal steps"},
                    "dy": {"type": "integer", "description": "Vertical steps (positive scrolls down)"},
                },
                "required": [],
            },
        },
    },
]

DISPATCH = {
    "computer_screenshot": _computer_screenshot,
    "computer_click": _computer_click,
    "computer_move": _computer_move,
    "computer_type": _computer_type,
    "computer_key": _computer_key,
    "computer_scroll": _computer_scroll,
}
