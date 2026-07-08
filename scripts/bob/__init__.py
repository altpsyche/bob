"""The `python -m bob` runtime package — owns command dispatch and the command registry.

This is a *real importable package* exposing the agent runtime as an API, so NE's interactive
shell (later) can consume `run_agent_events` in-process rather than via subprocess. It also owns
the command dispatch (`bob.cli`) and the command registry (`bob.registry`).

`python -m bob` requires `scripts/` on sys.path (the `bob_*` modules are siblings of this package).
The shim (the POSIX `bob` / the Windows bob.cmd) sets PYTHONPATH=scripts; in-process callers
(tests via _common) put scripts/ on sys.path. We add it here too, defensively.
"""
import os
import sys

_SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../scripts
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)


def __getattr__(name):
    # Lazy re-export of the agent-runtime API so `python -m bob <help>` stays light and the
    # heavy imports (openai, etc.) only load when the runtime is actually used.
    if name == "run_agent_events":
        from bob_loop import run_agent_events
        return run_agent_events
    raise AttributeError(f"module 'bob' has no attribute {name!r}")


__all__ = ["run_agent_events"]
