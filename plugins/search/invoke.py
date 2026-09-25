#!/usr/bin/env python3
"""bob search — search files in a directory and synthesise results via local LLM.

Usage:
  bob search "todo items"
  bob search "error handling" --path src/
  bob search "API endpoints" --ext .py
  bob search "config loading" --raw         show raw grep output, skip LLM
"""
import sys
import argparse
import re
import subprocess
import shutil
from pathlib import Path

REPO = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import osenv
from bob_core import CompletionError, load_config, check_litellm, complete, get_role


MAX_OUT_TOKENS = 1024   # the synthesis output cap (bob_core.complete clamps it to the window)
_EXT_RE = re.compile(r"^\.?[A-Za-z0-9_+-]{1,16}$")


def rg_install_hint() -> str:
    """The OS-appropriate command that installs ripgrep, for the no-search-tool message."""
    name = osenv.os_name()
    if name == "windows":
        return "winget install BurntSushi.ripgrep.MSVC"
    if name == "macos":
        return "brew install ripgrep"
    mgr = osenv.linux_package_manager()
    spec = osenv.resolve_package_cmd("ripgrep", os=name, manager=mgr) if mgr else {}
    if not spec.get("Exe"):
        return "install the 'ripgrep' package with your package manager"
    return " ".join((["sudo"] if spec.get("Sudo") else []) + [spec["Exe"], *spec["Args"]])


def _search_argv(query: str, ext: str | None) -> tuple:
    """The search command for this host plus how its output marks the file name: ripgrep and grep
    print a NUL after the path (so a path containing ':' still splits cleanly), findstr prints ':'.
    The pattern always sits behind -e / /c: and the options end before the path, so a query that
    looks like a flag (e.g. '--pre=sh') is matched as text, never parsed as an option."""
    if shutil.which("rg"):
        cmd = ["rg", "--max-count=5", "--with-filename", "--line-number", "--context=2",
               "--no-heading", "--color=never", "--smart-case", "--null"]
        if ext:
            cmd += ["--glob", f"*.{ext.lstrip('.')}"]
        return cmd + ["-e", query, "--", "."], "\0"
    if osenv.is_windows():
        pattern = f"*.{ext.lstrip('.')}" if ext else "*.*"
        # findstr.exe directly (no `cmd /c`, whose metacharacters would re-parse the query).
        return ["findstr", "/s", "/n", "/i", "/l", f"/c:{query}", pattern], ":"
    cmd = ["grep", "-r", "-n", "-I", "-i", "--null", "--max-count=5", "--context=2"]
    if ext:
        cmd.append(f"--include=*.{ext.lstrip('.')}")
    return cmd + ["-e", query, "--", "."], "\0"


def _filter_denied(out: str, sep: str, search_path: str, deny) -> str:
    """Drop every output line whose file `deny(Path)` rejects, and render the NUL separator as ':'.
    Lines with no file part (the '--' context separators) are kept."""
    base = Path(search_path)
    kept = []
    for line in out.splitlines():
        if sep not in line:
            kept.append(line)
            continue
        name, rest = line.split(sep, 1)
        if deny is not None and deny(base / name):
            continue
        kept.append(f"{name}:{rest}" if sep == "\0" else line)
    return "\n".join(kept).strip()


def run_rg(query: str, search_path: str, ext: str | None, deny=None) -> str:
    """Search `search_path` for `query` with ripgrep (preferred), findstr on Windows, else grep.
    `deny`: optional callable(Path) -> bool; a match in a file it rejects is withheld from the output."""
    if not query:
        return "(search needs a non-empty query)"
    if ext and not _EXT_RE.match(ext):
        return f"(invalid extension filter: {ext!r})"
    if not Path(search_path).is_dir():
        return f"(not a directory: {search_path})"
    cmd, sep = _search_argv(query, ext)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, cwd=search_path)
        out = _filter_denied(r.stdout or "", sep, search_path, deny)
        return out[:6000] if out else "(no matches found)"
    except subprocess.TimeoutExpired:
        return "(search timed out)"
    except FileNotFoundError:
        return f"(search tool not available: install ripgrep with `{rg_install_hint()}`)"


def synthesise(query: str, matches: str, config: dict) -> str:
    """Synthesise ripgrep results via LLM. Returns analysis string."""
    role = get_role(config, "chat")

    prompt = (
        f'Search query: "{query}"\n\n'
        f"Search results:\n```\n{matches}\n```\n\n"
        "Summarise what was found: highlight the most relevant matches, "
        "explain what the code or content is doing, and note any patterns. "
        "Be specific and concise."
    )

    text, _finish = complete(
        config, role,
        [
            {
                "role": "system",
                "content": (
                    "You are Bob, a code search assistant. "
                    "Analyse search results and give a clear, actionable summary. "
                    "Reference specific file names and line numbers from the results."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        MAX_OUT_TOKENS,
    )
    return text


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="bob search",
                                description="Search files and synthesise results via local LLM")
    p.add_argument("query", nargs="+", help="What to search for")
    p.add_argument("--path", default=".", help="Directory to search (default: current dir)")
    p.add_argument("--ext", default=None, help="File extension filter (e.g. .py, .ts, .md)")
    p.add_argument("--raw", action="store_true", help="Show raw matches only, skip LLM synthesis")
    p.add_argument("--role", default=None, help="Model role override")
    args = p.parse_args(argv)

    query = " ".join(args.query)
    search_path = str(Path(args.path).resolve())

    print(f"\033[90mSearching '{query}' in {search_path}...\033[0m", file=sys.stderr)
    matches = run_rg(query, search_path, args.ext)

    if args.raw or matches.startswith("("):
        print(matches)
        return 0

    try:
        config = load_config()
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    if not check_litellm(config):
        # LLM unavailable: fall back to raw output
        print(matches)
        return 0

    if args.role:
        config.setdefault("routing", {})["defaultRole"] = args.role

    try:
        print(synthesise(query, matches, config))
    except CompletionError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
