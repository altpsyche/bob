"""The shell's theme: config → a typed, console-resolved `Theme`.

The look is a self-contained, testable unit, separate from the REPL. Precedence (deep-merged,
lowest→highest): built-in `_DEFAULT_UI` ← `config/ui.json` (the shipped, user-editable file — the
one editing surface) ← `config['ui']` (a runtime override from user.json / data config). A partial
file/override is fine; a deleted file still renders from the defaults.

`Theme.load(config, console)` parses everything ONCE — colours, header spec, spacing, flags — and
resolves glyphs against the console's encoding (ASCII fallback on a non-UTF-8 terminal). The shell and
renderer then read typed fields (`theme.accent`, `theme.gear`, …), never the raw dict, so per-turn
rendering does no parsing.
"""
import json
from dataclasses import dataclass
from pathlib import Path

from bob_config import _deep_merge   # the ONE deep-merge helper (was a third private copy here)

_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"   # scripts/bob/theme.py -> repo/config
UI_FILE = _CONFIG_DIR / "ui.json"

# Built-in defaults — the single source of default values (config/ui.json ships a copy to edit).
_DEFAULT_UI = {
    "header": {
        "enabled": True,
        "text": "BOB",
        "font": "delta_corps_priest_1",              # any pyfiglet font; solid blocks + a directional B
        "align": "center",                           # left | center (also aligns the splash meta/tip)
        "gradient": ["#F4DBFB", "#D7A6EC", "#B579DC", "#8B54AE"],  # hex stops, bright→deep (mauve)
        "gradient_dir": "diagonal",                  # vertical | horizontal | diagonal (a sheen)
    },
    "colors": {                                      # rich style strings — one palette, accent ties it
        "accent": "#C48CD6",
        "success": "#8FD98F",
        "error": "#E86A8C",
        "warn": "#E8C06A",
        "tool": "#C48CD6",
        "muted": "grey58",
    },
    "glyphs": {"gear": "⚙", "ok": "✓", "bad": "✗", "dot": "●", "arrow": "›", "spinner": "dots"},
    "spacing": {
        "panel_padding": [0, 1],                     # rich Panel padding [vertical, horizontal]
        "header_margin": 1,                          # blank lines after the header
        "blank_between_turns": True,                 # a blank line before each turn's output
    },
    "prompt": "bob",                                 # prompt label before the arrow
    "tagline": "a private AI, running on your machine",  # subtitle under the header ("" hides it)
    "markdown": True,                                # render assistant answers as Markdown
    "meta_panel": False,                             # box the meta line (False = cleaner: bare + a rule)
    "rule": True,                                    # a faint accent rule under the splash
    "prose_width": 92,                               # cap assistant-answer width for readability (0 = full)
}

_ASCII_GLYPHS = {"gear": "*", "ok": "+", "bad": "x", "dot": "*", "arrow": ">", "spinner": "line"}
_ANSI_NAMES = {"black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"}


# --- merge + load ----------------------------------------------------------------------------------

