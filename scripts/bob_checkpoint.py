"""Checkpoint + rewind: snapshot the files a mutating step is about to touch, so a bad edit can be
undone. This is local undo (revert to a known-good point within/after a run), distinct from durable
resume across process death.

Two backends, both content-addressed:
  - shadow (PRIMARY, universal): copy the affected files into a store under data/, keyed by content hash.
    Works everywhere, including non-git dirs, and never touches the user's git history.
  - git (OPTIONAL, inspectable): where the path sits in a git work-tree, `git stash create` yields a
    dangling commit sha that captures the tree WITHOUT touching the working tree or index -- a real git
    object the user can inspect. These git writes live here, NOT in the deliberately read-only git.py.

Only tool-driven edits are snapshotted; files changed by a shell command are out of scope (as in Claude
Code's checkpointing). The store mirrors bob_session's discipline: SQLite, per-thread connection, WAL,
BEGIN IMMEDIATE, owner-scoped rows. The (run_id, step, owner) key is chosen so durable-run plumbing can
co-own this one store rather than building a parallel one.
"""
import hashlib
import json
import shutil
import sqlite3
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).parent.parent
DEFAULT_DB = REPO / "data" / "checkpoints.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def in_git_repo(path: Path) -> Path:
    """The git work-tree root containing `path`, or None. Uses a direct git subprocess (git.py stays
    read-only; the checkpoint store owns the git writes)."""
    start = path if path.is_dir() else path.parent
    try:
        r = subprocess.run(["git", "-C", str(start), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return Path(r.stdout.strip()) if r.stdout.strip() else None


class CheckpointStore:
    """SQLite-backed, owner-scoped store with two concerns sharing one DB and connection discipline:
    per-step file snapshots keyed by (run_id, step) for rewind, and durable per-run loop state keyed by
    run_id for resume across process death."""

    def __init__(self, db_path=None, shadow_dir=None, default_owner: str = "local"):
        self.path = Path(db_path or DEFAULT_DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.shadow_dir = Path(shadow_dir or (self.path.parent / "checkpoints"))
        self.shadow_dir.mkdir(parents=True, exist_ok=True)
        self._default_owner = default_owner
        self._local = threading.local()
        conn = self._conn()
        conn.execute("PRAGMA journal_mode=WAL")
        self._ensure_schema(conn)

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None, check_same_thread=False)
            c.execute("PRAGMA busy_timeout=5000")
            self._local.conn = c
        return c

    def _ensure_schema(self, conn) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS checkpoints (
                run_id     TEXT NOT NULL,
                step       INTEGER NOT NULL,
                owner_id   TEXT NOT NULL,
                kind       TEXT NOT NULL,          -- 'shadow' | 'git'
                ref        TEXT,                   -- git sha (git) or NULL (shadow)
                files_json TEXT NOT NULL,          -- [{path, hash, existed}] (shadow) or [path] (git)
                created_at TEXT NOT NULL,
                PRIMARY KEY (run_id, step)
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id        TEXT PRIMARY KEY,
                owner_id      TEXT NOT NULL,
                status        TEXT NOT NULL,        -- running|paused|done|failed|cancelled|queued
                goal          TEXT NOT NULL,
                scope         TEXT,
                agent_depth   INTEGER NOT NULL DEFAULT 0,
                step          INTEGER NOT NULL DEFAULT 0,   -- next step to execute
                exit_requested INTEGER NOT NULL DEFAULT 0,
                messages_json TEXT NOT NULL,        -- the loop's message list, tool results embedded
                todos_json    TEXT,
                result        TEXT,
                metrics_json  TEXT,
                lease_holder  TEXT,                 -- process token holding the run (resume coordination)
                lease_expires TEXT,                 -- ISO time the lease goes stale
                pid           INTEGER,              -- detached worker process (P2 task supervision)
                log_path      TEXT,                 -- detached worker's log file
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            )""")
        self._ensure_columns(conn, "runs", {
            "lease_holder": "TEXT", "lease_expires": "TEXT", "pid": "INTEGER", "log_path": "TEXT"})

    def _ensure_columns(self, conn, table: str, cols: dict) -> None:
        """Idempotently add any missing columns to `table` (migrates a store created before the column
        existed). SQLite has no ADD COLUMN IF NOT EXISTS, so check PRAGMA table_info first."""
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, decl in cols.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def has(self, run_id: str, step: int) -> bool:
        row = self._conn().execute(
            "SELECT 1 FROM checkpoints WHERE run_id=? AND step=?", [run_id, step]).fetchone()
        return row is not None

    # --- durable run state ------------------------------------------------------------------------
    def save_run(self, run_id: str, owner: str, status: str, goal: str, messages, step: int,
                 exit_requested: bool = False, scope: str = None, agent_depth: int = 0,
                 todos=None, result: str = None, metrics=None) -> None:
        """Upsert the durable state of a run (one row per run_id, owner-scoped). Persisting `messages`
        (which already carries tool-result turns) is what lets a resume continue without re-running
        completed tools. `created_at` is preserved across updates; `updated_at` always advances."""
        owner = owner or self._default_owner
        now = _now()
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """INSERT INTO runs (run_id, owner_id, status, goal, scope, agent_depth, step,
                                     exit_requested, messages_json, todos_json, result, metrics_json,
                                     created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(run_id) DO UPDATE SET
                     owner_id=excluded.owner_id, status=excluded.status, goal=excluded.goal,
                     scope=excluded.scope, agent_depth=excluded.agent_depth, step=excluded.step,
                     exit_requested=excluded.exit_requested, messages_json=excluded.messages_json,
                     todos_json=excluded.todos_json, result=excluded.result,
                     metrics_json=excluded.metrics_json, updated_at=excluded.updated_at""",
                [run_id, owner, status, goal, scope, agent_depth, step,
                 1 if exit_requested else 0, json.dumps(messages), json.dumps(todos or []),
                 result, json.dumps(metrics or {}), now, now])
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def load_run(self, run_id: str, owner: str):
        """The full durable state of a run for this owner, or None. Returns messages/todos/metrics
        already decoded from JSON."""
        row = self._conn().execute(
            """SELECT status, goal, scope, agent_depth, step, exit_requested, messages_json,
                      todos_json, result, metrics_json, created_at, updated_at, pid, log_path
               FROM runs WHERE run_id=? AND owner_id=?""",
            [run_id, owner or self._default_owner]).fetchone()
        if not row:
            return None
        return {"run_id": run_id, "owner": owner or self._default_owner, "status": row[0],
                "goal": row[1], "scope": row[2], "agent_depth": row[3], "step": row[4],
                "exit_requested": bool(row[5]), "messages": json.loads(row[6]),
                "todos": json.loads(row[7] or "[]"), "result": row[8],
                "metrics": json.loads(row[9] or "{}"), "created_at": row[10], "updated_at": row[11],
                "pid": row[12], "log_path": row[13]}

    def set_run_process(self, run_id: str, owner: str, pid: int, log_path: str) -> None:
        """Record the detached worker's pid + log path for a task (P2 supervision)."""
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE runs SET pid=?, log_path=?, updated_at=? WHERE run_id=? AND owner_id=?",
                [pid, log_path, _now(), run_id, owner or self._default_owner])
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def set_status(self, run_id: str, owner: str, status: str, result: str = None) -> None:
        """Mark a run's terminal (or intermediate) status; optionally record its final result."""
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            if result is None:
                conn.execute("UPDATE runs SET status=?, updated_at=? WHERE run_id=? AND owner_id=?",
                             [status, _now(), run_id, owner or self._default_owner])
            else:
                conn.execute(
                    "UPDATE runs SET status=?, result=?, updated_at=? WHERE run_id=? AND owner_id=?",
                    [status, result, _now(), run_id, owner or self._default_owner])
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def list_runs(self, owner: str) -> list:
        """Summaries of this owner's runs, most-recently-updated first."""
        rows = self._conn().execute(
            """SELECT run_id, status, goal, step, created_at, updated_at, pid, log_path
               FROM runs WHERE owner_id=? ORDER BY updated_at DESC""",
            [owner or self._default_owner]).fetchall()
        return [{"run_id": r[0], "status": r[1], "goal": r[2], "step": r[3],
                 "created_at": r[4], "updated_at": r[5], "pid": r[6], "log_path": r[7]} for r in rows]

    def acquire_lease(self, run_id: str, owner: str, holder: str, ttl_seconds: int = 3600) -> bool:
        """Take (or renew) the run's lease for `holder`. Returns False if a *different* holder still has an
        unexpired lease -- the guard against two processes resuming the same run and double-executing.
        Returns False for a nonexistent run."""
        owner = owner or self._default_owner
        now = datetime.now(timezone.utc)
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT lease_holder, lease_expires FROM runs WHERE run_id=? AND owner_id=?",
                [run_id, owner]).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return False
            cur_holder, cur_expires = row
            if cur_holder and cur_holder != holder and cur_expires:
                try:
                    live = datetime.fromisoformat(cur_expires) > now
                except ValueError:
                    live = False
                if live:
                    conn.execute("COMMIT")
                    return False
            expires = (now + timedelta(seconds=ttl_seconds)).isoformat()
            conn.execute(
                "UPDATE runs SET lease_holder=?, lease_expires=?, updated_at=? WHERE run_id=? AND owner_id=?",
                [holder, expires, now.isoformat(), run_id, owner])
            conn.execute("COMMIT")
            return True
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def release_lease(self, run_id: str, owner: str, holder: str) -> None:
        """Release the run's lease if `holder` still owns it (a no-op otherwise)."""
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE runs SET lease_holder=NULL, lease_expires=NULL, updated_at=? "
                "WHERE run_id=? AND owner_id=? AND lease_holder=?",
                [_now(), run_id, owner or self._default_owner, holder])
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # --- snapshot ---------------------------------------------------------------------------------
    def snapshot(self, run_id: str, step: int, owner: str, paths, prefer_git: bool = True) -> bool:
        """Snapshot `paths` before a mutating step. Idempotent per (run_id, step): a second call is a
        no-op. Uses the git backend when every path is in one work-tree and prefer_git is set; else the
        shadow store. Returns True if a checkpoint was written."""
        owner = owner or self._default_owner
        if self.has(run_id, step):
            return False
        paths = [Path(p) for p in paths if p]
        if not paths:
            return False

        root = self._common_git_root(paths) if prefer_git else None
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            if root is not None:
                ref = self._git_stash_create(root)
                if ref:
                    files = [str(p.resolve()) for p in paths]
                    conn.execute(
                        "INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?)",
                        [run_id, step, owner, "git", ref, json.dumps(files), _now()])
                    conn.execute("COMMIT")
                    return True
            # shadow fallback (also the path for non-git targets)
            entries = self._shadow_store(paths)
            conn.execute(
                "INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?)",
                [run_id, step, owner, "shadow", None, json.dumps(entries), _now()])
            conn.execute("COMMIT")
            return True
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def _common_git_root(self, paths):
        roots = {in_git_repo(p) for p in paths}
        roots.discard(None)
        return next(iter(roots)) if len(roots) == 1 else None

    def _shadow_store(self, paths) -> list:
        entries = []
        for p in paths:
            rp = p.resolve()
            if rp.exists() and rp.is_file():
                data = rp.read_bytes()
                h = _sha(data)
                blob = self.shadow_dir / h
                if not blob.exists():
                    blob.write_bytes(data)
                entries.append({"path": str(rp), "hash": h, "existed": True})
            else:
                # file does not exist yet (the step will create it) -> rewind should delete it
                entries.append({"path": str(rp), "hash": None, "existed": False})
        return entries

    def _git_stash_create(self, root: Path):
        try:
            r = subprocess.run(["git", "-C", str(root), "stash", "create"],
                               capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        sha = r.stdout.strip()
        return sha or None   # empty when the tree is clean (nothing to snapshot)

    # --- rewind -----------------------------------------------------------------------------------
    def get(self, run_id: str, step: int, owner: str):
        row = self._conn().execute(
            "SELECT kind, ref, files_json FROM checkpoints WHERE run_id=? AND step=? AND owner_id=?",
            [run_id, step, owner or self._default_owner]).fetchone()
        if not row:
            return None
        return {"kind": row[0], "ref": row[1], "files": json.loads(row[2])}

    def steps(self, run_id: str, owner: str) -> list:
        rows = self._conn().execute(
            "SELECT step FROM checkpoints WHERE run_id=? AND owner_id=? ORDER BY step",
            [run_id, owner or self._default_owner]).fetchall()
        return [r[0] for r in rows]

    def restore(self, run_id: str, step: int, owner: str) -> int:
        """Restore the working tree to its state before `step`. Returns the number of files restored.
        Raises KeyError if there is no such checkpoint for this owner."""
        cp = self.get(run_id, step, owner)
        if cp is None:
            raise KeyError(f"no checkpoint for run {run_id} step {step}")
        if cp["kind"] == "git":
            return self._restore_git(cp)
        return self._restore_shadow(cp)

    def _restore_shadow(self, cp) -> int:
        n = 0
        for e in cp["files"]:
            target = Path(e["path"])
            if e.get("existed"):
                blob = self.shadow_dir / e["hash"]
                if blob.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(blob.read_bytes())
                    n += 1
            else:
                if target.exists():           # the step created it -> undo by removing it
                    target.unlink()
                    n += 1
        return n

    def _restore_git(self, cp) -> int:
        files = cp["files"]
        if not files:
            return 0
        root = in_git_repo(Path(files[0]))
        if root is None:
            raise RuntimeError("git checkpoint target is no longer in a git work-tree")
        n = 0
        for f in files:
            r = subprocess.run(["git", "-C", str(root), "checkout", cp["ref"], "--", f],
                               capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                n += 1
        return n
