#!/usr/bin/env python3
"""Bob auth store — DB-backed agent API tokens with hot revocation, RBAC scopes, per-owner rate.

Holds the static config token→owner map (`config_token_owners`, shared by the agent HTTP server and
the MCP HTTP transport so both accept exactly the same bearers) and extends it with a SQLite token
store living beside the session DB (`data/sessions.db`). It closes the multi-user gap: an admin can
issue a scoped, rate-limited token to an owner and revoke it **without restarting the server** (the
server hashes+looks up the presented bearer per request, so `revoked=1` takes effect on the next call).

Security invariants:
  - **Token values are never stored** — only a salted SHA-256 hash. `issue()` returns the plaintext
    once; it cannot be recovered afterwards (list/lookup never expose it).
  - The salt resolves through the **secret seam** (`osenv.secret('agent_token_salt')`); absent, a
    per-install random salt is generated and persisted in the DB's own `auth_meta` (never a tracked file
    — `data/` is gitignored). So the hash is stable across restarts without a plaintext secret on disk.

Config tokens (`agent.apiTokens` + the litellm key) remain a **static fallback** — the store is
additive and only consulted when `agent.authStore` is on, so with it off the behavior is unchanged.
The litellm key is the one bob_core._litellm_key resolves (a generated secret unless the user set one);
`agent.acceptLitellmKey = false` drops it from the accepted set so only issued tokens open the API.

`authenticate` + `rate_allowed` are the ONE bearer check: the agent API and the MCP HTTP transport both
resolve a caller to an `Identity` (owner, scopes, rate) through them.

Admin CLI (the `bob agent token` verb front-door is deferred; this stays a CLI-only admin surface):
    python scripts/bob_authstore.py issue  --owner alice --scopes "file_*,web_fetch" --rate 60
    python scripts/bob_authstore.py list
    python scripts/bob_authstore.py revoke <hash-prefix>
"""
import hashlib
import json
import secrets as _secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).parent.parent

_SALT_SECRET = "agent_token_salt"   # secret name (osenv.secret)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _accepted_litellm_key(config: dict):
    """The litellm key as a bearer, or None when agent.acceptLitellmKey is false."""
    if not config.get("agent", {}).get("acceptLitellmKey", True):
        return None
    from bob_core import _litellm_key
    return _litellm_key(config)


def config_token_owners(config: dict) -> dict:
    """Map each accepted STATIC bearer token to an owner id. The litellm key (unless
    agent.acceptLitellmKey is false) maps to agent.defaultOwner; agent.apiTokens entries may be
    {token, owner} records or bare strings (legacy: token maps to itself as the owner).

    One source for every HTTP surface Bob exposes (the agent API and the MCP Streamable HTTP
    transport), so a token issued in config is accepted identically by both."""
    agent = config.get("agent", {})
    default_owner = agent.get("defaultOwner", "local")
    owners = {}
    key = _accepted_litellm_key(config)
    if key:
        owners[key] = default_owner
    for entry in agent.get("apiTokens", []):
        if isinstance(entry, dict) and entry.get("token"):
            owners[entry["token"]] = entry.get("owner") or default_owner
        elif isinstance(entry, str) and entry:
            owners[entry] = entry  # legacy flat-string token -> token-as-owner
    return owners


def config_token_meta(config: dict) -> dict:
    """Per-config-token scopes + rate, parallel to config_token_owners. A dict apiTokens entry may
    carry optional `scopes` (tool globs / role:<name>) and `rate` (per-min); everything else defaults to
    unrestricted scopes + agent.defaultRatePerMin (scopes None + rate 0 => no filtering, no limit)."""
    agent = config.get("agent", {})
    default_rate = int(agent.get("defaultRatePerMin", 0) or 0)
    meta = {}
    key = _accepted_litellm_key(config)
    if key:
        meta[key] = {"scopes": None, "rate": default_rate}
    for entry in agent.get("apiTokens", []):
        if isinstance(entry, dict) and entry.get("token"):
            meta[entry["token"]] = {"scopes": entry.get("scopes"),
                                    "rate": int(entry.get("rate", default_rate) or 0)}
        elif isinstance(entry, str) and entry:
            meta[entry] = {"scopes": None, "rate": default_rate}
    return meta


