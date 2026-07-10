"""Agent-capability eval fixtures.

Each task drives the REAL agent loop (run_agent_events) with a records-based (scripted) client and a
FakeRegistry — NO live model — so it runs identically on the CPU CI tier and locally. A task declares
the model's turns + the expected observable behavior; run_eval.py scores it deterministically from the
event stream (tools dispatched, final answer, permission/approval events). The tasks track the capability
wins quantitatively: parallel dispatch, permission deny + approval, delegation, plus the
multi-step tool-use baseline. Add a task = add a dict; the scorer is generic.
"""
import json


def _tc(name, **args):
    """One hermes <tool_call> block (the format the loop parses)."""
    return f'<tool_call>{json.dumps({"name": name, "arguments": args})}</tool_call>'


EVAL_TASKS = [
    {
        "name": "final_answer",                       # baseline: no tools, direct answer
        "goal": "What is 6 times 7?",
        "turns": ["The answer is 42."],
        "expect_final": True,
        "expect_final_contains": ["42"],
    },
    {
        "name": "tool_use_single",                    # one tool call, then an answer using it
        "goal": "Echo 'hi' and report it.",
        "turns": [_tc("echo", x="hi"), "Done: the tool returned hi."],
        "results": {"echo": "hi"},
        "expect_final": True,
        "expect_tools": ["echo"],
        "expect_final_contains": ["Done"],
    },
    {
        "name": "multi_step_chain",                   # two sequential tool calls, then synthesize
        "goal": "Read the file then search, then summarize.",
        "turns": [_tc("file_read", path="a.txt"), _tc("web_search", q="bob"),
                  "Summary combining both results."],
        "results": {"file_read": "file contents", "web_search": "search hits"},
        "expect_final": True,
        "expect_tools": ["file_read", "web_search"],
    },
    {
        "name": "parallel_tools",                     # two side-effect-free calls in one step
        "goal": "Look up two things at once.",
        "turns": [_tc("read_a", k=1) + _tc("read_b", k=2), "Both fetched."],
        "results": {"read_a": "A", "read_b": "B"},
        "maxParallelTools": 4,
        "expect_final": True,
        "expect_tools": ["read_a", "read_b"],
    },
    {
        "name": "permission_deny",                    # a denied tool never runs; agent still answers
        "goal": "Use the secret tool.",
        "turns": [_tc("secret_tool", q="creds"), "I could not access that; it was denied by policy."],
        "results": {"secret_tool": "SENSITIVE-VALUE"},
        "permissions": {"tools": {"secret_tool": "deny"}},
        "expect_final": True,
        "forbid_tools": ["secret_tool"],
        "forbid_final_contains": ["SENSITIVE-VALUE"],
        "expect_final_contains": ["could not"],
    },
    {
        "name": "approval_ask",                       # an 'ask' tool prompts, then runs on approval
        "goal": "Run the risky tool.",
        "turns": [_tc("risky", go=True), "Completed after approval."],
        "results": {"risky": "OK"},
        "permissions": {"tools": {"risky": "ask"}},
        "approve": lambda action: True,
        "expect_final": True,
        "expect_tools": ["risky"],
        "expect_events": ["approval_required"],
    },
    {
        "name": "delegation",                         # delegate via spawn_agent, use its summary
        "goal": "Delegate the research subtask.",
        "turns": [_tc("spawn_agent", task="research X"),
                  "Synthesized from the sub-agent's findings."],
        "results": {"spawn_agent":
                    json.dumps({"result": "sub findings", "steps": 2, "tools_used": ["web_search"]})},
        "expect_final": True,
        "expect_tools": ["spawn_agent"],
        "expect_final_contains": ["Synthesized"],
    },
    {
        "name": "resume_integrity",                   # prior turns pre-seeded via history reach the goal
        "goal": "Given the earlier findings, state the conclusion.",
        "history": [
            {"role": "user", "content": "Research the topic."},
            {"role": "assistant", "content": "I gathered the key facts: X and Y."},
        ],
        "turns": ["Conclusion: X and Y together imply Z."],
        "expect_final": True,
        "expect_final_contains": ["Conclusion"],
    },
    {
        "name": "step_budget_exhaustion",             # over-budget run still ends with a final event;
        "goal": "Keep calling tools past the budget.",  # the never-reached tool is not dispatched
        "maxSteps": 2,
        "turns": [_tc("step_one", k=1), _tc("step_two", k=2), _tc("never_reached", k=3), "done"],
        "results": {"step_one": "1", "step_two": "2", "never_reached": "3"},
        "expect_events": ["final"],
        "forbid_tools": ["never_reached"],
    },
    {
        "name": "computer_use_denied_when_off",       # a denied computer action never runs; agent answers
        "goal": "Click the button on screen.",
        "turns": [_tc("computer_click", coordinate=[10, 20]),
                  "I could not do that; computer-use is not permitted here."],
        "results": {"computer_click": "clicked"},
        "permissions": {"tools": {"computer_click": "deny"}},
        "expect_final": True,
        "forbid_tools": ["computer_click"],
        "expect_final_contains": ["could not"],
    },
    {
        "name": "computer_use_ask_enforced",          # an approval-gated action prompts; denial blocks it
        "goal": "Click the button on screen.",
        "turns": [_tc("computer_click", coordinate=[10, 20]),
                  "Understood, I will not click without approval."],
        "results": {"computer_click": "clicked"},
        "approval": ["computer_click"],
        "approve": lambda action: False,             # operator denies at the prompt
        "expect_final": True,
        "expect_events": ["approval_required"],
        "forbid_tools": ["computer_click"],
    },
    {
        "name": "computer_use_approved_runs",         # approved at the prompt -> the action runs
        "goal": "Click the button on screen.",
        "turns": [_tc("computer_click", coordinate=[10, 20]), "Clicked as approved."],
        "results": {"computer_click": "clicked"},
        "approval": ["computer_click"],
        "approve": lambda action: True,
        "expect_final": True,
        "expect_tools": ["computer_click"],
    },
]
