"""Lint / run-tests-and-fix support: run a configured check command and summarize its failures.

The primary contract is language-agnostic and matches how Aider does it: a non-zero exit code means the
check failed, and the captured stdout/stderr tail is fed back to the model. On top of that, optional
per-tool parsers (pytest / unittest / tsc / eslint) extract structured {file, line, message} failures when
the command is recognized; an unknown command falls back to the exit-code + tail. Nothing here calls an
LLM -- this is the objective gate, distinct from the goal-satisfaction critic in the agent loop.

Commands run through sandbox.run_command, the same sandbox + fail-closed + osenv-shell seam the shell
tool uses (so a test-fix run is confined under agent.sandbox='on' and uses pwsh on Windows).
"""
import hashlib
import re

import osenv
import sandbox

TAIL_LINES = 40


class FailureSummary:
    """Outcome of one check. `passed` is driven by the exit code; `failures` is the parsed detail (may be
    empty even on failure when the tool is unrecognized -- `tail` always carries the raw evidence)."""

    def __init__(self, tool, passed, returncode, failures, tail):
        self.tool = tool
        self.passed = passed
        self.returncode = returncode
        self.failures = failures        # list of {file, line, test, message}
        self.tail = tail

    @property
    def signature(self) -> str:
        """Stable identity of this failure, for the loop's forward-progress guard. Built from the sorted
        parsed failures when available, else the raw tail -- so a run that keeps failing identically is
        detectable and the loop can stop instead of burning its budget."""
        if self.failures:
            basis = "\n".join(sorted(f"{f.get('file')}:{f.get('line')}:{f.get('test')}" for f in self.failures))
        else:
            basis = self.tail
        return hashlib.sha1(basis.encode("utf-8", "replace")).hexdigest()[:16]

    def as_feedback(self) -> str:
        """A compact structured message for the model to fix from."""
        if self.passed:
            return f"{self.tool}: passed."
        lines = [f"{self.tool} failed (exit {self.returncode})."]
        for f in self.failures[:20]:
            loc = f.get("file") or ""
            if f.get("line"):
                loc += f":{f['line']}"
            name = f" {f['test']}" if f.get("test") else ""
            msg = f" - {f['message']}" if f.get("message") else ""
            lines.append(f"  {loc}{name}{msg}".rstrip())
        if not self.failures:
            lines.append(_tail(self.tail))
        return "\n".join(lines)


def _tail(text: str) -> str:
    rows = text.strip().split("\n")
    return "\n".join(rows[-TAIL_LINES:])


# --- per-tool parsers: (stdout, stderr) -> list of {file, line, test, message} --------------------

_PYTEST_RE = re.compile(r"^(?P<file>[^\s:]+):(?P<line>\d+):\s+(?:in\s+)?(?P<msg>.+)$")
_PYTEST_SUMMARY_RE = re.compile(r"^FAILED\s+(?P<file>[^\s:]+)::(?P<test>\S+)(?:\s+-\s+(?P<msg>.*))?$")


def parse_pytest(stdout: str, stderr: str) -> list:
    out = []
    for line in (stdout + "\n" + stderr).split("\n"):
        m = _PYTEST_SUMMARY_RE.match(line.strip())
        if m:
            out.append({"file": m.group("file"), "line": None, "test": m.group("test"),
                        "message": (m.group("msg") or "").strip()})
    return out


_UNITTEST_RE = re.compile(r"^(?P<kind>FAIL|ERROR):\s+(?P<test>\S+)")


def parse_unittest(stdout: str, stderr: str) -> list:
    # unittest prints the FAIL/ERROR banner on stderr.
    out = []
    for line in (stdout + "\n" + stderr).split("\n"):
        m = _UNITTEST_RE.match(line.strip())
        if m:
            out.append({"file": None, "line": None, "test": m.group("test"),
                        "message": m.group("kind")})
    return out


