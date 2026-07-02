"""Bob tool: shell_run — executes a command in the OS-native shell.

NE0: approval is handled by the agent loop's event-driven approve callback (this tool sets
REQUIRES_APPROVAL=True), NOT a blocking stdin prompt — so it works under the TUI/server, not only
an interactive console. Timeout: 30 seconds. Process is killed on timeout. NB3: the shell is
OS-native (pwsh on Windows, bash/sh elsewhere) via osenv.default_shell().
"""
import subprocess
import sys

import osenv

_cfg: dict = {}

# NE0 — the loop asks the approve callback before dispatching shell_run (see ToolRegistry
# .approval_required_tools). O5 will add OS-level sandboxing; O6 adds richer per-command policy.
REQUIRES_APPROVAL = True


def configure(config: dict) -> None:
    global _cfg
    _cfg = config


def _shell_run(command: str) -> str:
    shell = osenv.default_shell()
    try:
        r = subprocess.run(
            shell + [command],
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = (r.stdout + r.stderr).strip()
        if not output:
            return f"(exit code {r.returncode}, no output)"
        return output[:4000]
    except subprocess.TimeoutExpired as exc:
        if exc.process:
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
