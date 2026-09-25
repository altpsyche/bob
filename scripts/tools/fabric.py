"""Bob tool: fabric_run, runs a fabric pattern on text input.

The binary is the repo-staged one (bin/fabric, bin/fabric.exe on Windows) when present, else `fabric`
on PATH; the resolved absolute path is what runs. A pattern is a bare name, so a value shaped like a
flag or a path is refused before it reaches the command line. Every call names Bob's LiteLLM vendor and
model explicitly (the ones `bob fabric-setup` configures), so a user's own DEFAULT_VENDOR in fabric's .env
never reroutes the agent's calls."""
import re
import shutil
import subprocess
import sys
from pathlib import Path

for _d in (str(Path(__file__).parent.parent), str(Path(__file__).parent)):
    if _d not in sys.path:
        sys.path.insert(0, _d)

import osenv  # noqa: E402
from build import _FABRIC_MODEL, _FABRIC_VENDOR  # noqa: E402

_fabric_bin: str = ""
_PATTERN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def resolve_fabric() -> str:
    """Absolute path of the fabric binary to run: repo bin/ first, then PATH. Empty if neither."""
    staged = osenv.bin_exe("fabric")
    if staged.exists():
        return str(staged)
    return shutil.which("fabric") or ""


def configure(config: dict) -> None:
    global _fabric_bin
    _fabric_bin = resolve_fabric()


def _fabric_run(pattern: str, input: str) -> str:
    if not _fabric_bin:
        return (
            "fabric not found (repo bin/ or PATH).\n"
            "Run: bob fabric-setup   (installs and configures fabric)"
        )
    if not isinstance(pattern, str) or not _PATTERN_RE.match(pattern) or ".." in pattern:
        return f"fabric_run: invalid pattern name {pattern!r} (letters, digits, '_', '-', '.' only)"
    try:
        r = subprocess.run(
            [_fabric_bin, "--pattern", pattern, "--vendor", _FABRIC_VENDOR, "--model", _FABRIC_MODEL],
            input=input,
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = r.stdout.strip()
        err = r.stderr.strip()
        if not output and err:
            return f"fabric error: {err[:1000]}"
        return output[:4000] if output else "(no output)"
    except subprocess.TimeoutExpired as exc:
        if exc.process:
            exc.process.kill()
        return "fabric timed out after 120s."
    except Exception as e:
        return f"fabric_run error: {e}"


def test() -> str:
    if not _fabric_bin:
        return "fabric not available — skipping test"
    # Use a simple built-in pattern that always works
    result = _fabric_run("summarize", "The quick brown fox jumps over the lazy dog.")
    return result or "(fabric returned empty output)"


TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "fabric_run",
            "description": (
                "Run a fabric AI pattern on text input. "
                "Patterns include: summarize, extract_wisdom, improve_writing, "
                "create_outline, analyze_paper, write_essay, and many more. "
                "Use `bob fabric -l` to see all available patterns."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Fabric pattern name (e.g. 'summarize', 'extract_wisdom')",
                    },
                    "input": {
                        "type": "string",
                        "description": "Text to process with the pattern",
                    },
                },
                "required": ["pattern", "input"],
            },
        },
    }
]

DISPATCH = {"fabric_run": _fabric_run}
