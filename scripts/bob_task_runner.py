#!/usr/bin/env python
"""Bob detached-task worker — the process a `bob task start`/`resume` launches via osenv.start_detached
to run one durable agent run in the background, surviving the client's disconnect.

Thin by design: it drives bob_loop.run_agent (which persists run state and writes the terminal status
itself when checkpointing is on) under a cancel token wired to SIGTERM, so `bob task cancel` (which sends
SIGTERM to the process group) stops the run cleanly at the next step boundary. The run-state store is the
single source of truth for status/result; this worker adds no parallel bookkeeping."""
import argparse
import signal
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_SCRIPTS / "tools"))


def run_task(config: dict, run_id: str, owner: str, goal: str = None, resume: bool = False,
             cancel=None, allow_computer: bool = False) -> int:
    """Run one durable agent run to completion. `resume` continues a checkpointed run (goal is loaded
    from its row); otherwise `goal` starts a fresh run under `run_id`. Returns a process exit code."""
    import bob_loop
    agent = config.setdefault("agent", {})
    agent["checkpoint"] = True     # a detached task always persists its state
    agent["unattended"] = True     # no interactive operator: gates the most dangerous tools off
    if allow_computer:
        agent.setdefault("computerUse", {})["allowUnattended"] = True
    try:
        bob_loop.run_agent(goal or "", config, agency="silent", run_id=run_id, owner=owner,
                           cancel=cancel, resume=(run_id if resume else None))
    except Exception as e:
        print(f"bob-task: run {run_id} failed — {e}", file=sys.stderr)
        return 1
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="bob-task-runner")
    p.add_argument("--run-id", required=True)
    p.add_argument("--owner", default=None)
    p.add_argument("--goal", default=None)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--allow-computer", action="store_true",
                   help="permit computer-use in this unattended task (off by default)")
    args = p.parse_args(argv)

    try:
        from bob_core import load_config
    except Exception as e:
        print(f"bob-task: failed to load config — {e}", file=sys.stderr)
        return 1
    config = load_config()

    # Bring inference up if needed (a detached run has no interactive stack to lean on).
    try:
        import stack
        stack.ensure_inference(config)
    except Exception as e:
        print(f"bob-task: inference not reachable — {e}", file=sys.stderr)

    import bob_loop
    cancel = bob_loop.CancelToken()
    signal.signal(signal.SIGTERM, lambda *_: cancel.cancel())   # `task cancel` stops us at a step boundary

    return run_task(config, args.run_id, args.owner, goal=args.goal, resume=args.resume, cancel=cancel,
                    allow_computer=args.allow_computer)


if __name__ == "__main__":
    sys.exit(main())
