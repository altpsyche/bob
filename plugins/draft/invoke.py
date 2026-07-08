#!/usr/bin/env python3
"""bob draft — draft text from a one-line prompt via local LLM.

Usage:
  bob draft "write an email apologising for the late delivery"
  bob draft --type email "apologise for missing the deadline"
  bob draft --type pr "add streaming support to the chat API"
  bob draft --type slack "let the team know the deploy is done"
  bob draft --type doc "explain how the plugin system works"
"""
import sys
import argparse
from pathlib import Path

REPO = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from bob_core import load_config, get_llm_client, check_litellm, get_role, _port


SYSTEM_PROMPTS = {
    "email": (
        "You are Bob, a professional writer. Draft a clear, concise email based on the user's prompt. "
        "Format: Subject line, blank line, greeting, body, closing signature. "
        "Output only the draft — no preamble, no commentary."
    ),
    "pr": (
        "You are Bob, a senior software developer. Write a pull request description based on the prompt. "
        "Use markdown. Include: ## Summary (2-3 bullets), ## What changed, ## How to test. "
        "Output only the PR description — no preamble."
    ),
    "slack": (
        "You are Bob, a direct communicator. Draft a Slack message based on the prompt. "
        "Keep it brief, direct, and human. No markdown headers. "
        "Output only the message — no preamble."
    ),
    "doc": (
        "You are Bob, a technical writer. Write clear documentation based on the prompt. "
        "Use markdown with appropriate structure. Be precise and complete. "
        "Output only the documentation — no preamble."
    ),
    "default": (
        "You are Bob, a skilled writer. Draft the requested text based on the prompt. "
        "Be clear, direct, and appropriate for the context. "
        "Output only the draft — no preamble, no commentary."
    ),
}

# Long-form drafts (PR, docs) route to the thinking role; short-form to chat. The routing
# table itself lives in bob_core.get_role — here we only pick the task.
TYPE_TASK_MAP = {"pr": "think", "doc": "think"}


def draft(prompt: str, type: str = "default", config: dict = None) -> str:
    """Draft text via LLM. Returns drafted string."""
    config = config or {}
    draft_type = type if type in SYSTEM_PROMPTS else "default"
    system_prompt = SYSTEM_PROMPTS[draft_type]

    role = get_role(config, TYPE_TASK_MAP.get(draft_type, "chat"))

    client = get_llm_client(config)

    resp = client.chat.completions.create(
        model=role,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        stream=False,
        timeout=int((config or {}).get("agent", {}).get("requestTimeout", 600)),
    )
    return resp.choices[0].message.content or ""


def main():
    p = argparse.ArgumentParser(description="Draft text from a prompt via local LLM")
    p.add_argument("prompt", nargs="*", help="What to draft")
    p.add_argument("--type", "-t", choices=["email", "pr", "slack", "doc"], default=None,
                   help="Draft type — shapes tone and format")
    p.add_argument("--role", default=None, help="Model role override")
    args = p.parse_args()

    prompt_text = " ".join(args.prompt).strip()
    if not prompt_text and not sys.stdin.isatty():
        prompt_text = sys.stdin.read().strip()

    if not prompt_text:
        print("Usage: bob draft \"<what to write>\" [--type email|pr|slack|doc]", file=sys.stderr)
        print("", file=sys.stderr)
        print("Examples:", file=sys.stderr)
        print('  bob draft "apologise for missing the deadline" --type email', file=sys.stderr)
        print('  bob draft "add streaming support to the API" --type pr', file=sys.stderr)
        print('  bob draft "focus instrumental playlist for coding"', file=sys.stderr)
        sys.exit(1)

    try:
        config = load_config()
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if not check_litellm(config):
        port = _port(config, "litellmPort")
        print(f"Error: LiteLLM proxy not reachable at localhost:{port}", file=sys.stderr)
        print("Run: bob up", file=sys.stderr)
        sys.exit(1)

    if args.role:
        config.setdefault("routing", {})["defaultRole"] = args.role

    print(draft(prompt_text, args.type or "default", config))


if __name__ == "__main__":
    main()
