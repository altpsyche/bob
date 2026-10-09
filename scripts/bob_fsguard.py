"""Filesystem guard: path allowlist + secrets denylist, shared by the file tools and the code index.

Pure and stateless — callers pass the allow-list (and, for secret checks, the home dir), so this module
holds no config globals and is safe to import from any tool. The secrets denylist refuses sensitive files
(the litellm key / api tokens in config.json / secrets.json, *.psd1 config, *.db session/memory stores,
logs, .env files, the generated client configs that embed Bob's LiteLLM key, n8n's config (its credential
encryption key), and the usual home credential dirs) even when they fall
inside an allowed root, which by default is the repo root and would otherwise expose them.

Writes are refused on a wider set (is_denied_write): Bob's own configuration and code, so an unattended
caller with a write root over the checkout cannot change its own config or plant code the next process
imports. An attended run may write them one approved call at a time (protected_write_allowed; the
approval gate always asks for such a call). The human CLI paths that edit config/user.json write it
directly, not here.
"""
from pathlib import Path

import osenv

REPO = Path(__file__).resolve().parent.parent

# Generated client configs (repo-relative) that embed Bob's LiteLLM key (Continue, aider; litellm.yaml on an
# install generated before the key moved to the proxy's environment) or wire a client to it (dsh, aider's
# metadata beside its conf). The ONE list: the generators write these 0600 and the guard refuses them.
KEY_BEARING = frozenset({
    "config/litellm.yaml", "config/continue/config.yaml", "config/aider/.aider.conf.yml",
    "config/aider/model-metadata.json", "config/dsh/settings.yaml", "config/dsh/cordis.patch.yml"})

# config.json / secrets.json carry litellmKey / apiTokens / provider keys; .credentials.yaml is the
# DeepSeek Harness credential store.
DENY_BASENAMES = {"config.json", "secrets.json", ".credentials.yaml"}
DENY_SUFFIXES = (".psd1", ".db")   # .psd1 config files; *.db session/memory stores
_N8N_DATA = ("tools", "n8n-data")   # n8n's user folder; any `config` under it holds the encryption key

# Repo-relative files and trees agent/MCP writes may not touch unattended: the config overlays and
# registries Bob loads at startup, Bob's own code under scripts/, and any Python under plugins/ (the
# loader imports tool.py, which imports its siblings). Any of them could raise a caller's own trust.
WRITE_DENY_FILES = frozenset({"config/user.json", "config/defaults.json", "config/models.json"})
WRITE_DENY_TREES = ("scripts",)


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
    if _is_generated_secret(rp):
        return True
    return in_secret_dir(rp, home)


_KEY_BEARING_FOLDED = frozenset(k.casefold() for k in KEY_BEARING)
_WRITE_DENY_FILES_FOLDED = frozenset(k.casefold() for k in WRITE_DENY_FILES)


def _repo_rel(rp: Path):
    """`rp` (resolved) as casefolded parts relative to the repo root, or None when it is outside.
    Casefolded because on a case-insensitive filesystem (APFS, NTFS) `CONFIG/User.json` opens the same
    file, so a case-sensitive match would let it past the guard."""
    try:
        repo = REPO.resolve()
    except OSError:
        return None
    parts = tuple(seg.casefold() for seg in rp.parts)
    root = tuple(seg.casefold() for seg in repo.parts)
    if parts[:len(root)] != root:
        return None
    return parts[len(root):]


def is_protected_code(target: Path) -> bool:
    """True for Bob's own config and code (WRITE_DENY_*), which agent/MCP writes must not change."""
    try:
        rp = target.resolve()
    except Exception:
        return True
    rel = _repo_rel(rp)
    if not rel:
        return False
    if "/".join(rel) in _WRITE_DENY_FILES_FOLDED or rel[0] in WRITE_DENY_TREES:
        return True
    return rel[0] == "plugins" and rel[-1].endswith(".py")


def protected_write_allowed() -> bool:
    """Whether the tool call now running may write Bob's own config or code: only inside an attended
    run, one with an approver and no unattended allow-set, where the approval gate has just asked for
    this exact call (bob_permissions.touches_protected). A call dispatched outside a run is refused."""
    try:
        from tool_registry import get_run_context
    except ImportError:
        return False
    ctx = get_run_context()
    return (ctx is not None and getattr(ctx, "approve", None) is not None
            and getattr(ctx, "unattended_allow", None) is None)


def is_denied_write(target: Path, home: Path = None) -> bool:
    """True when an agent/MCP write to `target` must be refused: every secret is_denied_secret refuses,
    plus Bob's own config and code (is_protected_code) outside an attended, approved call."""
    if is_denied_secret(target, home=home):
        return True
    return is_protected_code(target) and not protected_write_allowed()


def _is_generated_secret(rp: Path) -> bool:
    """True for a key-bearing file Bob generates inside the repo (KEY_BEARING) and n8n's config files.
    Compared casefolded: on a case-insensitive filesystem (APFS, NTFS) `CONFIG/Continue/config.yaml`
    opens the same file, so a case-sensitive match would let it past the denylist."""
    rel = _repo_rel(rp)
    if rel is None:
        return False
    if "/".join(rel) in _KEY_BEARING_FOLDED:
        return True
    return rel[:2] == _N8N_DATA and rp.name.casefold() == "config"
