#!/usr/bin/env python3
"""Agent-capability eval harness (records-based, CPU-safe).

Runs each fixture task (tests/eval/tasks.py) through the REAL agent loop with a scripted client + a
FakeRegistry, scores it deterministically from the event stream, and prints a per-task + total
capability score. No live model → runs on the CPU CI tier and locally, identically.

Non-gating by default (exit 0 regardless of score) — CI reports the number until baselines stabilize,
then `--gate <0..1>` can enforce a threshold. `--json` emits a machine-readable summary for a CI artifact.

    python tests/eval/run_eval.py            # print report, exit 0
    python tests/eval/run_eval.py --json     # + JSON line
    python tests/eval/run_eval.py --gate 0.9 # exit 1 if score < 0.9
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_TESTS = os.path.dirname(_HERE)
_REPO = os.path.dirname(_TESTS)
for _p in (_HERE, _TESTS, os.path.join(_REPO, "scripts"), os.path.join(_REPO, "scripts", "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _common          # noqa: E402 — sets scripts/ on sys.path + provides the fakes
from tasks import EVAL_TASKS  # noqa: E402

_BASE_AGENT = {"toolFormat": "hermes", "maxSteps": 6, "maxContextTokens": 0, "maxToolResultTokens": 1000}


def score_task(task: dict, events: list, reg, final) -> tuple:
    """Deterministic rubric over the event stream. Returns (earned, total, checks)."""
    result = final.get("result") if final else None
    checks = []
    if task.get("expect_final"):
        checks.append(("final_produced", result is not None))
    for t in task.get("expect_tools", []):
        checks.append((f"called:{t}", t in reg.dispatched))
    for t in task.get("forbid_tools", []):
        checks.append((f"not_called:{t}", t not in reg.dispatched))
    for sub in task.get("expect_final_contains", []):
        checks.append((f"final~'{sub}'", bool(result) and sub in result))
    for sub in task.get("forbid_final_contains", []):
        checks.append((f"final!~'{sub}'", not (result and sub in result)))
    for evt in task.get("expect_events", []):
        checks.append((f"event:{evt}", any(e.get("type") == evt for e in events)))
    earned = sum(1 for _, ok in checks if ok)
    return earned, len(checks), checks


def run_task(task: dict) -> tuple:
    """Drive one task through the real loop with a scripted client. Returns (earned, total, checks)."""
    import bob_core
    import bob_loop

    agent = dict(_BASE_AGENT)
    agent["permissions"] = task.get("permissions", {})
    agent["maxParallelTools"] = task.get("maxParallelTools", 1)
    agent["maxSteps"] = task.get("maxSteps", _BASE_AGENT["maxSteps"])   # per-task step budget
    cfg = _common.fake_config(agent=agent)
    reg = _common.FakeRegistry(task.get("results", {}),
                               mutating_tools=task.get("mutating"),
                               approval_required_tools=task.get("approval"))

    orig_check, orig_client = bob_core.check_litellm, bob_core.get_llm_client
    bob_core.check_litellm = lambda config=None: True
    bob_core.get_llm_client = lambda config=None: _common.scripted_client(task["turns"])
    try:
        events = list(bob_loop.run_agent_events(
            task["goal"], cfg, agency=task.get("agency", "silent"), registry=reg,
            approve=task.get("approve"), owner=task.get("owner", "local"),
            history=task.get("history")))   # resume-integrity fixtures pre-seed prior turns
    finally:
        bob_core.check_litellm, bob_core.get_llm_client = orig_check, orig_client

    final = next((e for e in reversed(events) if e.get("type") == "final"), None)
    return score_task(task, events, reg, final)


def run_all(tasks: list = None) -> tuple:
    """Run every task. Returns (earned_total, max_total, results) where results is a list of
    (name, earned, max, checks)."""
    tasks = tasks if tasks is not None else EVAL_TASKS
    earned_total = max_total = 0
    results = []
    for task in tasks:
        earned, total, checks = run_task(task)
        results.append((task["name"], earned, total, checks))
        earned_total += earned
        max_total += total
    return earned_total, max_total, results


def _report(earned: int, total: int, results: list) -> None:
    for name, e, m, checks in results:
        status = "PASS" if e == m else "FAIL"
        print(f"  [{status}] {name:<22} {e}/{m}")
        for label, ok in checks:
            if not ok:
                print(f"          - MISS {label}")
    pct = (earned / total * 100.0) if total else 0.0
    print(f"\nCapability score: {earned}/{total} ({pct:.1f}%)")


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="run_eval", description="Bob agent-capability eval.")
    p.add_argument("--json", action="store_true", help="emit a JSON summary line")
    p.add_argument("--gate", type=float, default=None, help="exit 1 if score < this fraction (0..1)")
    args = p.parse_args(argv)

    earned, total, results = run_all()
    _report(earned, total, results)
    score = (earned / total) if total else 0.0

    if args.json:
        print(json.dumps({"earned": earned, "total": total, "score": round(score, 4),
                          "tasks": [{"name": n, "earned": e, "max": m} for n, e, m, _ in results]}))
    if args.gate is not None and score < args.gate:
        print(f"[eval] score {score:.3f} below gate {args.gate:.3f}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