class Identity:
    """A resolved caller: owner id + optional RBAC scopes + per-minute rate. `scopes=None` means
    unrestricted; a list restricts tools (globs) and model roles (`role:<name>` entries)."""
    __slots__ = ("owner", "scopes", "rate")

    def __init__(self, owner: str, scopes=None, rate: int = 0):
        self.owner = owner
        self.scopes = scopes
        self.rate = int(rate or 0)

    def allowed_roles(self):
        """The `role:<name>` scopes as a set, or None when the identity carries none (unrestricted)."""
        roles = {s[5:] for s in (self.scopes or []) if isinstance(s, str) and s.startswith("role:")}
        return roles or None

    def tool_globs(self) -> list:
        """The tool-glob scopes (everything that isn't a role scope); [] means every tool."""
        return [s for s in (self.scopes or []) if isinstance(s, str) and not s.startswith("role:")]


def bearer_token(authorization: str) -> str:
    """The token of an `Authorization: Bearer <token>` header, '' for anything else."""
    header = authorization or ""
    return header[7:].strip() if header.startswith("Bearer ") else ""


def authenticate(authorization: str, token_owner: dict, token_meta: dict = None, store=None):
    """Resolve a bearer header to an Identity, or None when it is missing or unknown. The static config
    map is checked first, then (only when a store is given) the DB-backed tokens, hashed and looked up
    per call so a revoked token stops working on the next request."""
    token = bearer_token(authorization)
    if not token:
        return None
    owner = (token_owner or {}).get(token)
    if owner is not None:
        meta = (token_meta or {}).get(token) or {}
        return Identity(owner, meta.get("scopes"), meta.get("rate", 0))
    if store is not None:
        rec = store.lookup(token)   # None if absent or revoked
        if rec is not None:
            return Identity(rec["owner"], rec.get("scopes"), rec.get("rate_per_min", 0))
    return None


def rate_allowed(buckets: dict, identity, now: float) -> bool:
    """Per-owner token-bucket rate limit: False when the owner has spent its allowance. rate<=0 means
    unlimited. The bucket refills at `rate` tokens/min; `buckets` is the caller's owner -> state map."""
    rate = identity.rate
    if rate <= 0:
        return True
    tokens, last = buckets.get(identity.owner, (float(rate), now))
    tokens = min(float(rate), tokens + (now - last) * rate / 60.0)
    if tokens < 1.0:
        buckets[identity.owner] = (tokens, now)
        return False
    buckets[identity.owner] = (tokens - 1.0, now)
    return True


def open_store(config: dict):
    """The DB-backed token store beside the session DB when agent.authStore is on, else None."""
    if not config.get("agent", {}).get("authStore", False):
        return None
    from bob_core import session_db_path
    return AuthStore(session_db_path(config))


