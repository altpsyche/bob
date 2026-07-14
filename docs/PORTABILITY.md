# Portability & config resolution

Bob is one Python runtime that boots and runs the same on every supported OS. Portability comes from
three pieces:

- **`scripts/osenv.py`**: the OS seam. Every OS-specific behavior (data dir, secrets, package
  install, notifications, process teardown, shell) funnels through here, so the rest of the code
  stays OS-agnostic.
- **`scripts/bob_config.py`**: the Python config resolver. Builds the runtime config from
  neutral JSON at startup; no generated cache is required off Windows.
- **`scripts/bob/kernel.py`**: the cold-start provisioner. Installs prerequisites, builds the
  engine, creates venvs, generates configs, downloads models, and wires clients. The two shell stubs
  (`setup.sh` / `setup.bat`) just ensure `python3` is present and hand off to `python -m bob.kernel`.

## Config resolution (all JSON)

Bob's runtime config is resolved live, per OS, from neutral sources:

- **`config/defaults.json`**: the neutral single source of truth: `ports`, `roleTable`, and
  the `runtime.*` defaults (persona, memory, vision, voice, agent). Both the Python runtime and the
  provisioner read it.
- **`config/models.json`**: the model registry (roles, files, VRAM, SHA256, profiles).
- **`config/user.json`**: *your* override, in the runtime-config shape, e.g.
  `{"agent": {"maxSteps": 8}}` or `{"peers": {"deepseek": {"apiKey": "…"}}}`. This is the documented
  authoring surface on every OS. (A `config/user.toml` is also accepted on Python 3.11+.)

`bob_core.load_config()` resolves the config the same way on every OS: live from `defaults.json`
deep-merged with `user.json`. Command dispatch and help both come from
`scripts/bob/registry.py`, the single source.

Secrets never live in a tracked file. They resolve through the seam `osenv.secret(name)` with
precedence **env var → OS keychain → `data/secrets.json` → config default**. See
[SECURITY.md](SECURITY.md).

State and data (`sessions.db`, `bob.db`, `schedules.json`, `logs/`) default to the repo-relative
`data/` and `logs/` dirs on every OS; set `BOB_DATA_DIR` to relocate them (with a
one-time migration).

## Point Bob at any OpenAI-compatible endpoint

Bob only needs a reachable OpenAI-compatible chat endpoint on `litellmPort` (default `8081`). The
provisioner runs llama-swap + LiteLLM locally, but you can bring your own: point `litellmPort` at any
running OpenAI-compatible server and skip the local inference stack.

Drop a `config/user.json`:

```json
{ "litellmPort": 8081 }
```

Set the base URL your clients call to `http://localhost:8081/v1` (or wherever your endpoint lives,
the port is the seam). The master key resolves through the secret seam (`litellmKey`, default
`sk-local`), so set it via env or keychain rather than a tracked file. At startup Bob **probes** the
endpoint (`bob_core.capability_probe`) and degrades with a clear message if it is unreachable,
rather than assuming a particular provisioner ran.

The agent core (`bob agent`, `bob agent serve`, `bob agent mcp`) needs nothing more than a resolvable
config and a reachable endpoint, no local build required.

## Supported OS / package-manager matrix

Bob is honest about what is tested versus what is expected to work. See the README's
[supported matrix](../README.md#supported-matrix) for the authoritative gated/supported table; the
short version:

| OS | Status | Package managers |
|----|--------|------------------|
| **Linux** (glibc) | gated on the CPU tier every PR; NVIDIA CUDA proven in the release-tag GPU tier | `apt`, `dnf`, `pacman`, `zypper`, plus **`rpm-ostree`** for atomic Fedora (Bazzite/Silverblue) |
| **Windows 11** | gated on the CPU tier every PR; NVIDIA CUDA proven in the release-tag GPU tier | `scoop` shim for the `bob` command; toolchain via `install_prereqs.bat` |
| **macOS** | not yet | n/a |
| **AMD / ROCm** | not yet | n/a |

Package installation goes through `osenv` (`PACKAGE_MAP` / `resolve_package_*` / `install_package`),
which selects the right manager for the host. On atomic Fedora the toolchain is layered via
`rpm-ostree` and the installer recommends a Fedora **distrobox** as the preferred path.

## Cold-start provisioner

`python -m bob.kernel` is the cross-OS cold-start path (it runs under the system `python3` before the
venvs exist):

```
python -m bob.kernel prereqs [--cpu]     # Tier 0, toolchain + a venv-compatible Python
python -m bob.kernel setup [flags]       # Tier 1, the fresh-machine orchestrator
python -m bob.kernel bootstrap [flags]   #          submodules -> build -> venvs -> gen -> fetch
python -m bob.kernel venv <name...>      #          create tools/venv-<name>
python -m bob.kernel build-swap          #          build the llama-swap proxy (Go)
```

Flags (kebab-case, identical on both OSes): `--skip-models`, `--skip-build`, `--skip-voice`,
`--launch`, `--with-webui`, `--cpu`, `--profile <name>`. `setup` needs no root; only Tier 0
prerequisites use one batched `sudo` (Linux). This page is the "how the pieces fit" reference;
**[SETUP.md](SETUP.md) is the "how to install" guide, and [MANUAL-INSTALL.md](MANUAL-INSTALL.md) is
the step-by-step for advanced users.**

## Reproducibility & releases

[`versions.lock`](../versions.lock) (neutral JSON, read on every OS) pins submodule commits, per-venv
requirements, minimum toolchain versions, and the model manifest (repo → revision → sha256). It is
generated from those sources (`bob lock`), staleness-gated in CI, and installs run *from the lock*,
each model download's checksum is verified, fail-loud on mismatch. `bob doctor` reports drift,
`bob version` reports the release, and `bob update` moves between releases and rolls the build output
back on a failed upgrade.

macOS/Metal and AMD/ROCm remain non-goals for now; `scripts/osenv.py` is where they slot in.