_TSC_RE = re.compile(r"^(?P<file>[^\s(]+)\((?P<line>\d+),\d+\):\s+error\s+(?P<code>TS\d+):\s+(?P<msg>.+)$")


def parse_tsc(stdout: str, stderr: str) -> list:
    out = []
    for line in (stdout + "\n" + stderr).split("\n"):
        m = _TSC_RE.match(line.strip())
        if m:
            out.append({"file": m.group("file"), "line": int(m.group("line")), "test": m.group("code"),
                        "message": m.group("msg").strip()})
    return out


_ESLINT_LOC_RE = re.compile(r"^(?P<line>\d+):(?P<col>\d+)\s+(?:error|warning)\s+(?P<msg>.+?)\s{2,}(?P<rule>\S+)$")
_ESLINT_FILE_RE = re.compile(r"^(?P<file>[/.].*\.[a-zA-Z]+)$")


def parse_eslint(stdout: str, stderr: str) -> list:
    out, cur = [], None
    for raw in (stdout + "\n" + stderr).split("\n"):
        line = raw.rstrip()
        fm = _ESLINT_FILE_RE.match(line.strip())
        if fm and not line.strip()[0].isdigit():
            cur = fm.group("file")
            continue
        m = _ESLINT_LOC_RE.match(line.strip())
        if m:
            out.append({"file": cur, "line": int(m.group("line")), "test": m.group("rule"),
                        "message": m.group("msg").strip()})
    return out


PARSERS = {
    "pytest": parse_pytest,
    "unittest": parse_unittest,
    "tsc": parse_tsc,
    "eslint": parse_eslint,
}


def sniff(cmd: str) -> str:
    """Pick a parser name from the command string, or "" for the generic exit-code+tail fallback."""
    c = cmd.lower()
    if "pytest" in c:
        return "pytest"
    if "unittest" in c:
        return "unittest"
    if "tsc" in c or "tsc" in c.split() or "typescript" in c:
        return "tsc"
    if "eslint" in c:
        return "eslint"
    return ""


def summarize(cmd: str, returncode: int, stdout: str, stderr: str, parser: str = "auto") -> FailureSummary:
    """Turn a finished command into a FailureSummary. `parser` 'auto' sniffs from the command; a named
    parser forces one; an unknown/blank parser uses the generic exit-code + tail."""
    passed = returncode == 0
    name = sniff(cmd) if parser == "auto" else (parser if parser in PARSERS else "")
    failures = [] if passed else (PARSERS[name](stdout, stderr) if name in PARSERS else [])
    tail = "" if passed else _tail((stdout or "") + ("\n" + stderr if stderr else ""))
    return FailureSummary(tool=(name or "check"), passed=passed, returncode=returncode,
                          failures=failures, tail=tail)


def run_check(cmd: str, config: dict, parser: str = "auto", timeout: int = 120) -> FailureSummary:
    """Run one check command through the shared sandbox seam and summarize it. The command is a single
    string handed to the OS-native shell (bash/pwsh) via osenv, so a user's `pytest -q` works cross-OS."""
    argv = osenv.default_shell() + [cmd]
    r = sandbox.run_command(argv, config, timeout=timeout)
    return summarize(cmd, r.returncode, r.stdout or "", r.stderr or "", parser=parser)


def run_checks(config: dict, which=("lint", "test"), timeout: int = 120) -> list:
    """Run the configured lint and/or test commands (agent.lintCmd / agent.testCmd), skipping any that are
    unset. Returns a list of FailureSummary in the requested order."""
    agent = config.get("agent", {})
    parser = agent.get("testParser", "auto")
    cmds = {"lint": agent.get("lintCmd", ""), "test": agent.get("testCmd", "")}
    out = []
    for key in which:
        cmd = (cmds.get(key) or "").strip()
        if cmd:
            out.append(run_check(cmd, config, parser=parser, timeout=timeout))
    return out
