"""Bob session store — persist agent conversations to SQLite.

A Session bundles an id, a rolling message history ({role, content} turns), and an optional
token budget. The agent HTTP server uses it to support multi-turn, multi-client conversations
and to bound spend per client; the CLI stays stateless. This is the seam for the multi-user /
MCP work — id + history + budget persistence, with per-owner scoping.

Concurrency: FastAPI runs the sync route handlers in a threadpool, so the store is hit
from many threads at once. Each thread gets its **own** SQLite connection (`threading.local`),
the DB runs in **WAL** mode with a **busy_timeout**, and `append_turn` does its read-modify-write
inside a single `BEGIN IMMEDIATE` transaction — so concurrent appends can't lose turns.
"""
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).parent.parent


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionStore:
    """SQLite-backed session store, safe under concurrent threadpool access.

    One connection per thread (`threading.local`); WAL + busy_timeout; atomic `append_turn`.
    """

    def __init__(self, db_path, default_owner: str = "local"):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._all_conns: list = []          # every opened conn, for close()
        self._conns_lock = threading.Lock()
        self._default_owner = default_owner  # owner stamped when none is supplied (e.g. litellmKey)
        conn = self._conn()                 # opens + registers this thread's conn
        conn.execute("PRAGMA journal_mode=WAL")   # persistent DB property; concurrent readers + 1 writer
        self._ensure_schema(conn)

    # -- connection management ------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        """The calling thread's own connection (opened on first use). isolation_level=None puts
        the connection in autocommit mode so explicit BEGIN IMMEDIATE transactions work cleanly."""
        c = getattr(self._local, "conn", None)
        if c is None:
            # check_same_thread=False so close() can run from a different thread than the one that
            # opened the conn (FastAPI threadpool workers open them; teardown closes from elsewhere).
            # Safe because each conn is *used* by exactly one thread — only close() crosses threads.
            c = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None, check_same_thread=False)
            c.execute("PRAGMA busy_timeout=5000")   # per-connection; wait out a transient writer lock
            self._local.conn = c
            with self._conns_lock:
                self._all_conns.append(c)
        return c

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        """Single create+migrate site. Idempotent — safe on a fresh DB and on one that predates
        the owner_id or client columns. Backfills owner_id from client (or the default)."""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id           TEXT PRIMARY KEY,
                created_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL,
                history      TEXT NOT NULL DEFAULT '[]',
                token_budget INTEGER NOT NULL DEFAULT 0,
                tokens_spent INTEGER NOT NULL DEFAULT 0,
                client       TEXT,
                name         TEXT
            )
            """
        )
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        if "client" not in cols:                       # DB older than the client column
            conn.execute("ALTER TABLE sessions ADD COLUMN client TEXT")
        if "owner_id" not in cols:                     # add + backfill ownership
            conn.execute("ALTER TABLE sessions ADD COLUMN owner_id TEXT")
            conn.execute(
                "UPDATE sessions SET owner_id = COALESCE(NULLIF(client,''), ?) WHERE owner_id IS NULL",
                [self._default_owner],
            )
        if "name" not in cols:                         # human-readable session label (nullable)
            conn.execute("ALTER TABLE sessions ADD COLUMN name TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_updated_at ON sessions(updated_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_owner ON sessions(owner_id, updated_at DESC)")

    def close(self) -> None:
        """Close every connection this store opened (test teardown / shutdown)."""
        with self._conns_lock:
            for c in self._all_conns:
                try:
                    c.close()
                except Exception:
                    pass
            self._all_conns = []
        self._local = threading.local()

    # -- CRUD -----------------------------------------------------------------

    def create(self, token_budget: int = 0, owner_id: str = None) -> dict:
        sid = uuid.uuid4().hex
        now = _now()
        owner = owner_id or self._default_owner
        self._conn().execute(
            "INSERT INTO sessions (id, created_at, updated_at, history, token_budget, tokens_spent, client, owner_id)"
            " VALUES (?,?,?,?,?,?,?,?)",
            [sid, now, now, "[]", int(token_budget), 0, owner, owner],  # client mirrors owner (compat)
        )  # autocommit (isolation_level=None)
        return self.get(sid)

    def get(self, sid: str):
        row = self._conn().execute(
            "SELECT id, created_at, updated_at, history, token_budget, tokens_spent, client, owner_id, name"
            " FROM sessions WHERE id=?",
            [sid],
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "created_at": row[1],
            "updated_at": row[2],
            "history": json.loads(row[3]),
            "token_budget": row[4],
            "tokens_spent": row[5],
            "client": row[6],
            "owner_id": row[7],
            "name": row[8],
        }

    def get_owned(self, sid: str, owner_id: str):
        """Return the session only if owner_id matches; else None (caller maps to 404 — no leak)."""
        s = self.get(sid)
        return s if s and s.get("owner_id") == owner_id else None

    def append_turn(self, sid: str, user_content: str, assistant_content, tokens_used: int = 0):
        """Append a user turn (+ assistant reply if any) and add tokens_used to the tally.

        Atomic: BEGIN IMMEDIATE takes the write lock before the read, so two concurrent appends
        serialize instead of racing on a stale history (no lost updates)."""
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT history FROM sessions WHERE id=?", [sid]).fetchone()
            if row is None:
                c.execute("ROLLBACK")
                return None
            history = json.loads(row[0])
            history.append({"role": "user", "content": user_content})
            if assistant_content is not None:
                history.append({"role": "assistant", "content": assistant_content})
            c.execute(
                "UPDATE sessions SET history=?, tokens_spent=tokens_spent+?, updated_at=? WHERE id=?",
                [json.dumps(history), int(tokens_used), _now(), sid],
            )
            c.execute("COMMIT")
        except Exception:
            try:
                c.execute("ROLLBACK")
            except Exception:
                pass
            raise
        return self.get(sid)

    def set_name_owned(self, sid: str, owner_id: str, name: str) -> bool:
        """Set a session's human-readable name, only if owner_id matches. Does NOT bump updated_at —
        renaming isn't activity, so it must not reorder the recency-sorted list. Returns True if set."""
        cur = self._conn().execute(
            "UPDATE sessions SET name=? WHERE id=? AND owner_id=?", [name, sid, owner_id])
        return cur.rowcount > 0

    def over_budget(self, sid: str) -> bool:
        """True if the session has a positive token_budget and has reached/exceeded it."""
        s = self.get(sid)
        if not s or not s["token_budget"]:
            return False
        return s["tokens_spent"] >= s["token_budget"]

    def delete(self, sid: str) -> bool:
        cur = self._conn().execute("DELETE FROM sessions WHERE id=?", [sid])  # autocommit
        return cur.rowcount > 0

    def delete_owned(self, sid: str, owner_id: str) -> bool:
        """Delete only if owner_id matches. Non-owner sees the same {deleted: False} as a missing id."""
        cur = self._conn().execute("DELETE FROM sessions WHERE id=? AND owner_id=?", [sid, owner_id])
        return cur.rowcount > 0

    def delete_all_owned(self, owner_id: str) -> int:
        """Delete every session for one owner. Returns how many were removed (owner-scoped, so it can't
        wipe another owner's history)."""
        cur = self._conn().execute("DELETE FROM sessions WHERE owner_id=?", [owner_id])
        return cur.rowcount

    def list_ids(self) -> list:
        return [
            r[0]
            for r in self._conn()
            .execute("SELECT id FROM sessions ORDER BY updated_at DESC")
            .fetchall()
        ]

    def list_owned(self, owner_id: str) -> list:
        """Session ids for one owner, newest first. Owner-scoped counterpart of list_ids
        (which is cross-owner) — index-covered by idx_sessions_owner."""
        return [
            r[0]
            for r in self._conn()
            .execute(
                "SELECT id FROM sessions WHERE owner_id=? ORDER BY updated_at DESC",
                [owner_id],
            )
            .fetchall()
        ]
