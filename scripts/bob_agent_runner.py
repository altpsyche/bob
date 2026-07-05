#!/usr/bin/env python
"""Bob scheduled-agent runner (ONE-C Slice 5) — fired every minute by the OS task (cron on POSIX,
schtasks on Windows), registered via `bob agent install`. Reads data/schedules.json, evaluates each
cron expression, and runs due schedules in-process. Port of scripts/bob-agent.ps1 (retired).

Thin by design: all logic lives in scripts/tools/schedule.py (importable + tested); this file only sets
up sys.path, loads config, and delegates — the same core the `bob agent schedule` verbs and the agent
tools call."""
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_SCRIPTS / "tools"))


def main() -> int:
    try:
        from bob_core import load_config
    except Exception as e:  # config unavailable -> nothing we can do
        print(f"bob-agent: failed to load config — {e}", file=sys.stderr)
        return 1
    import schedule
    config = load_config()
    try:
        schedule.run_due_schedules(config)
    except Exception as e:
        print(f"bob-agent: run failed — {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
