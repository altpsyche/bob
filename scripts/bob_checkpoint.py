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
from datetime import datetime, timezone
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
    """SQLite-backed, owner-scoped snapshots keyed by (run_id, step)."""

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

    def has(self, run_id: str, step: int) -> bool:
        row = self._conn().execute(
            "SELECT 1 FROM checkpoints WHERE run_id=? AND step=?", [run_id, step]).fetchone()
        return row is not None

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
