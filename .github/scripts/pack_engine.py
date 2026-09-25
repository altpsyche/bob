#!/usr/bin/env python3
"""Package a staged engine directory into the release archive, on every OS, with one format.

One packer for the linux and windows publish jobs (and unit-tested in tests/test_pack_engine.py), so
the asset layout, the compression and the manifest row's size/sha can never drift between them.

Format: `.tar.xz`. xz over gzip is worth ~25% on CUDA fat binaries, which is pure download time for
every user, and Python's tarfile reads it with no extra dependency, so the client side (which is
stdlib-only, pre-venv) needs nothing new. zip is gone: Compress-Archive's deflate was the weakest of
the three and it was a second, Windows-only path to keep honest.

Symlinks are stored as symlinks (a SONAME link costs a tar header, not a second copy of a 500 MB lib).

It also emits the archive's engines.json row (engine_row), the one definition of the row shape that the
publish jobs write and tests/test_release_manifest.py resolves, so the two cannot drift.

  python .github/scripts/pack_engine.py dist/<name> dist/<name>.tar.xz
  -> {"path": ..., "bytes": <archive size>, "sha256": ..., "files": N}  (JSON on stdout)
  python .github/scripts/pack_engine.py row --os linux --tier cuda --asset dist/<name>.tar.xz \
      --repo <owner/repo> --tag <vX.Y.Z> --commit <llama.cpp sha> [--out rows/<name>.json]
  -> {"<name>": {row}}  (JSON on stdout, and to --out when given)
"""
import argparse
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

# The CUDA engines are one fat multi-arch binary (Turing..Blackwell) built against this CUDA major.
CUDA_ARCHS = "75;80;89;120"
CUDA_MAJOR = 12
# The build recipe the published rows were made with. The release pipeline reuses a prior release's
# engines only when its commit AND recipe match, so bump this whenever the build flags change what the
# binary is (2: portable CPU code, -DGGML_NATIVE=OFF, instead of the build runner's instruction set).
RECIPE = 2


def _tar(staging: Path, tar_path: Path) -> int:
    """Uncompressed tar of `staging`, with the directory itself as the archive's top-level entry
    (so an extraction yields <name>/llama-server, matching what lifecycle._install_prebuilt walks)."""
    count = 0
    with tarfile.open(tar_path, "w") as t:
        for p in sorted(staging.rglob("*")):
            t.add(p, arcname=str(Path(staging.name) / p.relative_to(staging)), recursive=False)
            count += 1
    return count


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def engine_name(os_name: str, tier: str, cpu_arch: str = "x86_64") -> str:
    """The asset/row key: llama-server-<os>-<arch>-<tier>."""
    return f"llama-server-{os_name}-{cpu_arch}-{tier}"


def engine_row(os_name: str, tier: str, repo: str, tag: str, commit: str, cpu_arch: str = "x86_64",
               asset=None, sha256: str = None, nbytes: int = None) -> dict:
    """The engines.json row for one published archive, as {name: row}. sha256/bytes come from `asset` (the
    packed .tar.xz) unless given explicitly. `tier` is the published name ("cuda" | "cpu"); the resolver
    maps its internal "gpu" onto "cuda"."""
    name = engine_name(os_name, tier, cpu_arch)
    if asset is not None:
        asset = Path(asset)
        sha256 = sha256 or _sha256(asset)
        nbytes = nbytes if nbytes is not None else os.path.getsize(asset)
    if not sha256 or nbytes is None:
        raise ValueError("engine_row needs an asset or explicit sha256 + nbytes")
    cuda = tier == "cuda"
    return {name: {
        "component": "llama-server", "os": os_name, "cpuArch": cpu_arch, "tier": tier,
        "url": f"https://github.com/{repo}/releases/download/{tag}/{name}.tar.xz",
        "sha256": sha256, "bytes": int(nbytes), "builtFromCommit": commit,
        "cudaArchs": CUDA_ARCHS if cuda else "",
        "cudaMajor": CUDA_MAJOR if cuda else None,
        "recipe": RECIPE,
    }}


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
    return {"path": str(out), "bytes": os.path.getsize(out), "sha256": _sha256(out), "files": files}


def _row_main(argv: list) -> int:
    p = argparse.ArgumentParser(prog="pack_engine.py row")
    p.add_argument("--os", required=True, dest="os_name")
    p.add_argument("--tier", required=True, choices=("cuda", "cpu"))
    p.add_argument("--arch", default="x86_64")
    p.add_argument("--asset", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--tag", required=True)
    p.add_argument("--commit", required=True)
    p.add_argument("--out")
    a = p.parse_args(argv)
    row = engine_row(a.os_name, a.tier, a.repo, a.tag, a.commit, cpu_arch=a.arch, asset=a.asset)
    text = json.dumps(row, indent=2) + "\n"
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "row":
        return _row_main(argv[1:])
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    print(json.dumps(pack(argv[0], argv[1])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
