"""Filesystem guard: path allowlist + secrets denylist, shared by the file tools and the code index.

Pure and stateless — callers pass the allow-list (and, for secret checks, the home dir), so this module
holds no config globals and is safe to import from any tool. The secrets denylist refuses sensitive files
(the litellm key / api tokens in config.json / secrets.json, *.psd1 config, *.db session/memory stores,
logs, .env files, and the usual home credential dirs) even when they fall inside an allowed root, which
by default is the repo root and would otherwise expose them.
"""
from pathlib import Path

import osenv

DENY_BASENAMES = {"config.json", "secrets.json"}  # carry litellmKey / apiTokens / provider keys
DENY_SUFFIXES = (".psd1", ".db")   # .psd1 config files; *.db session/memory stores


def default_home() -> Path:
    """User home dir. Callers that need it test-overridable resolve their own and pass it in."""
    return Path.home()


def abs_path(path: str, allowed: list) -> Path:
    """Resolve a caller-supplied path to an absolute Path. A RELATIVE path resolves against the first
    allowed root (the repo root by default), NOT the process cwd -- so `.`/`./`/`sub/file` mean "inside
    the workspace" regardless of where `bob` was launched from. Absolute paths pass through unchanged."""
    p = Path(path)
    if p.is_absolute():
        return p
    base = allowed[0] if allowed else Path.cwd()
    return base / p


def is_allowed(target: Path, allowed: list) -> bool:
    """True if `target` resolves inside one of the allowed roots."""
    try:
        resolved = target.resolve()
        return any(resolved.is_relative_to(a.resolve()) for a in allowed)
    except Exception:
        return False


def in_secret_dir(rp: Path, home: Path) -> bool:
    """True if the resolved path sits under a platform secret directory: the resolved data-dir secrets
    file's dir, and the usual home credential dirs."""
    candidates = [
        osenv.secrets_file(),                 # <data_dir>/secrets.json (any OS)
        home / ".ssh", home / ".aws",
        home / ".gnupg", home / ".config" / "bob",
    ]
    for base in candidates:
        try:
            # Resolve BOTH sides: `rp` is already resolved, so the base must be too -- otherwise a
            # Windows 8.3 short-name / symlinked temp home (e.g. RUNNER~1) never matches the long
            # resolved target and the denial silently misses (green on Linux, leaks on Windows).
            b = base.resolve()
            if rp == b or rp.is_relative_to(b):
                return True
        except (OSError, ValueError):
            continue
    return False


def is_denied_secret(target: Path, home: Path = None) -> bool:
    """True for sensitive files that must never be read or written even inside an allowed root."""
    if home is None:
        home = default_home()
    try:
        rp = target.resolve()
    except Exception:
        return True
    name = rp.name.lower()
    if name in DENY_BASENAMES or name.startswith(".env"):
        return True
    if rp.suffix.lower() in DENY_SUFFIXES:
        return True
    if "logs" in (seg.lower() for seg in rp.parts):
        return True
    return in_secret_dir(rp, home)
