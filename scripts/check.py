#!/usr/bin/env python3
"""ONE-E — the pre-commit / CI gate, in Python (port of the retired check.ps1). Runs, in order:

  1. py_compile over scripts/, plugins/, tests/ (excluding external/)
  2. versions.lock in sync with its sources    (python -m bob.versions --check)
  3. executable bits on the shell entrypoints   (git-tracked mode 100755)
  4. the stdlib-unittest suite in tests/         (skip with --no-tests)

Exits non-zero on the first category that fails, so the git pre-commit hook (or CI) blocks. Stdlib-only,
so any interpreter runs it — the pwsh AST-parse step is gone (there is no PowerShell left to parse).

  python scripts/check.py            # full
  python scripts/check.py --no-tests # skip the unittest suite (static checks only)
"""
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
PY = os.environ.get("BOB_PYTHON") or sys.executable


def _env() -> dict:
    e = dict(os.environ)
    e["PYTHONPATH"] = str(SCRIPTS) + (os.pathsep + e["PYTHONPATH"] if e.get("PYTHONPATH") else "")
    e["PYTHONIOENCODING"] = "utf-8"
    return e


def _run(argv: list) -> int:
    return subprocess.run(argv, env=_env()).returncode


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    no_tests = "--no-tests" in argv or "-NoTests" in argv
    failed = False

    # 1. py_compile ---------------------------------------------------------
    print("[check] py_compile...")
    py_files = [str(p) for base in ("scripts", "plugins", "tests")
                for p in (REPO / base).rglob("*.py")
                if "external" not in p.parts]
    if _run([PY, "-m", "py_compile", *py_files]) != 0:
        print("[check] py_compile FAILED"); failed = True

    # 2. versions.lock in sync with its sources -----------------------------
    print("[check] versions.lock in sync...")
    if _run([PY, "-m", "bob.versions", "--check"]) != 0:
        print("[check] versions.lock STALE — run: bob lock"); failed = True

    # 3. executable bits on the shell entrypoints ---------------------------
    # git tracks the +x bit; a file committed 100644 makes './setup.sh' die with 'permission denied' on
    # every fresh clone. Assert the entrypoints + hook stay executable (reads the tracked mode, so it
    # works on Windows too, where the filesystem bit is meaningless).
    print("[check] entrypoint exec bits...")
    for f in ("bob", "install_prereqs.sh", "setup.sh", "scripts/hooks/pre-commit"):
        entry = subprocess.run(["git", "-C", str(REPO), "ls-files", "--stage", "--", f],
                               capture_output=True, text=True).stdout.strip()
        if not entry:
            print(f"[check] MISSING from index: {f}"); failed = True; continue
        mode = entry.split()[0]
        if mode != "100755":
            print(f"[check] NOT EXECUTABLE: {f} (git mode {mode}) — run: git update-index --chmod=+x {f}")
            failed = True

    # 4. unittest suite -----------------------------------------------------
    if not no_tests:
        print("[check] unittest suite...")
        if _run([PY, "-m", "unittest", "discover", "-s", str(REPO / "tests"), "-p", "test_*.py"]) != 0:
            print("[check] tests FAILED"); failed = True

    if failed:
        print("[check] FAILED")
        return 1
    print("[check] all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
