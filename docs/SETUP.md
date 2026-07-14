# SETUP

Bob installs and runs on **Windows and Linux** from one command per OS. This page is
the "how to install" guide for both; [PORTABILITY.md](PORTABILITY.md) is the "how the split works"
reference (portable runtime + cross-platform provisioner), and [MANUAL-INSTALL.md](MANUAL-INSTALL.md) is
the by-hand path for advanced users / debugging a partial install. The exact steps below are the same
ones the [CI acceptance matrix](../.github/workflows/ci.yml) runs on a fresh Ubuntu **and** Windows
runner on every change, so "clean machine → these steps → working Bob" is continuously proven.

## Hardware

The verified configuration is an NVIDIA RTX 5080 (16 GB VRAM, Blackwell sm_120), Ryzen 9 7950X3D, 64 GB
RAM. RTX 4000-series (Ada) and RTX 3000-series (Ampere) are supported with the same scripts; setup
detects the GPU and adapts the build. Profiles for 16/12/8 GB VRAM (up to 32 GB) are included. A
**CPU / no-GPU tier** (`bob profile cpu` + `bob build --cpu`, one tiny model) exists for CI and GPU-less
dev boxes, correctness/wiring only, not performance. See the [Supported matrix](../README.md#supported-matrix)
for exactly what each OS × GPU combination is tested to do. macOS and AMD/ROCm are not yet supported.

## Install

One command per OS. Only **git** is needed up front (the installer installs git too if it is missing).
Add `--cpu` on a GPU-less box.

<table>
<tr><th>Linux (glibc; apt/dnf/pacman/zypper; atomic Fedora via rpm-ostree)</th><th>Windows 11 (NVIDIA)</th></tr>
<tr><td>

```bash
curl -fsSL https://raw.githubusercontent.com/altpsyche/bob/main/install/install.sh | sh
```

Asks for `sudo` once for system packages. On atomic Fedora (Bazzite/Silverblue) it layers via
`rpm-ostree` and recommends a Fedora distrobox, see [MANUAL-INSTALL.md](MANUAL-INSTALL.md).

</td><td>

```powershell
irm https://raw.githubusercontent.com/altpsyche/bob/main/install/install.ps1 | iex
```

</td></tr>
</table>

The one command ensures git, clones with submodules into `~/bob` (Windows `%USERPROFILE%\bob`) or
fast-forwards an existing clone, runs the prereq step, runs setup, then runs
`python -m bob.kernel verify-install` (checks installed submodules + model SHAs against
[`versions.lock`](../versions.lock)). It is **idempotent**: re-run it any time and completed steps are
skipped. macOS is out of scope until 2.0.

(The short `https://get.bob.sh/install.sh` and `https://get.bob.sh/install.ps1` forms are the planned
short URLs once the domain fronts these files; use the `raw.githubusercontent.com` URLs above today.)

### Manual install (full control / fallback)

For full control, or to debug a partial install, clone and run the two steps by hand. The entry scripts
are thin shell stubs: the Linux `.sh` ensures a system `python3`, the Windows `.bat` requires Python, and
both hand off to the Python cold-start kernel (`python -m bob.kernel`).

<table>
<tr><th>Linux</th><th>Windows 11</th></tr>
<tr><td>

Prereqs you provide first: **git**. (`install_prereqs.sh` ensures `python3` and installs the toolchain
via your package manager.)

```bash
git clone --recurse-submodules https://github.com/altpsyche/bob.git ~/bob
cd ~/bob
./install_prereqs.sh       # compiler, cmake, ninja, go, node, python3, CUDA (add --cpu to skip CUDA)
./setup.sh                 # build, venvs, models, wire clients
bob                        # inference auto-starts; or `bob chat "hi"`
```

</td><td>

Prereqs you provide first: **Git**, **Python 3.12** (`winget install Python.Python.3.12`), and **VS2022**
with the *Desktop development with C++* workload (for the CUDA build).

```bat
git clone --recurse-submodules https://github.com/altpsyche/bob.git C:\bob
cd C:\bob
install_prereqs.bat        :: Node, uv, Go, CUDA, cmake
setup.bat                  :: build, venvs, models, wire clients
bob                        :: inference auto-starts
```

</td></tr>
</table>

Both entry scripts print the Bob release they belong to (`VERSION`) at startup, install *from
[`versions.lock`](../versions.lock)* (pinned + checksum-verified), and are **idempotent**: if something
fails partway, fix it and re-run; completed steps are skipped. Common flags (same on both):

- `--skip-models`: build + configure but skip the model downloads
- `--skip-build`: skip the llama.cpp/llama-swap compile (use an existing `bin/`)
- `--skip-voice`: skip the voice + vision step (whisper build + model downloads)
- `--profile 12gb` / `--profile cpu`: pick a model profile before downloading anything
- `--cpu`: force the CPU build tier (skip CUDA)
- `--with-webui`: also build the Open WebUI venv (opt-in; multi-GB torch/transformers)
- `--launch`: start the stack when setup finishes

`setup` needs **no root**: only `install_prereqs` (system packages) uses sudo. After setup, open a new
terminal to pick up the PATH change, then just run `bob` (inference auto-starts on demand, no separate
`bob up` needed; `bob up` remains an optional pre-warm). On a GPU-less box `bob profile auto` selects the
`cpu` tier automatically. Verify with `bob doctor` (see [Verifying the install](#verifying-the-install)).

## What setup does, step by step

`setup` runs `python -m bob.kernel setup`, which imports the same capability functions the agent and
`bob --run` use (one code path) and runs these steps in order:

0. **Diagnose**: a machine summary (GPU, VRAM, RAM, CUDA, NUMA topology, mlock privilege, active profile, model files) before anything is installed. Run `bob diagnose` at any time to see the same report.
1. `git submodule update --init --recursive` fetches the llama.cpp and llama-swap source trees.
2. **Build llama.cpp** (`build.build_llama`), compiles the CUDA engine, or the CPU tier with `--cpu` / when no CUDA toolkit is found, and writes the binaries to `bin/`. Skips if the binary already exists (`bob build --force` to rebuild). Before replacing a binary it backs it up as `bin/<name>.bak`; `bob update` snapshots `bin/` before a rebuild and rolls back automatically if the new build fails to verify.
3. **Build llama-swap**: the model-swap proxy (Go).
4. **Python venvs**: `tools/venv-aider` and `tools/venv-litellm` (plus `tools/venv-webui` with `--with-webui`) are created via `osenv.new_bob_venv` and their deps installed. Kept separate on purpose, their pins conflict. (`venv-eval` is provisioned lazily by `bob eval`.)
5. **Generate configs** (`generate.gen_all`), writes `config/llama-swap.yaml` + `config/litellm.yaml` from `config/models.json`. Never edit them by hand; both are regenerated on every `bob up`/`serve`.
6. **Fetch models** (`provision.fetch_models`), downloads the active profile's GGUFs (resume + SHA256-verify vs `versions.lock`).
7. **Wire clients**: symlinks `config/continue/config.yaml` to `~/.continue/config.yaml` and checks VS Code extension status.
8. **fabric**: builds the fabric CLI (Go) and points it at the local endpoint.
9. **Install the `bob` CLI**: symlinks `./bob` into `~/.local/bin` (POSIX) or a `bob.cmd` shim into scoop\shims (Windows).
10. **Memory lock:** reports the mlock privilege status. On Linux it prints the `ulimit`/`limits.conf` guidance (mlock is an rlimit, not a grantable privilege); on Windows, if `mlockBig` is enabled in `config/user.json` it grants `SeLockMemoryPrivilege` (UAC). Open a new terminal afterward for it to take effect.
11. **Optional services:** prints the opt-in service info (n8n, SearXNG, Langfuse) and installs nothing. A default install is 100% Docker-free; services start on demand, not at setup. See [Optional services](#optional-services).
12. **Onboarding**: a first-run profile prompt (name / work / optional DeepSeek key) when `config/user.json` has no `bob` section; skipped on a non-interactive run.

After setup, run `bob agent install` once to register the recurring background-agent runner (Linux cron / Windows Scheduled Task), separate from setup because it references the final install location. `bob agent status` confirms it.

To pin llama.cpp to a specific commit or bump to a newer version, see [MANUAL-INSTALL.md § 4](MANUAL-INSTALL.md#4-build-llamacpp) and [TUNING.md](TUNING.md#bumping-the-llamacpp-submodule).

## Optional services

A default install is **100% Docker-free** and needs no services for core inference. These extend the
stack with automation, private web search, and observability. Each is opt-in and starts on demand, never
at setup. Start one with `bob services <name> start`; the Docker-backed ones run a guided Docker install
(via the same apt/dnf/pacman/zypper/rpm-ostree/winget package seam) if Docker is missing, then bring the
service up.

| Service | Port | Runs as | What it does | Why you'd want it |
|---|---|---|---|---|
| **n8n** | 5678 | Native (Node) | Visual workflow automation (like Zapier, local): chains bob calls, webhooks, and APIs | Automate tasks without scripts: summarize PRs on open, generate commit messages, run daily digests |
| **SearXNG** | 8888 | Docker | Self-hosted meta-search (queries Google/Bing without sending your searches to the cloud) | Backs the `searxng-search` MCP so Continue.dev `@web` gets self-hosted search results |
| **Langfuse** | 3001 | Docker | bob observability: every prompt, completion, latency, and token count in a dashboard (plus its own Postgres) | Debug unexpected model output; compare quant levels; trace exactly what aider/Cline sends |

Web search for the agent and CLI does **not** need any of these: the default in-process `ddgs` metasearch
provider (pure Python, no service, no daemon, no Docker) works identically on every OS out of the box.
Optional providers are Brave/Tavily via API key (`agent.searchProvider`) or the opt-in `searxng` service;
all fall back to `ddgs`.

Tracing is Docker-free too: the default trace sink is a local file sink, writing spans to
`logs/traces/<trace_id>.jsonl`, viewed with `bob traces` (`bob traces list`, `bob traces show <id>`).
`agent.tracing` gates tracing (off by default) and `agent.tracingSink` picks `file` (default) or `otlp`;
`otlp` exports to `agent.otlpEndpoint` (for example an opted-in Langfuse). Langfuse is not required for
observability.

Start each service on demand:
```bash
bob services n8n start        # native, no Docker
bob services searxng start    # Docker; guided Docker install if missing
bob services langfuse start   # Docker; guided Docker install if missing
```

> **If you opt into a Docker service on Windows:** if Docker Desktop was just installed, log out and back
> in first. Then in Docker Desktop → Settings → General → uncheck **"Use containerd for pulling and
> storing images"** → Apply & Restart. If that setting is on, SearXNG fails with
> `exec /bin/sh: exec format error`. Only needs to be changed once.

Check status:
```bash
bob services status
```

URLs once a service is up:
- n8n: http://localhost:5678
- SearXNG: http://localhost:8888
- Langfuse: http://localhost:3001 (login: `admin@local.dev` / `admin123`)

Day-to-day management: `bob services status|start|stop|logs`, or per-service `bob services <name> start`.

For a detailed walkthrough of what the Docker-backed services do internally, including troubleshooting, see [MANUAL-INSTALL.md § Docker services](MANUAL-INSTALL.md#12-docker-services).

## Verifying the install

```bash
bob up                    # starts llama-swap (:8080) + LiteLLM proxy (:8081)  (+ Open WebUI :3000 if set up with --with-webui)
bob models                # should list: planner, coder, chat, fim, embed, vision, agent
bob bench                 # performance check (see expected numbers below)
bob chat coder "hi"       # end-to-end sanity check (routes via :8081 LiteLLM proxy)
bob diagnose              # re-run hardware summary at any time; flags any unresolved issues
bob doctor           # full pre-flight: deps + endpoint, GPU/VRAM, writable dirs, config parse, reproducibility
bob version          # the installed release + component versions (llama-swap, llama-server, submodule commits)
bob plugins list     # should show: summarise, draft, search, play (built-in plugins)
```

**Agent system:** `bob doctor` (superset of `bob setup check`) validates all agent dependencies (the Hermes 3 model file, tool loading, scheduled task registration) plus a runtime pre-flight (endpoint, GPU/VRAM, writable `logs/`+`data/`, `config.json` parses) and a **reproducibility** block (installed submodule commits + present-model checksums vs [`versions.lock`](../versions.lock)). If any check fails, it prints the exact fix command. Run `bob setup check` (or `bob doctor --quick`) for just the dependency subset.

**Pro models** (optional): set `DEEPSEEK_API_KEY` and `ZHIPU_API_KEY` environment variables, then run `bob gen`. The pro models (`chat-pro`, `planner-pro`, `coder-pro`) will be available via the LiteLLM proxy at `:8081`. See [USAGE.md § Pro models](USAGE.md#pro-models-api-backed-no-platform-fee).

**Voice and Vision (Phase 2):** included in `setup` automatically (step: builds whisper, downloads STT model, piper TTS, and vision mmproj). To skip: `./setup.sh --skip-voice`. See [USAGE.md § Voice](USAGE.md#voice-phase-2) and [USAGE.md § Vision](USAGE.md#vision-phase-2).

**Memory lock** is handled automatically during setup (step 10). If you enable `mlockBig: true` in `config/user.json` after setup, run `bob mlock` to grant `SeLockMemoryPrivilege` and restart your terminal.

On an RTX 5080 with the 14B Q4 coder model, expected numbers are **pp512 ≈ 4600 t/s, tg128 ≈ 89 t/s**. These confirm the engine is on the fast Blackwell hardware path. Ada and Ampere cards will show lower numbers; what matters is that prefill is not disproportionately slow relative to generation (see [TUNING.md](TUNING.md#verifying-the-fast-path)).

If prefill throughput is around 1000 t/s rather than 4000+, the build is using a slower fallback, most likely because it was compiled against CUDA 13.x or has a stale build cache. Fix this by running `bob build --force`, which wipes the build directory and recompiles from scratch. Make sure CUDA 12.8 is the active toolkit when you do.
