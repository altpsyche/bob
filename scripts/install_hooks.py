#!/usr/bin/env python3
"""ONE-E — install Bob's git hooks (port of the retired install-hooks.ps1). Copies scripts/hooks/* into
.git/hooks (that dir isn't version-controlled) and marks them executable. Idempotent.

  python scripts/install_hooks.py
"""
import shutil
import stat
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def install_hooks() -> int:
    git_dir = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--git-dir"],
                             capture_output=True, text=True).stdout.strip()
    if not git_dir:
        print("not a git repository — nothing to install", file=sys.stderr)
        return 1
    hooks_dst = (REPO / git_dir / "hooks") if not Path(git_dir).is_absolute() else Path(git_dir) / "hooks"
    hooks_dst.mkdir(parents=True, exist_ok=True)
    src_dir = REPO / "scripts" / "hooks"
    n = 0
    for src in sorted(src_dir.iterdir()):
        if not src.is_file():
            continue
        dst = hooks_dst / src.name
        shutil.copyfile(src, dst)
        dst.chmod(dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        print(f"installed {src.name} -> {dst}")
        n += 1
    print(f"{n} hook(s) installed.")
    return 0


if __name__ == "__main__":
    sys.exit(install_hooks())
