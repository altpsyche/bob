#!/usr/bin/env python3
"""Bob auth store (O8) — DB-backed agent API tokens with hot revocation, RBAC scopes, per-owner rate.

Extends N1's static token→owner map ([bob_agent_server.py] `_build_token_owner`) with a SQLite token
store living beside the N2 session DB (`data/sessions.db`). It closes the multi-user gap: an admin can
issue a scoped, rate-limited token to an owner and revoke it **without restarting the server** (the
server hashes+looks up the presented bearer per request, so `revoked=1` takes effect on the next call).

Security invariants:
  - **Token values are never stored** — only a salted SHA-256 hash. `issue()` returns the plaintext
    once; it cannot be recovered afterwards (list/lookup never expose it).
  - The salt resolves through the **C3 secret seam** (`osenv.secret('agent_token_salt')`); absent, a
    per-install random salt is generated and persisted in the DB's own `auth_meta` (never a tracked file
    — `data/` is gitignored). So the hash is stable across restarts without a plaintext secret on disk.

Config tokens (N1 `agent.apiTokens` + the litellm key) remain a **static fallback** — the store is
additive and only consulted when `agent.authStore` is on, so default-off is byte-identical to pre-O8.

Admin CLI (the `bob agent token` verb front-door is deferred to avoid verbs.json churn — same discipline
as O4's `--deep`):
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

_SALT_SECRET = "agent_token_salt"   # C3 secret name (osenv.secret)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        # salt injectable for tests; else C3 secret seam, else a persisted per-install random salt.
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
    rel = "data/sessions.db"
    try:
        from bob_core import load_config
        rel = load_config().get("agent", {}).get("sessionDbPath", rel)
    except Exception:
        pass
    return AuthStore(REPO / rel.replace("\\", "/"))


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="bob-authstore", description="Manage Bob agent API tokens (O8).")
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
