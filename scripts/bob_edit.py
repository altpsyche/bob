"""Structured edit engine: apply precise partial edits to files instead of rewriting them whole.

One importable core used by the file_edit tool and the CLI. Matching is strict by design -- exact match
first, then a single uniform-indentation-tolerant retry, then reject. There is deliberately NO fuzzy
edit-distance application: matching code by similarity lands edits at the wrong location. When a search
block matches more than one place, the edit is rejected as ambiguous rather than guessed. SequenceMatcher
is used only to show the closest existing text in a rejection, never to decide where to apply.

Multi-file edits are all-or-nothing: every hunk across every file is validated first; if any hunk is
rejected, nothing is written and the caller is told which hunk failed.

The rejection message begins with "EDIT REJECTED" -- deliberately not "Tool error"/"Unknown tool"/
"Bad arguments" (which would trigger the loop's same-call self-repair retry) and not malformed JSON (which
would hit the parse-error path). It reads as a plain instruction the model corrects from with a new call.
"""
import difflib
from pathlib import Path

import bob_fsguard

REJECT_PREFIX = "EDIT REJECTED"


class EditResult:
    """Outcome of computing/applying an edit batch. `ok` false means nothing was written."""

    def __init__(self):
        self.ok = True
        self.files = {}          # path(str) -> new content
        self.originals = {}      # path(str) -> old content (for diff)
        self.rejections = []     # list of {path, hunk_index, reason, closest}
        self.message = ""
        self.diff = ""


def normalize_edits(args: dict) -> list:
    """Accept either a single-file form {path, edits|search|replace|content} or a multi-file form
    {files: [...]} and return a list of per-file dicts: {path, edits:[{search,replace}], content?}.
    `content` (whole-file) is carried through for the whole format; `edits` for search/replace."""
    if not isinstance(args, dict):
        raise ValueError("edit arguments must be an object")
    if "files" in args and args["files"] is not None:
        files = args["files"]
        if not isinstance(files, list):
            raise ValueError("'files' must be a list")
        return [_normalize_one(f) for f in files]
    return [_normalize_one(args)]


def _normalize_one(f: dict) -> dict:
    if not isinstance(f, dict) or not f.get("path"):
        raise ValueError("each edit needs a 'path'")
    out = {"path": f["path"]}
    if "content" in f and f["content"] is not None:
        out["content"] = f["content"]
        out["edits"] = []
        return out
    if f.get("diff"):
        # Unified diff: parsed into content-based search/replace hunks, ignoring line numbers.
        out["edits"] = [{"search": b, "replace": a} for b, a in parse_unified_diff(f["diff"])]
        if not out["edits"]:
            raise ValueError("could not parse any hunks from the unified diff")
        return out
    edits = f.get("edits")
    if edits is None:
        # single search/replace pair inline
        if "search" in f or "replace" in f:
            edits = [{"search": f.get("search", ""), "replace": f.get("replace", "")}]
        else:
            edits = []
    if not isinstance(edits, list):
        raise ValueError("'edits' must be a list of {search, replace}")
    out["edits"] = [{"search": e.get("search", ""), "replace": e.get("replace", "")} for e in edits]
    return out


def parse_unified_diff(text: str) -> list:
    """Parse a unified diff into a list of (before, after) content blocks, one per hunk.

    Line numbers in the @@ headers are ignored on purpose (models get them wrong); each hunk is applied
    as a content search/replace against the current file, so it lands wherever the context actually is.
    Context lines join both sides, `-` lines the before, `+` lines the after."""
    hunks = []
    before, after, in_hunk = [], [], False

    def flush():
        if in_hunk and (before or after):
            hunks.append(("\n".join(before), "\n".join(after)))

    lines = _to_lf(text).split("\n")
    if lines and lines[-1] == "":
        lines.pop()             # drop the trailing-newline artifact, not a real blank context line
    for raw in lines:
        if raw.startswith("@@"):
            flush()
            before, after, in_hunk = [], [], True
            continue
        if raw.startswith("--- ") or raw.startswith("+++ ") or raw.startswith("diff ") \
                or raw.startswith("index ") or raw.startswith("\\"):
            continue
        if not in_hunk:
            continue
        if raw.startswith("-"):
            before.append(raw[1:])
        elif raw.startswith("+"):
            after.append(raw[1:])
        elif raw.startswith(" "):
            before.append(raw[1:])
            after.append(raw[1:])
        elif raw == "":
            before.append("")
            after.append("")
    flush()
    return hunks