class AuthStore:
    """SQLite-backed token store, safe under FastAPI's threadpool (one connection per thread, WAL).

    Mirrors bob_session.SessionStore's connection discipline so it can share `data/sessions.db`."""

    def __init__(self, db_path, salt: str = None):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._all_conns: list = []
        self._conns_lock = threading.Lock()
        conn = self._conn()
        conn.execute("PRAGMA journal_mode=WAL")
        self._ensure_schema(conn)
        # salt injectable for tests; else secret seam, else a persisted per-install random salt.
        self._salt = salt if salt is not None else self._resolve_salt(conn)

    # -- connections (mirrors SessionStore) -----------------------------------

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None,
                                check_same_thread=False)
            c.execute("PRAGMA busy_timeout=5000")
            self._local.conn = c
            with self._conns_lock:
                self._all_conns.append(c)
        return c

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_tokens (
                token_hash   TEXT PRIMARY KEY,
                owner        TEXT NOT NULL,
                scopes       TEXT NOT NULL DEFAULT '[]',
                rate_per_min INTEGER NOT NULL DEFAULT 0,
                created_at   TEXT NOT NULL,
                revoked      INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS auth_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )

    def _resolve_salt(self, conn: sqlite3.Connection) -> str:
        try:
            from osenv import secret
            s = secret(_SALT_SECRET, default=None)
            if s:
                return s
        except Exception:
            pass
        row = conn.execute("SELECT value FROM auth_meta WHERE key='salt'").fetchone()
        if row:
            return row[0]
        gen = _secrets.token_hex(16)
        conn.execute("INSERT OR REPLACE INTO auth_meta(key, value) VALUES('salt', ?)", (gen,))
        return gen

    # -- hashing --------------------------------------------------------------

    def _hash(self, token: str) -> str:
        return hashlib.sha256((self._salt + (token or "")).encode("utf-8")).hexdigest()

    # -- API ------------------------------------------------------------------

    def issue(self, owner: str, scopes=None, rate_per_min: int = 0) -> str:
        """Create a token for `owner` and return its plaintext ONCE (only the salted hash is stored)."""
        token = "bob_" + _secrets.token_urlsafe(32)
        self._conn().execute(
            "INSERT OR REPLACE INTO auth_tokens"
            "(token_hash, owner, scopes, rate_per_min, created_at, revoked) VALUES(?,?,?,?,?,0)",
            (self._hash(token), owner, json.dumps(list(scopes or [])), int(rate_per_min or 0), _now()),
        )
        return token

    def lookup(self, token: str):
        """Resolve a presented bearer -> {owner, scopes, rate_per_min} or None. A revoked or unknown
        token returns None (hot revocation — the row is checked on every call)."""
        row = self._conn().execute(
            "SELECT owner, scopes, rate_per_min, revoked FROM auth_tokens WHERE token_hash=?",
            (self._hash(token),),
        ).fetchone()
        if row is None or row[3]:
            return None
        return {"owner": row[0], "scopes": json.loads(row[1] or "[]"), "rate_per_min": int(row[2] or 0)}

    def revoke(self, token: str) -> bool:
        """Revoke by presented plaintext token. Returns True if a live token was revoked."""
        return self._revoke_where("token_hash=? AND revoked=0", (self._hash(token),)) > 0

    def revoke_prefix(self, prefix: str) -> int:
        """Revoke by token-hash prefix (what `list` shows, since plaintext isn't stored). Returns count."""
        if not prefix:
            return 0
        return self._revoke_where("token_hash LIKE ? AND revoked=0", (prefix + "%",))

    def _revoke_where(self, where: str, params) -> int:
        cur = self._conn().execute(f"UPDATE auth_tokens SET revoked=1 WHERE {where}", params)
        return cur.rowcount

    def list(self) -> list:
        """All tokens, newest-visible order, WITHOUT plaintext (only a hash prefix, for revoke-by-id)."""
        rows = self._conn().execute(
            "SELECT token_hash, owner, scopes, rate_per_min, created_at, revoked "
            "FROM auth_tokens ORDER BY created_at"
        ).fetchall()
        return [
            {"hash_prefix": r[0][:12], "owner": r[1], "scopes": json.loads(r[2] or "[]"),
             "rate_per_min": int(r[3] or 0), "created_at": r[4], "revoked": bool(r[5])}
            for r in rows
        ]

    def close(self) -> None:
        with self._conns_lock:
            for c in self._all_conns:
                try:
                    c.close()
                except Exception:
                    pass
            self._all_conns.clear()


# --------------------------------------------------------------------------- admin CLI

def _open_default() -> AuthStore:
    from bob_core import load_config, session_db_path
    try:
        config = load_config()
    except Exception:
        config = {}
    return AuthStore(session_db_path(config))


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="bob-authstore", description="Manage Bob agent API tokens.")
    sub = p.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("issue", help="issue a new token for an owner")
    pi.add_argument("--owner", required=True)
    pi.add_argument("--scopes", default="", help="comma-separated tool globs / role:<name> (empty = all)")
    pi.add_argument("--rate", type=int, default=0, help="per-minute rate limit (0 = unlimited)")
    pr = sub.add_parser("revoke", help="revoke tokens by hash prefix (from `list`)")
    pr.add_argument("prefix")
    sub.add_parser("list", help="list tokens (owner/scopes/rate; never the plaintext)")
    args = p.parse_args(argv)

    store = _open_default()
    try:
        if args.cmd == "issue":
            scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]
            token = store.issue(args.owner, scopes, args.rate)
            print("Store this token now — it will NOT be shown again:")
            print(token)
        elif args.cmd == "revoke":
            print(f"Revoked {store.revoke_prefix(args.prefix)} token(s) matching {args.prefix!r}.")
        elif args.cmd == "list":
            rows = store.list()
            if not rows:
                print("(no tokens)")
            for r in rows:
                flag = " REVOKED" if r["revoked"] else ""
                print(f"{r['hash_prefix']}  owner={r['owner']}  scopes={r['scopes']}  "
                      f"rate={r['rate_per_min']}  {r['created_at']}{flag}")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
