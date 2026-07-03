"""Bob tool: shell_run — executes a command in the OS-native shell.

NE0: approval is handled by the agent loop's event-driven approve callback (this tool sets
REQUIRES_APPROVAL=True), NOT a blocking stdin prompt — so it works under the TUI/server, not only
an interactive console. Timeout: 30 seconds. Process is killed on timeout. NB3: the shell is
OS-native (pwsh on Windows, bash/sh elsewhere) via osenv.default_shell().
"""
import subprocess
import sys

import osenv
import sandbox

_cfg: dict = {}

# NE0 — the loop asks the approve callback before dispatching shell_run (see ToolRegistry
# .approval_required_tools). O5 — when agent.sandbox='on', the command runs under an OS sandbox
# (scripts/sandbox.py); O6 adds richer per-command policy at the dispatch choke point.
REQUIRES_APPROVAL = True


def configure(config: dict) -> None:
    global _cfg
    _cfg = config


def _execute(argv: list):
    """Run the command vector. O5: sandboxed when agent.sandbox='on' (fail closed via
    SandboxUnavailable if no backend exists — never a silent unsandboxed run under 'on'); otherwise
    in-process, byte-identical to pre-O5. Returns a CompletedProcess; propagates TimeoutExpired."""
    if sandbox.sandbox_mode(_cfg) == sandbox.SANDBOX_ON:
        return sandbox.run_sandboxed(argv, timeout=30, limits=sandbox.sandbox_limits(_cfg))
    return subprocess.run(argv, capture_output=True, text=True, timeout=30)


def _shell_run(command: str) -> str:
    shell = osenv.default_shell()
    argv = shell + [command]
    try:
        r = _execute(argv)
        output = ((r.stdout or "") + (r.stderr or "")).strip()
        if not output:
            return f"(exit code {r.returncode}, no output)"
        return output[:4000]
    except sandbox.SandboxUnavailable as e:
        return (f"shell_run refused: agent.sandbox='on' but no sandbox backend is available ({e}). "
                f"Install a backend or set agent.sandbox='off' to run unsandboxed.")
    except subprocess.TimeoutExpired as exc:
        if getattr(exc, "process", None):
            exc.process.kill()
        return "Command timed out after 30s and was killed."
    except FileNotFoundError:
        return f"shell '{shell[0]}' not found."
    except Exception as e:
        return f"shell_run error: {e}"


def test() -> str:
    print("[shell test] Skipped — shell_run requires loop-level approval before it runs.", file=sys.stderr)
    return "shell_run test: OK (approval is requested by the agent loop, not this tool)"


TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "shell_run",
            "description": (
                "Run a PowerShell command and return its output. "
                "Always requires explicit user confirmation before executing. "
                "Use specific tools (git, file, web) when possible."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "PowerShell command to execute",
                    }
                },
                "required": ["command"],
            },
        },
    }
]

DISPATCH = {"shell_run": _shell_run}