def _detect_eol(text: str) -> str:
    """The dominant end-of-line for `text`; used to re-emit edited content in the file's own style."""
    if "\r\n" in text:
        return "\r\n"
    return "\n"


def _to_lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _leading_ws(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _closest(content: str, search: str) -> str:
    """The block of file lines most similar to `search`, to echo in a rejection (display only)."""
    clines = content.split("\n")
    slines = search.split("\n")
    win = max(1, len(slines))
    best_ratio, best = 0.0, ""
    sm = difflib.SequenceMatcher()
    sm.set_seq2("\n".join(slines))
    for i in range(0, max(1, len(clines) - win + 1)):
        cand = "\n".join(clines[i : i + win])
        sm.set_seq1(cand)
        r = sm.ratio()
        if r > best_ratio:
            best_ratio, best = r, cand
    return best if best_ratio >= 0.5 else ""


def _apply_search_replace(content: str, search: str, replace: str):
    """Return (new_content, None) on a unique apply, or (None, reason) on reject.

    Ladder: exact-unique substring, then uniform-indentation-tolerant line match (unique), else reject.
    Ambiguity (more than one match) is a rejection, never a guess."""
    if search == "":
        return None, "empty search block; use the whole-file format to create or replace a file"

    # 1. exact substring, must be unique.
    count = content.count(search)
    if count == 1:
        return content.replace(search, replace, 1), None
    if count > 1:
        return None, f"ambiguous: the search block matches {count} places; add surrounding context to pin one"

    # 2. uniform-indentation-tolerant line match, must be unique.
    clines = content.split("\n")
    slines = search.split("\n")
    n = len(slines)
    stripped_s = [ln.strip() for ln in slines]
    hits = []
    for i in range(0, len(clines) - n + 1):
        window = clines[i : i + n]
        if [ln.strip() for ln in window] == stripped_s:
            hits.append(i)
    if len(hits) == 1:
        i = hits[0]
        # Re-indent the replacement by the delta between the file block's indent and the search's indent,
        # so a model that dropped the leading indentation still lands correctly-indented code.
        file_indent = _leading_ws(clines[i]) if clines[i].strip() else ""
        search_indent = _leading_ws(slines[0]) if slines[0].strip() else ""
        # Rebase each non-blank replace line: drop the search block's leading indent, add the file's.
        # This preserves the replace block's own relative nesting while matching the file's indentation.
        reindented = []
        for rl in replace.split("\n"):
            if not rl.strip():
                reindented.append(rl)
                continue
            body = rl[len(search_indent):] if rl.startswith(search_indent) else rl.lstrip()
            reindented.append(file_indent + body)
        new_lines = clines[:i] + reindented + clines[i + n:]
        return "\n".join(new_lines), None
    if len(hits) > 1:
        return None, f"ambiguous: the search block matches {len(hits)} places; add surrounding context to pin one"

    return None, "not found"


def render_diff(path: str, old: str, new: str) -> str:
    """The single unified-diff renderer for the whole codebase (approval preview + CLI both call this)."""
    diff = difflib.unified_diff(
        _to_lf(old).split("\n"), _to_lf(new).split("\n"),
        fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="",
    )
    return "\n".join(diff)


def _compute(args: dict, allowed_write: list, *, home=None) -> EditResult:
    """Validate every hunk across every file without writing. Populates EditResult.files with the new
    content for each file when ok, or EditResult.rejections when not."""
    res = EditResult()
    try:
        specs = normalize_edits(args)
    except ValueError as e:
        res.ok = False
        res.message = f"{REJECT_PREFIX} (no changes written). {e}."
        return res

    if not allowed_write:
        res.ok = False
        res.message = (f"{REJECT_PREFIX} (no changes written). file_edit is disabled; add paths to "
                       "agent.allowedWritePaths in config/user.json to enable.")
        return res

    diffs = []
    for spec in specs:
        path = spec["path"]
        p = bob_fsguard.abs_path(path, allowed_write)
        if not bob_fsguard.is_allowed(p, allowed_write):
            res.ok = False
            res.rejections.append({"path": path, "hunk_index": None, "reason": "outside allowedWritePaths",
                                   "closest": ""})
            continue
        if bob_fsguard.is_denied_secret(p, home=home):
            res.ok = False
            res.rejections.append({"path": path, "hunk_index": None, "reason": "sensitive file refused",
                                   "closest": ""})
            continue

        exists = p.exists()
        old = p.read_text(encoding="utf-8", errors="replace") if exists else ""

        # Whole-file form.
        if "content" in spec:
            new = spec["content"]
            res.files[path] = new
            res.originals[path] = old
            diffs.append(render_diff(path, old, new))
            continue

        edits = spec["edits"]
        if not edits:
            res.ok = False
            res.rejections.append({"path": path, "hunk_index": None,
                                   "reason": "no edits supplied for this file", "closest": ""})
            continue

        # Search/replace: creating a new file requires a whole-file content, not a search block.
        if not exists:
            res.ok = False
            res.rejections.append({"path": path, "hunk_index": 0,
                                   "reason": "file does not exist; use the whole-file 'content' form to create it",
                                   "closest": ""})
            continue

        working = _to_lf(old)
        file_rejected = False
        for idx, e in enumerate(edits):
            new_working, reason = _apply_search_replace(working, _to_lf(e["search"]), _to_lf(e["replace"]))
            if reason is not None:
                res.ok = False
                file_rejected = True
                res.rejections.append({"path": path, "hunk_index": idx, "reason": reason,
                                       "closest": _closest(working, _to_lf(e["search"]))})
                break
            working = new_working
        if file_rejected:
            continue
        # Re-emit in the file's own EOL style.
        new = working.replace("\n", _detect_eol(old)) if _detect_eol(old) == "\r\n" else working
        res.files[path] = new
        res.originals[path] = old
        diffs.append(render_diff(path, old, working))

    res.diff = "\n".join(d for d in diffs if d)
    if not res.ok:
        res.files = {}          # all-or-nothing: never expose partial writes
        res.message = _reject_message(res.rejections)
    return res


def _reject_message(rejections: list) -> str:
    lines = [f"{REJECT_PREFIX} (no changes written)."]
    for r in rejections:
        where = f"hunk {r['hunk_index']} of " if r.get("hunk_index") is not None else ""
        lines.append(f"- {where}{r['path']}: {r['reason']}")
        if r.get("closest"):
            lines.append("  Did you mean to match these existing lines?")
            for cl in r["closest"].split("\n")[:12]:
                lines.append(f"    {cl}")
    lines.append("Re-read the file and resubmit only the failed search block(s) with the exact current text.")
    return "\n".join(lines)


def preview_edits(args: dict, allowed_write: list, *, home=None) -> EditResult:
    """Compute the edit and render its diff WITHOUT writing. The approval preview and CLI dry-run use this."""
    return _compute(args, allowed_write, home=home)


def apply_edits(args: dict, allowed_write: list, *, home=None) -> EditResult:
    """Validate all hunks, then write every file if all pass; on any rejection, write nothing."""
    res = _compute(args, allowed_write, home=home)
    if not res.ok:
        return res
    written = []
    for path, new in res.files.items():
        p = bob_fsguard.abs_path(path, allowed_write)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(new, encoding="utf-8")
        written.append(path)
    res.message = f"Applied edits to {len(written)} file(s): " + ", ".join(written)
    return res
