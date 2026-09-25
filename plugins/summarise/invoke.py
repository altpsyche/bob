#!/usr/bin/env python3
"""bob summarise — summarise a file or piped text via local LLM.

Usage:
  bob summarise <file>              summarise a file
  bob summarise                     summarise stdin (pipe text to it)
  cat notes.txt | bob summarise     same
  bob summarise report.md --length long
"""
import sys
import argparse
from pathlib import Path

REPO = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from bob_core import CompletionError, load_config, check_litellm, complete, get_role, _port

LENGTH_MAP = {
    "short": "2-3 sentences",
    "medium": "a concise paragraph (5-8 sentences)",
    "long": "a structured summary with key points, decisions, and action items",
}

MAX_CHARS = 12000
MAX_OUT_TOKENS = 1024   # the summary's output cap (bob_core.complete clamps it to the model's window)


def summarise(content: str, length: str = "medium", config: dict = None) -> str:
    """Summarise content via LLM. Returns summary string."""
    config = config or {}
    if not content.strip():
        return "(empty input)"
    if len(content) > MAX_CHARS:
        content = content[:MAX_CHARS] + f"\n\n[...truncated at {MAX_CHARS} chars]"

    role = get_role(config, "chat")
    text, _finish = complete(
        config, role,
        [
            {
                "role": "system",
                "content": (
                    "You are Bob, a concise summariser. "
                    "Output only the summary — no preamble like 'Here is a summary:', no meta-commentary."
                ),
            },
            {
                "role": "user",
                "content": f"Summarise the following in {LENGTH_MAP[length]}:\n\n{content}",
            },
        ],
        MAX_OUT_TOKENS,
    )
    return text


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="bob summarise", description="Summarise a file or stdin via local LLM")
    p.add_argument("file", nargs="?", help="File to summarise (omit to read stdin)")
    p.add_argument("--role", default=None, help="Model role override (default: chat)")
    p.add_argument("--length", choices=["short", "medium", "long"], default="medium",
                   help="Summary length: short=2-3 sentences, medium=paragraph, long=structured")
    args = p.parse_args(argv)

    if args.file:
        fp = Path(args.file)
        if not fp.exists():
            print(f"Error: file not found: {args.file}", file=sys.stderr)
            return 1
        content = fp.read_text(encoding="utf-8", errors="replace")
    elif not sys.stdin.isatty():
        content = sys.stdin.read()
    else:
        print("Usage: bob summarise <file>  OR  cat file | bob summarise", file=sys.stderr)
        return 1

    try:
        config = load_config()
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    if not check_litellm(config):
        port = _port(config, "litellmPort")
        print(f"Error: LiteLLM proxy not reachable at localhost:{port}", file=sys.stderr)
        print("Run: bob up", file=sys.stderr)
        return 1

    if args.role:
        config.setdefault("routing", {})["defaultRole"] = args.role

    try:
        print(summarise(content, args.length, config))
    except CompletionError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