def load_ui(config=None) -> dict:
    """Merged theme dict: `_DEFAULT_UI` ← `config/ui.json` ← `config['ui']`."""
    ui = dict(_DEFAULT_UI)
    try:
        if UI_FILE.exists():
            ui = _deep_merge(ui, json.loads(UI_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    return _deep_merge(ui, (config or {}).get("ui", {}))


# --- colour / gradient -----------------------------------------------------------------------------

def _parse_hex(s: str):
    try:
        s = s.lstrip("#")
        return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except Exception:
        return 255, 255, 255


def lerp_color(stops: list, pos: float) -> str:
    """The hex colour at position `pos` (0..1) along the gradient stops."""
    rgb = [_parse_hex(s) for s in (stops or ["#FFFFFF"])] or [(255, 255, 255)]
    if len(rgb) == 1:
        return "#%02x%02x%02x" % rgb[0]
    pos = min(max(pos, 0.0), 1.0)
    segs = len(rgb) - 1
    x = pos * segs
    lo = min(int(x), segs - 1)
    frac = x - lo
    a, b = rgb[lo], rgb[lo + 1]
    return "#%02x%02x%02x" % tuple(round(a[j] + (b[j] - a[j]) * frac) for j in range(3))


def gradient(stops: list, n: int) -> list:
    """n hex colours evenly interpolated across the stops."""
    if n <= 1:
        return [lerp_color(stops, 0.0)]
    return [lerp_color(stops, i / (n - 1)) for i in range(n)]


def ptk_color(name: str) -> str:
    """Map a rich colour name/hex to a prompt_toolkit colour token (ansi names need an 'ansi' prefix)."""
    if name in _ANSI_NAMES:
        return f"ansi{name}"
    return name if name.startswith("#") else "ansicyan"


# --- glyphs (unicode-aware) ------------------------------------------------------------------------

def unicode_ok(console) -> bool:
    """True if the console's encoding can render the glyphs — else callers fall back to ASCII so a
    non-UTF-8 terminal degrades cleanly instead of crashing."""
    enc = getattr(getattr(console, "file", None), "encoding", None) or "utf-8"
    try:
        "⚙●✓✗⠋".encode(enc)
        return True
    except Exception:
        return False


def resolve_glyphs(ui: dict, console) -> dict:
    """Configured glyphs when the console can render them, else the ASCII set."""
    return _deep_merge(_ASCII_GLYPHS, ui.get("glyphs", {})) if unicode_ok(console) else dict(_ASCII_GLYPHS)


# --- the Theme -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Header:
    enabled: bool
    text: str
    font: str
    align: str            # left | center
    gradient: tuple       # hex stops
    gradient_dir: str     # vertical | horizontal | diagonal


@dataclass(frozen=True)
class Theme:
    header: Header
    # colours (rich style strings)
    accent: str
    success: str
    error: str
    warn: str
    tool: str
    muted: str
    # glyphs (already console-resolved)
    gear: str
    ok: str
    bad: str
    dot: str
    arrow: str
    spinner: str
    # layout / spacing
    panel_padding: tuple
    header_margin: int
    blank_between_turns: bool
    prompt: str
    tagline: str
    markdown: bool
    meta_panel: bool
    rule: bool
    prose_width: int
    source: str           # path to the editable ui.json (for /theme + docs)

    @property
    def centered(self) -> bool:
        return self.header.align == "center"

    @property
    def prompt_style(self) -> dict:
        """prompt_toolkit style map for the input line + toolbar, all derived from the accent so a
        re-themed accent recolours the whole prompt (dark accent-tinted toolbar bg, light accent fg)."""
        return {
            "prompt": f"bold {ptk_color(self.accent)}",
            "arrow": ptk_color(self.success),
            "bottom-toolbar": f"bg:{lerp_color([self.accent, '#000000'], 0.82)} "
                              f"{lerp_color([self.accent, '#ffffff'], 0.5)}",
        }

    @classmethod
    def load(cls, config=None, console=None) -> "Theme":
        ui = load_ui(config)
        h, c, sp = ui["header"], ui["colors"], ui["spacing"]
        g = resolve_glyphs(ui, console)
        return cls(
            header=Header(
                enabled=bool(h.get("enabled", True)),
                text=h.get("text", "BOB"),
                font=h.get("font", "delta_corps_priest_1"),
                align=h.get("align", "center"),
                gradient=tuple(h.get("gradient", ["#FFFFFF"])),
                gradient_dir=h.get("gradient_dir", "diagonal"),
            ),
            accent=c["accent"], success=c["success"], error=c["error"],
            warn=c["warn"], tool=c["tool"], muted=c["muted"],
            gear=g["gear"], ok=g["ok"], bad=g["bad"], dot=g["dot"], arrow=g["arrow"], spinner=g["spinner"],
            panel_padding=tuple(sp.get("panel_padding", [0, 1])),
            header_margin=int(sp.get("header_margin", 1)),
            blank_between_turns=bool(sp.get("blank_between_turns", True)),
            prompt=ui.get("prompt", "bob"),
            tagline=ui.get("tagline", ""),
            markdown=bool(ui.get("markdown", True)),
            meta_panel=bool(ui.get("meta_panel", False)),
            rule=bool(ui.get("rule", True)),
            prose_width=int(ui.get("prose_width", 92)),
            source=str(UI_FILE),
        )


# --- header rendering ------------------------------------------------------------------------------

def render_header(theme: Theme, console) -> None:
    """The big wordmark: pyfiglet font + per-character gradient, centred as one block. Degrades to a
    bold line if pyfiglet/font is unavailable, and to an ASCII-safe font on a non-unicode console."""
    h = theme.header
    if not h.enabled:
        return
    font = h.font if unicode_ok(console) else "big"          # block fonts need UTF-8
    try:
        from pyfiglet import Figlet
        art = Figlet(font=font, width=max(20, console.width or 80)).renderText(h.text)
    except Exception:
        console.print(f"[bold {theme.accent}]{h.text}[/]")
        return
    lines = art.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        console.print(f"[bold {theme.accent}]{h.text}[/]")
        return

    from rich.cells import cell_len
    from rich.text import Text
    # figlet pads lines to its canvas width with ragged trailing space, which drifts a centred string
    # off-axis. Strip it, then normalize every line to the exact block width so the block centres as one
    # unit (aligned with the rule/meta below).
    glyphs = [ln.rstrip() for ln in lines]
    rows = len(glyphs)
    cols = max((cell_len(g) for g in glyphs), default=1)
    justify = "center" if theme.centered else "left"
    for r, ln in enumerate(glyphs):
        t = Text(no_wrap=True)
        for cidx, ch in enumerate(ln):
            vy = r / (rows - 1) if rows > 1 else 0.0
            vx = cidx / (cols - 1) if cols > 1 else 0.0
            pos = vx if h.gradient_dir == "horizontal" else vy if h.gradient_dir == "vertical" else (vx + vy) / 2
            t.append(ch, style=lerp_color(h.gradient, pos))
        gap = cols - cell_len(ln)
        if gap > 0:
            t.append(" " * gap)          # uniform width → rich centres every line identically
        console.print(t, justify=justify, crop=False)
