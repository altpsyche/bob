#!/usr/bin/env python3
"""Package a staged engine directory into the release archive, on every OS, with one format.

One packer for the linux and windows publish jobs (and unit-tested in tests/test_pack_engine.py), so
the asset layout, the compression and the manifest row's size/sha can never drift between them.

Format: `.tar.xz`. xz over gzip is worth ~25% on CUDA fat binaries, which is pure download time for
every user, and Python's tarfile reads it with no extra dependency, so the client side (which is
stdlib-only, pre-venv) needs nothing new. zip is gone: Compress-Archive's deflate was the weakest of
the three and it was a second, Windows-only path to keep honest.

Symlinks are stored as symlinks (a SONAME link costs a tar header, not a second copy of a 500 MB lib).

  python .github/scripts/pack_engine.py dist/<name> dist/<name>.tar.xz
  -> {"path": ..., "bytes": <archive size>, "sha256": ..., "files": N}  (JSON on stdout)
"""
import hashlib
import json
import lzma
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

PRESET = 6   # xz -6: within ~1% of -9 on these binaries, at a fraction of the memory and time


def _tar(staging: Path, tar_path: Path) -> int:
    """Uncompressed tar of `staging`, with the directory itself as the archive's top-level entry
    (so an extraction yields <name>/llama-server, matching what lifecycle._install_prebuilt walks)."""
    count = 0
    with tarfile.open(tar_path, "w") as t:
        for p in sorted(staging.rglob("*")):
            t.add(p, arcname=str(Path(staging.name) / p.relative_to(staging)), recursive=False)
            count += 1
    return count


def _compress(tar_path: Path, out: Path) -> None:
    """xz the tar. Uses the xz CLI with -T0 when present (multi-threaded: ~1 minute instead of ~10 on
    a GB-scale CUDA payload), else Python's single-threaded lzma so the packer still works anywhere."""
    if shutil.which("xz"):
        with open(out, "wb") as fh:
            subprocess.run(["xz", "-T0", f"-{PRESET}", "-c", str(tar_path)], stdout=fh, check=True)
        return
    with open(tar_path, "rb") as src, lzma.open(out, "wb", preset=PRESET) as dst:
        shutil.copyfileobj(src, dst, length=1 << 20)


def pack(staging: str, out: str) -> dict:
    """Package `staging` into `out` (.tar.xz). Returns the manifest facts: path, bytes, sha256, files."""
    staging, out = Path(staging), Path(out)
    if not staging.is_dir():
        raise SystemExit(f"pack_engine: not a directory: {staging}")
    out.parent.mkdir(parents=True, exist_ok=True)
    tar_path = out.with_suffix("")          # <name>.tar
    files = _tar(staging, tar_path)
    try:
        _compress(tar_path, out)
    finally:
        tar_path.unlink(missing_ok=True)
    h = hashlib.sha256()
    with open(out, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return {"path": str(out), "bytes": os.path.getsize(out), "sha256": h.hexdigest(), "files": files}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    print(json.dumps(pack(argv[0], argv[1])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
