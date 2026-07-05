# SETUP

Bob installs and runs on **Windows and Linux** from one documented, two-step path per OS. This page is
the "how to install" guide for both; [PORTABILITY.md](PORTABILITY.md) is the "how the split works"
reference (portable runtime + cross-platform provisioner), and [MANUAL-INSTALL.md](MANUAL-INSTALL.md) is
the by-hand path for advanced users / debugging a partial install. The exact steps below are the same
ones the [ND2 CI acceptance matrix](../.github/workflows/ci.yml) runs on a fresh Ubuntu **and** Windows
runner on every change, so "clean machine → these steps → working Bob" is continuously proven.

## Hardware

The verified configuration is an NVIDIA RTX 5080 (16 GB VRAM, Blackwell sm_120), Ryzen 9 7950X3D, 64 GB
RAM. RTX 4000-series (Ada) and RTX 3000-series (Ampere) are supported with the same scripts — setup
detects the GPU and adapts the build. Profiles for 16/12/8 GB VRAM (up to 32 GB) are included. A
**CPU / no-GPU tier** (`bob profile cpu` + `bob build --cpu`, one tiny model) exists for CI and GPU-less
dev boxes — correctness/wiring only, not performance. See the [Supported matrix](../README.md#supported-matrix)
for exactly what each OS × GPU combination is tested to do. macOS and AMD/ROCm are not yet supported.

## Install

Two commands per OS (a possible logout between them on Windows if Docker Desktop was just installed).
The Windows entry is `.bat`, the Linux entry is `.sh` — both are thin wrappers over the same OS-aware
`scripts/*.ps1` (PowerShell 7 runs on both). Add `--cpu` to `install_prereqs` on a GPU-less box.

<table>
<tr><th>Windows 11 (NVIDIA)</th><th>Linux (glibc; apt/dnf/pacman/zypper)</th></tr>
<tr><td>

Prereqs you provide first: **Git**, **Scoop** (`irm get.scoop.sh | iex`), **PowerShell 7**
(`winget install Microsoft.PowerShell`).

```powershell
git clone --recurse-submodules <your-remote> C:\bob
cd C:\bob
.\install_prereqs.bat      # Node, uv, Go, Python 3.12, CUDA, cmake, Docker
# (log out/in if Docker Desktop was just installed)
.\setup.bat                # build → venvs → models → wire clients
bob up
```

</td><td>

Prereqs you provide first: **git**, **curl**. `install_prereqs.sh` installs **PowerShell 7** and the
toolchain via your package manager.

```bash
git clone --recurse-submodules <your-remote> ~/bob
cd ~/bob
./install_prereqs.sh       # pwsh + compiler, cmake, ninja, go, node, python3 (add --cpu to skip CUDA)
./setup.sh                 # build → venvs → models → wire clients
bob up
```

</td></tr>
</table>

Both entry scripts print the Bob release they belong to (`VERSION`) at startup, install *from
[`versions.lock`](../versions.lock)* (pinned + checksum-verified), and are **idempotent** — if something
fails partway, fix it and re-run; completed steps are skipped. Common flags (same on both, `.bat`/`.sh`):

- `-SkipModels` — build + configure but skip the model downloads
- `-SkipVoice` — skip the voice + vision step (whisper build + model downloads)
- `-Profile 12gb` / `-Profile cpu` — pick a model profile before downloading anything
- `-Launch` — start the stack when setup finishes

After setup, open a new terminal to pick up the PATH change, then `bob up`. On a GPU-less box `bob profile
auto` selects the `cpu` tier automatically. Verify the install with `bob doctor` (see
[Verifying the install](#verifying-the-install)).

## What setup does, step by step

`setup.bat` calls `scripts\setup.ps1`, which runs these steps in order. You can run any of them individually if you need to redo a specific part:

0. `.\scripts\diagnose.ps1` prints a machine summary (GPU, VRAM, RAM, CUDA, NUMA topology, mlock privilege, active profile, model files) before anything is installed. Run `bob diagnose` at any time to see the same report.
1. `git submodule update --init --recursive` fetches the llama.cpp and llama-swap source trees.
2. `.\scripts\build-llama.ps1` compiles llama.cpp (CUDA, or `-Cpu` for the no-GPU tier) and writes the binaries to `bin/`. Skips if the binary already exists; pass `-Force` to rebuild from scratch. Before replacing an existing binary it backs it up as `bin/<name>.bak`. `bob update` generalizes this: it snapshots `bin/` before a rebuild and rolls back automatically if the new build fails to verify (ND3), so you rarely restore by hand.
3. `.\scripts\build-llama-swap.ps1` compiles the model-swap proxy.
4. Python virtual environments are created in `tools/venv-aider`, `tools/venv-webui`, `tools/venv-litellm`, and `tools/venv-eval` and their dependencies are installed. These are kept separate on purpose. Their dependency pins conflict and can't be merged into one environment.
5. `.\scripts\gen-llama-swap.ps1` generates `config/llama-swap.yaml` and `.\scripts\gen-litellm.ps1` generates `config/litellm.yaml` — both from `config/models.psd1`. Neither file should ever be edited by hand; both are regenerated on every `bob serve`.
6. `.\scripts\fetch-models.ps1` downloads GGUF model files for the active profile.
7. `.\scripts\setup-clients.ps1` symlinks `config/continue/config.yaml` to `~/.continue/config.yaml` and checks VS Code extension status.
8. `.\scripts\setup-fabric.ps1` installs the fabric CLI and configures it to use the local endpoint.
9. `.\scripts\install-cli.ps1` puts the `llm` command on PATH.
10. **Memory lock:** reads `config/user.psd1` to check if `mlockBig = $true` is set. If it is, runs `scripts\grant-mlock.ps1` automatically (UAC prompt; one-time per machine). If `mlockBig` is not set but ≥ 32 GB RAM is free, prints a tip on how to enable it. Otherwise reports why it was skipped. Open a new terminal after setup completes for the privilege to take effect.
11. `.\scripts\setup-docker.ps1` pulls and starts the Docker services stack (Langfuse, SearXNG, n8n). Runs automatically if Docker Desktop is installed; skipped gracefully if not.
12. **Agent scheduler:** `.\scripts\install-cli.ps1` also registers tab completions. After setup, run `bob agent install` once to register the `BobAgent` Windows Scheduled Task (runs background agent goals on cron schedules). This is separate from `setup.bat` because the scheduled task references the final install location. Run `bob agent status` to confirm it's registered.

To pin llama.cpp to a specific commit or bump to a newer version, see [MANUAL-INSTALL.md § 4](MANUAL-INSTALL.md#4-build-llamacpp) and [TUNING.md](TUNING.md#bumping-the-llamacpp-submodule).

**Docker services** (Langfuse, SearXNG, n8n) extend the stack with observability, private web search, and automation:

| Service | Port | What it does | Why you'd want it |
|---|---|---|---|
| **Langfuse** | 3001 | bob observability: every prompt, completion, latency, and token count in a dashboard | Debug unexpected model output; compare quant levels; trace exactly what aider/Cline sends |
| **SearXNG** | 8888 | Self-hosted meta-search (queries Google/Bing without sending your searches to the cloud) | Powers Continue.dev `@web`: type `@web <query>` in Continue chat and the model gets live search results |
| **n8n** | 5678 | Visual workflow automation (like Zapier, local): chains bob calls, webhooks, and APIs | Automate tasks without scripts: summarize PRs on open, generate commit messages, run daily digests |

None are required for core inference. They start automatically at the end of `setup.bat` if Docker Desktop is installed.

### Installing Docker services

Docker services start automatically at the end of `setup.bat`. No separate step needed.

> **Before `setup.bat` finishes:** Docker Desktop → Settings → General → uncheck
> **"Use containerd for pulling and storing images"** → Apply & Restart.
> If this setting is on, SearXNG fails with `exec /bin/sh: exec format error`. Only needs to be changed once.

Verify the containers started:
```powershell
bob services status
# Expected: four rows, all "Up": compose-langfuse-postgres-1, compose-langfuse-1, compose-searxng-1, compose-n8n-1
```

URLs after first run:
- Langfuse: http://localhost:3001 (login: `admin@local.dev` / `admin123`)
- SearXNG: http://localhost:8888
- n8n: http://localhost:5678

Day-to-day management: `bob services start|stop|status|logs`.

For a detailed walkthrough of what `setup-docker.ps1` does internally, including troubleshooting, see [MANUAL-INSTALL.md § Docker services](MANUAL-INSTALL.md#12-docker-services).

## Verifying the install

```powershell
bob up                    # starts llama-swap (:8080) + LiteLLM proxy (:8081)  (+ Open WebUI :3000 if set up with -WithWebui)
bob models                # should list: planner, coder, chat, fim, embed, vision, agent
bob bench                 # performance check (see expected numbers below)
bob chat coder "hi"       # end-to-end sanity check (routes via :8081 LiteLLM proxy)
bob diagnose              # re-run hardware summary at any time; flags any unresolved issues
bob doctor           # full pre-flight: deps + endpoint, GPU/VRAM, writable dirs, config parse, reproducibility
bob version          # the installed release + component versions (llama-swap, llama-server, submodule commits)
bob plugins list     # should show: summarise, draft, search, play (built-in plugins)
```

**Agent system:** `bob doctor` (superset of `bob setup check`) validates all agent dependencies — the Hermes 3 model file, tool loading, scheduled task registration — plus a runtime pre-flight (endpoint, GPU/VRAM, writable `logs/`+`data/`, `config.json` parses) and a **reproducibility** block (installed submodule commits + present-model checksums vs [`versions.lock`](../versions.lock)). If any check fails, it prints the exact fix command. Run `bob setup check` for just the dependency subset.

**Pro models** (optional): set `DEEPSEEK_API_KEY` and `ZHIPU_API_KEY` environment variables, then run `bob gen`. The pro models (`chat-pro`, `planner-pro`, `coder-pro`) will be available via the LiteLLM proxy at `:8081`. See [USAGE.md § Pro models](USAGE.md#pro-models-api-backed-no-platform-fee).

**Voice and Vision (Phase 2):** included in `setup.bat` automatically (step 11 — builds whisper, downloads STT model, piper TTS, and vision mmproj). To skip: `setup.bat -SkipVoice`. See [USAGE.md § Voice](USAGE.md#voice-phase-2) and [USAGE.md § Vision](USAGE.md#vision-phase-2).

**Memory lock** is handled automatically during setup (step 10). If you enable `mlockBig = $true` in `config/user.psd1` after setup, run `bob mlock` to grant `SeLockMemoryPrivilege` and restart your terminal.

On an RTX 5080 with the 14B Q4 coder model, expected numbers are **pp512 ≈ 4600 t/s, tg128 ≈ 89 t/s**. These confirm the engine is on the fast Blackwell hardware path. Ada and Ampere cards will show lower numbers; what matters is that prefill is not disproportionately slow relative to generation (see [TUNING.md](TUNING.md#verifying-the-fast-path)).

If prefill throughput is around 1000 t/s rather than 4000+, the build is using a slower fallback, most likely because it was compiled against CUDA 13.x or has a stale build cache. Fix this by running `scripts\build-llama.ps1 -Force`, which wipes the build directory and recompiles from scratch. Make sure CUDA 12.8 is the active toolkit when you do.
