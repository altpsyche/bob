"""Bob tool: budget_summary — token/cost usage summary.

Read-only: queries the LiteLLM proxy for spend, reads the configured
limits from config/litellm.yaml, reports the local memory-DB size (always $0 — fully local), and links
Langfuse if it's up. One core fn (budget_summary(config)) reached three ways: the agent tool below, the
`bob budget` verb (cli._handle_budget), and `bob --run budget_summary` — no duplicated logic."""
import re
import sys
from pathlib import Path

_cfg: dict = {}

REPO = Path(__file__).resolve().parent.parent.parent


def configure(config: dict) -> None:
    global _cfg
    _cfg = config
    scripts_dir = str(Path(__file__).parent.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)


def _read_litellm_limits() -> tuple:
    """(max_budget, budget_duration) from config/litellm.yaml, or (None, None). Regex-parsed
    (no YAML dep for two scalars)."""
    cfg_file = REPO / "config" / "litellm.yaml"
    if not cfg_file.exists():
        return (None, None)
    raw = cfg_file.read_text(encoding="utf-8", errors="replace")
    max_b = re.search(r"max_budget:\s*(\S+)", raw)
    dur = re.search(r'budget_duration:\s*"?([^"\r\n]+)"?', raw)
    return (max_b.group(1) if max_b else None,
            dur.group(1).strip() if dur else None)


def budget_summary(config: dict) -> str:
    """The core capability: a formatted budget/usage report. Never raises on an unreachable service —
    a down LiteLLM/Langfuse degrades to a hint."""
    import requests
    from bob_core import _get_db_path, _port

    litellm_port = _port(config, "litellmPort")
    base = f"http://localhost:{litellm_port}"
    lines = ["", "Bob Budget", "-" * 40]

    # LiteLLM health + all-time spend.
    litellm_up = False
    try:
        requests.get(f"{base}/health", timeout=3)
        litellm_up = True
    except requests.RequestException:
        pass
    if litellm_up:
        try:
            g = requests.get(f"{base}/global/spend", timeout=5).json()
            if g and g.get("spend") is not None:
                lines.append(f"{'Spend (all-time)':<20} ${round(float(g['spend']), 4)}")
        except (requests.RequestException, ValueError):
            pass
    else:
        lines.append("LiteLLM not running — start with: bob litellm")

    max_budget, budget_duration = _read_litellm_limits()
    lines += ["", "Configured limits:"]
    if max_budget:
        lines.append(f"  {'Max budget:':<18} ${max_budget}")
    if budget_duration:
        lines.append(f"  {'Period:':<18} {budget_duration}")

    # Local memory DB size — always $0 (fully local).
    db_path = Path(_get_db_path(config))
    if db_path.exists():
        db_kb = round(db_path.stat().st_size / 1024, 1)
        lines += ["", f"Local memory DB: {db_path} ({db_kb} KB)  [cost: $0 — fully local]"]

    # Langfuse tracing link, if it's up.
    try:
        lf_port = _port(config, "langfusePort")
        requests.get(f"http://localhost:{lf_port}/api/public/health", timeout=2)
        lines += ["", f"Langfuse tracing: http://localhost:{lf_port}  (detailed per-request logs)"]
    except (requests.RequestException, KeyError):
        pass

    lines.append("")
    return "\n".join(lines)


def _budget_summary() -> str:
    return budget_summary(_cfg)


def test() -> str:
    return budget_summary(_cfg)


TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "budget_summary",
            "description": ("Report token/cost usage: LiteLLM all-time spend and configured limits, the "
                            "local memory-DB size (always $0 — fully local), and a Langfuse link if it's "
                            "running. Read-only. Use when the user asks about spend, cost, or usage."),
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

DISPATCH = {"budget_summary": _budget_summary}
