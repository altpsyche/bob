#!/usr/bin/env python3
"""Stage the prebuilt engine the DEFAULT install path would download, so CI can run it in a clean container.

Resolves the row exactly as lifecycle.ensure_engine does (the newest ready release's engines.json, behind the
commit-match guard against this checkout's llama.cpp pin), then downloads, SHA-verifies and stages it with
lifecycle._install_prebuilt into <out_dir>. When no row matches (this checkout pins a llama.cpp commit no
release has shipped yet) the default install builds from source, so there is nothing prebuilt to test: it
says so and exits 0 with found=false.

  PYTHONPATH=scripts python .github/scripts/fetch_default_engine.py <out_dir> [--tier cpu]
  -> prints found=true|false (also appended to $GITHUB_OUTPUT when set)
"""
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import osenv  # noqa: E402
from bob import lifecycle  # noqa: E402


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    out = Path(argv[0])
    tier = argv[argv.index("--tier") + 1] if "--tier" in argv else "cpu"
    row = lifecycle._select_engine_row("llama-server", osenv.os_name(), osenv.normalized_cpu_arch(), tier)
    found = row is not None
    if found:
        print(lifecycle._install_prebuilt(row, out))
        print(f"row: {row.get('url')} (builtFromCommit {row.get('builtFromCommit')})")
    else:
        print(f"::notice::no published {tier} engine matches this checkout's llama.cpp pin; the default "
              "install builds from source here (acceptance-cpu covers that path)")
    line = f"found={'true' if found else 'false'}"
    print(line)
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
