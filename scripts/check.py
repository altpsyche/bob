#!/usr/bin/env python3
"""The pre-commit / CI gate, in Python. Runs every category and reports each one:

  1. py_compile over scripts/, plugins/, tests/ (excluding external/)
  2. versions.lock in sync with its sources    (python -m bob.versions --check)
  3. executable bits on the shell entrypoints   (git-tracked mode 100755)
  4. release metadata: CHANGELOG.md carries `## [Unreleased]` and a `## [<VERSION>]` section as its newest
     release, and on a release-tag build the tag is exactly v<VERSION>
  5. n8n workflows are portable: no `host.docker.internal`, no literal `sk-local` key
  6. the stdlib-unittest suite in tests/, plus a skip guard: any skip outside _ALLOWED_SKIPS fails
     (a missing optional dep silently skipping a whole area is how a green gate lies)  (skip with --no-tests)

Exits non-zero if any category failed, so the git pre-commit hook (or CI) blocks. Stdlib-only, so any
interpreter runs it; the suite runs under BOB_PYTHON (else this interpreter), which must carry the
runtime deps for the skip guard to pass.

  python scripts/check.py            # full
  python scripts/check.py --no-tests # skip the unittest suite (static checks only)
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
PY = os.environ.get("BOB_PYTHON") or sys.executable

# Skips the suite may take, as (test-id prefix, platforms where the skip is expected; None = anywhere).
# Everything else that skips is a dependency the gate interpreter lacks, and fails the check.
_ALLOWED_SKIPS = (
    ("test_release_manifest.TestPublishedManifestLive", None),   # opt-in network test (its own CI job)
    ("test_agent_parallel.", None),                               # wall-clock test needs >= 4 host cores
    ("test_vision.", None),                                       # no-PIL passthrough skips when Pillow is in
    ("test_sandbox.TestLinuxConfinement", {"win32", "darwin"}),   # bubblewrap is Linux-only
    ("test_osenv.TestProcessLifecyclePosix", {"win32"}),
    ("test_pack_engine.", {"win32"}),                             # symlinks need privilege on Windows
    ("test_kernel.", {"win32"}),
    ("test_build.", {"win32"}),
    ("test_search_plugin.", {"win32"}),                           # needs a POSIX shell script
)

_N8N_FORBIDDEN = ("host.docker.internal", "sk-local")


def _env() -> dict:
    e = dict(os.environ)
    e["PYTHONPATH"] = str(SCRIPTS) + (os.pathsep + e["PYTHONPATH"] if e.get("PYTHONPATH") else "")
    e["PYTHONIOENCODING"] = "utf-8"
    return e


def _run(argv: list) -> int:
    return subprocess.run(argv, env=_env()).returncode


def release_metadata_problems(repo: Path = REPO, env: dict = None) -> list:
    """Why VERSION, CHANGELOG.md and (on a tag build) the tag disagree; [] when consistent. The shape is
    what bob.versions.cut_changelog writes: `## [Unreleased]` on top, then `## [<version>] (<date>)`."""
    env = os.environ if env is None else env
    problems = []
    version = (repo / "VERSION").read_text(encoding="utf-8").strip()
    heads = [ln.strip() for ln in (repo / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
             if ln.startswith("## [")]
    if "## [Unreleased]" not in heads:
        problems.append("CHANGELOG.md has no '## [Unreleased]' heading (bob release cuts from it)")
    released = [re.match(r"## \[([^\]]+)\]", h).group(1) for h in heads if h != "## [Unreleased]"]
    if version not in released:
        problems.append(f"CHANGELOG.md has no '## [{version}]' section for VERSION {version}")
    elif released[0] != version:
        problems.append(f"CHANGELOG.md's newest release is [{released[0]}] but VERSION is {version}")
    ref = env.get("GITHUB_REF", "")
    if ref.startswith("refs/tags/"):
        tag = ref[len("refs/tags/"):]
        if tag != f"v{version}":
            problems.append(f"release tag {tag} does not match VERSION (expected v{version})")
    return problems


def n8n_problems(repo: Path = REPO) -> list:
    """Workflow JSON that only works on one machine: a Docker-Desktop-only host or the literal default key."""
    problems = []
    for f in sorted((repo / "tools" / "n8n-workflows").glob("*.json")):
        text = f.read_text(encoding="utf-8")
        try:
            json.loads(text)
        except ValueError as e:
            problems.append(f"{f.relative_to(repo)}: invalid JSON ({e})")
            continue
        for bad in _N8N_FORBIDDEN:
            n = text.count(bad)
            if n:
                problems.append(f"{f.relative_to(repo)}: {n} x '{bad}'")
    return problems


def unexpected_skips(skipped: list, platform: str = sys.platform) -> list:
    """The (test id, reason) skips not covered by _ALLOWED_SKIPS on `platform`."""
    out = []
    for test_id, reason in skipped:
        if not any(test_id.startswith(prefix) and (plats is None or platform in plats)
                   for prefix, plats in _ALLOWED_SKIPS):
            out.append((test_id, reason))
    return out


def _suite_worker(out_path: str) -> int:
    """Run the suite in THIS interpreter and record its skips to out_path (JSON), for the parent's guard."""
    import unittest
    tests_dir = str(REPO / "tests")
    suite = unittest.defaultTestLoader.discover(tests_dir, pattern="test_*.py", top_level_dir=tests_dir)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    Path(out_path).write_text(json.dumps({
        "ok": result.wasSuccessful(),
        "skipped": [[t.id(), str(reason)] for t, reason in result.skipped],
    }), encoding="utf-8")
    return 0 if result.wasSuccessful() else 1


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--suite-worker"]:
        return _suite_worker(argv[1])
    no_tests = "--no-tests" in argv or "-NoTests" in argv
    failed = False
    try:
        sys.stdout.reconfigure(line_buffering=True)   # keep [check] lines ordered with the child output
    except (AttributeError, ValueError):
        pass

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

    # 4. release metadata ---------------------------------------------------
    print("[check] VERSION / CHANGELOG / tag agree...")
    for p in release_metadata_problems():
        print(f"[check] RELEASE: {p}"); failed = True

    # 5. n8n workflows portable ---------------------------------------------
    print("[check] n8n workflows portable...")
    for p in n8n_problems():
        print(f"[check] N8N: {p}"); failed = True

    # 6. unittest suite + skip guard ----------------------------------------
    if not no_tests:
        print("[check] unittest suite...")
        fd, out = tempfile.mkstemp(prefix="bob-check-", suffix=".json")
        os.close(fd)
        try:
            rc = _run([PY, str(Path(__file__).resolve()), "--suite-worker", out])
            try:
                report = json.loads(Path(out).read_text(encoding="utf-8") or "{}")
            except ValueError:
                report = {}
        finally:
            Path(out).unlink(missing_ok=True)
        if rc != 0:
            print("[check] tests FAILED"); failed = True
        if "skipped" not in report:
            print("[check] skip guard: the suite produced no report"); failed = True
        else:
            extra = unexpected_skips([tuple(s) for s in report["skipped"]])
            if extra:
                print(f"[check] skip guard: {len(extra)} unexpected skip(s) (a dep missing from {PY}?):")
                for test_id, reason in extra:
                    print(f"[check]   SKIP {test_id}: {reason}")
                failed = True

    if failed:
        print("[check] FAILED")
        return 1
    print("[check] all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
