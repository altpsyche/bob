# Manual Installation Guide

This guide walks through every step that `install_prereqs.bat` and `setup.bat` perform,
one command at a time. Use it when you want full control, are troubleshooting a failed
automated install, or want to understand what the scripts actually do.

**If you just want to get running quickly, use `install_prereqs.bat` then `setup.bat` instead.**
This document is for advanced users who prefer to drive each step manually.

The bulk of this guide (steps 3 to 13) is Windows-specific commands. On **Linux** the same steps run
under `pwsh` via the OS-aware seam ([PORTABILITY.md](PORTABILITY.md), Module NC). See the Linux
section below.

---

## 0. Linux (Module NC)

Bob's orchestration runs on Linux under PowerShell 7 (`pwsh`). Two thin bootstrappers mirror the
`.bat` entry points; the real build/serve logic is the same OS-aware `.ps1`.

```bash
# 1. Prerequisites: installs pwsh, then the toolchain (compiler, cmake, ninja, go, node, python3)
#    via apt/dnf/pacman/zypper. Add --cpu to skip the CUDA toolkit (CPU-only tier).
./install_prereqs.sh            # GPU box (expects NVIDIA driver + CUDA toolkit)
./install_prereqs.sh --cpu      # no GPU / CI runner

# 2. Build + configure: builds llama.cpp (CUDA, or CPU when no GPU), creates venvs, fetches models,
#    wires clients. Auto-selects the CPU build + 'cpu' profile when no GPU is detected.
./setup.sh                      # full
./setup.sh -Profile cpu         # force the tiny CPU profile
./setup.sh -SkipModels          # skip downloads

# 3. Run
pwsh scripts/up.ps1             # bring the stack up (nohup + pidfile)
./bob agent "say hi"            # or: pwsh scripts/bob.ps1 agent "say hi"
```

What differs from Windows (all behind [`scripts/_platform.ps1`](../scripts/_platform.ps1)):

| Concern | Windows | Linux |
|---|---|---|
| Package install | winget / scoop | apt / dnf / pacman / zypper (`Install-Package`) |
| Compiler / generator | MSVC + Visual Studio cmake | gcc/g++ + Ninja |
| CUDA toolkit | `C:\Program Files\NVIDIA…` | `/usr/local/cuda*` (`Get-CudaRoot`) |
| Services | hidden background process | `nohup` + pidfile (`Start-BobBackgroundProcess`) |
| Agent scheduler | Scheduled Task | crontab line (`Register-AgentTask`) |
| Secrets | env → `data/secrets.json` | env → `secret-tool` → `data/secrets.json` |
| Notifications | WinRT toast | `notify-send` |

CUDA toolkit install on Linux is distro-specific: `install_prereqs.sh` installs the toolchain and
flags CUDA as a manual step (or use `--cpu`). Verify a fresh install end-to-end with
`pwsh scripts/smoke.ps1 -Up` (the shared cross-OS smoke, formerly `smoke-linux.ps1`), the same gate the
[ND2 CI acceptance matrix](../.github/workflows/ci.yml) runs on Ubuntu and Windows on every change.

**Reproducibility (Module ND).** Whichever path you take, the install is pinned by
[`versions.lock`](../versions.lock): `fetch-models.ps1` downloads each model at its locked HF revision and
verifies the SHA256 (fail-loud on mismatch), and `bob doctor` reports any drift between the installed
submodules/models and the lock. After changing a submodule, regenerate the lock with `bob lock`
(`check.ps1` fails on a stale lock). See [PORTABILITY.md § Reproducibility](PORTABILITY.md#reproducibility--releases-module-nd).

Everything below is the Windows walkthrough.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Clone the repository](#2-clone-the-repository)
3. [Set up CUDA environment](#3-set-up-cuda-environment)
4. [Build llama.cpp](#4-build-llamacpp)
5. [Build llama-swap](#5-build-llama-swap)
6. [Create Python virtual environments](#6-create-python-virtual-environments)
7. [Generate the llama-swap config](#7-generate-the-llama-swap-config)
8. [Download models](#8-download-models)
9. [Wire VS Code clients](#9-wire-vs-code-clients)
10. [Build and configure fabric](#10-build-and-configure-fabric)
11. [Install the `llm` CLI command](#11-install-the-llm-cli-command)
12. [Docker services (Langfuse, SearXNG, n8n)](#12-docker-services)
13. [Verify the installation](#13-verify-the-installation)

---

## 1. Prerequisites

Install these in order. Each one is required before the next step can proceed.

### 1.1 PowerShell 7

```powershell
winget install Microsoft.PowerShell
```

Restart your terminal. Verify: `pwsh --version` should print `7.x.x`.

### 1.2 Git

Download from https://git-scm.com. During install, keep the default options.
Verify: `git --version`.

### 1.3 Scoop (package manager)

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
irm get.scoop.sh | iex
```

Verify: `scoop --version`.

### 1.4 VS2022 with Desktop C++ workload

Required to compile llama.cpp. Cannot be automated.

```powershell
winget install Microsoft.VisualStudio.2022.Community
```

After install, open **Visual Studio Installer** → **Modify** → check
**Desktop development with C++** → click **Modify**.

Verify: the C++ compiler exists at roughly
`C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Tools\MSVC\<ver>\bin\Hostx64\x64\cl.exe`.

### 1.5 Node.js

Required for Continue.dev MCP servers (`@filesystem`, `@url`, `@github`).

```powershell
winget install OpenJS.NodeJS --accept-package-agreements --accept-source-agreements
```

Verify: `node --version`.

### 1.6 uv / uvx

Required for `mcp-server-fetch` (the Continue `@url` provider runs via `uvx`).

```powershell
winget install astral-sh.uv --accept-package-agreements --accept-source-agreements
```

Verify: `uvx --version`.

### 1.7 Go

Required to build llama-swap and fabric.

```powershell
scoop install go
```

Verify: `go version`.

### 1.8 Python 3.12

**Must be exactly 3.12.** The venvs are pinned to it; Open WebUI has conflicts with 3.13+.

```powershell
scoop install python312
```

The scoop install makes `python3.12` available. Verify:
```powershell
scoop prefix python312     # prints the install path
python3.12 --version       # or: py -3.12 --version
```

### 1.9 CUDA Toolkit

#### Blackwell (RTX 5080, 5090): requires exactly CUDA 12.8

```powershell
winget install Nvidia.CUDA --version 12.8 --accept-package-agreements --accept-source-agreements
```

#### Ada Lovelace (RTX 4090, 4080, 4070 …) or Ampere (RTX 3090, 3080 …): any CUDA 12.x

```powershell
winget install Nvidia.CUDA --version 12.8 --accept-package-agreements --accept-source-agreements
```

CUDA 12.8 works on all three generations. After install, verify the toolkit exists:
```powershell
Test-Path "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.8"   # should be True
```

Restart your terminal after CUDA installs to pick up the new PATH entries.

### 1.10 cmake 3.x

**Do not use cmake 4.x**: it is explicitly excluded by llama.cpp's `CMakeLists.txt`.

Check first (VS2022 bundles cmake 3.31.x and that's sufficient):
```powershell
$vsI = & 'C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe' `
    -latest -products * -requires Microsoft.VisualStudio.Component.VC.CMake.Project `
    -property installationPath
Test-Path "$vsI\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
```

If that's not present, install via winget:
```powershell
winget install Kitware.CMake --version 3.31.7 --accept-package-agreements --accept-source-agreements
```

After install: `cmake --version`; confirm output starts with `cmake version 3.`.

### 1.11 Docker Desktop

Required for Langfuse, SearXNG, and n8n. Skip if you don't need those services.

```powershell
winget install Docker.DockerDesktop --accept-package-agreements --accept-source-agreements
```

**After install: log out of Windows and back in.** Docker Desktop adds your user to the
`docker-users` group and this only takes effect at login.

After logging back in, launch Docker Desktop from the Start menu and wait for the whale
icon in the system tray to go solid (60 to 90 seconds).

**Disable the containerd snapshotter** before pulling any images, otherwise SearXNG fails:
Docker Desktop → Settings → General → uncheck **"Use containerd for pulling and storing images"**
→ Apply & Restart.

---

## 2. Clone the repository

```powershell
git clone --recurse-submodules <your-remote> C:\bob
cd C:\bob
```

The `--recurse-submodules` flag fetches `external/llama.cpp`, `external/llama-swap`,
and `external/fabric` in one pass. If you cloned without it:

```powershell
git submodule update --init --recursive
```

Verify the submodules are populated:
```powershell
Test-Path external\llama.cpp\CMakeLists.txt   # True
Test-Path external\llama-swap\main.go         # True
Test-Path external\fabric\cmd\fabric\main.go  # True
```

---

## 3. Set up CUDA environment

The cmake build needs to find the CUDA toolkit. Set these environment variables for the
current session (and add them to your system environment if you want them permanent):

```powershell
$cudaRoot = "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.8"
$env:CUDA_PATH       = $cudaRoot
$env:CUDA_PATH_V12_8 = $cudaRoot
$env:PATH            = "$cudaRoot\bin;$env:PATH"
```

Verify nvcc is reachable: `nvcc --version`.

Identify your GPU's CUDA compute architecture; you need this for the cmake step:

```powershell
nvidia-smi --query-gpu=compute_cap --format=csv,noheader
# Returns something like: 12.0  (Blackwell), 8.9 (Ada), 8.6 (Ampere)
```

Convert to the cmake `CUDA_ARCHITECTURES` value (remove the dot):

| GPU generation | Example cards | `nvidia-smi` output | cmake value |
|---|---|---|---|
| Blackwell | RTX 5080, 5090 | `12.0` | `120` |
| Ada Lovelace | RTX 4090, 4080, 4070 Ti | `8.9` | `89` |
| Ada Lovelace (lower) | RTX 4070, 4060 Ti | `8.9` | `89` |
| Ampere | RTX 3090, 3080, 3070 | `8.6` | `86` |

---

## 4. Build llama.cpp

All commands run from `C:\bob` unless noted.

### 4.1 Locate cmake

Use the VS bundled cmake (3.31.x) to avoid version issues:

```powershell
$vswhere = 'C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe'
$vsPath  = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Workload.NativeDesktop -property installationPath
$cmake   = "$vsPath\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
# Verify:
& $cmake --version
```

If cmake is on your PATH and is version 3.x, you can use `cmake` directly.

### 4.2 Configure

Replace `120` with your GPU's compute architecture value from the table above.

```powershell
$cudaRoot = "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.8"
$arch     = "120"   # <-- set to your GPU value

cd external\llama.cpp

& $cmake -B build `
    -G "Visual Studio 17 2022" `
    -T "cuda=$cudaRoot" `
    -DGGML_CUDA=ON `
    -DCMAKE_CUDA_ARCHITECTURES="$arch" `
    -DGGML_CUDA_FORCE_CUBLAS=OFF `
    -DCUDAToolkit_ROOT="$cudaRoot"
```

Expected output ends with `-- Build files have been written to: .../external/llama.cpp/build`.

### 4.3 Build

```powershell
& $cmake --build build --config Release -j
```

This takes 5 to 20 minutes depending on your machine. Expected output ends with
`Build succeeded.`

### 4.4 Copy binaries and CUDA DLLs

```powershell
cd C:\bob

# Copy the server binary
Copy-Item external\llama.cpp\build\bin\Release\llama-server.exe bin\

# Copy required CUDA runtime DLLs (from CUDA toolkit)
$cuBin = "$cudaRoot\bin"
Copy-Item "$cuBin\cublas64_12.dll"   bin\
Copy-Item "$cuBin\cublasLt64_12.dll" bin\
Copy-Item "$cuBin\cudart64_12.dll"   bin\
```

Verify: `bin\llama-server.exe --version` should print version info without errors.

**MSVC compiler version note:** If cmake fails with `unsupported Microsoft Visual Studio version`,
add `-DCMAKE_CUDA_FLAGS="-allow-unsupported-compiler"` to the configure command, or install
MSVC v14.4x through the VS Installer (matches CUDA 12.8).

### 4.5 Pin to a specific commit

The repository records the exact llama.cpp commit verified to work on Blackwell. To switch to a different commit:

```powershell
cd external\llama.cpp
git checkout <commit-or-tag>
cd C:\bob
git add external/llama.cpp
git commit -m "pin llama.cpp to <commit>"
```

After changing the submodule commit, rebuild from scratch:

```powershell
.\scripts\build-llama.ps1 -Force
```

See [TUNING.md](TUNING.md#bumping-the-llamacpp-submodule) for bumping to a newer version and re-verifying performance afterward.

---

## 5. Build llama-swap

```powershell
cd C:\bob\external\llama-swap
go build -o ..\..\bin\llama-swap.exe .
cd C:\bob
```

Verify: `bin\llama-swap.exe --version`.

---

## 6. Create Python virtual environments

**All four venvs are required.** They are kept separate because Open WebUI, aider, LiteLLM,
and lm-eval have conflicting dependency pins that cannot coexist in a single environment.

First, resolve the Python 3.12 executable:

```powershell
# Try scoop shim first
$py = (Get-Command python3.12 -ErrorAction SilentlyContinue)?.Source
# Or use the py launcher:
if (-not $py) { $py = & py -3.12 -c "import sys; print(sys.executable)" }
Write-Host "Python 3.12: $py"
```

### 6.1 Open WebUI venv

```powershell
& $py -m venv tools\venv-webui
tools\venv-webui\Scripts\python.exe -m pip install --upgrade pip
tools\venv-webui\Scripts\python.exe -m pip install -r tools\webui-requirements.lock
```

### 6.2 aider venv

```powershell
& $py -m venv tools\venv-aider
tools\venv-aider\Scripts\python.exe -m pip install --upgrade pip
tools\venv-aider\Scripts\python.exe -m pip install -r tools\aider-requirements.lock
```

### 6.3 LiteLLM venv

```powershell
& $py -m venv tools\venv-litellm
tools\venv-litellm\Scripts\python.exe -m pip install --upgrade pip
tools\venv-litellm\Scripts\python.exe -m pip install -r tools\litellm-requirements.txt
```

### 6.4 Eval venv (optional, benchmarking only)

`venv-eval` (lm-eval + transformers) is **not** built by setup; it's provisioned on demand by the
first `bob eval` run, or explicitly with `pwsh scripts/bootstrap-eval.ps1` (both go through the shared
`New-BobVenv` helper, so you don't hand-roll the paths). To build it manually anyway:

```powershell
& $py -m venv tools\venv-eval
tools\venv-eval\Scripts\python.exe -m pip install --upgrade pip
tools\venv-eval\Scripts\python.exe -m pip install -r tools\eval-requirements.txt
```

Each venv install takes 2 to 10 minutes. `venv-webui` (Open WebUI) is the largest (torch/transformers,
multi-GB) and is **opt-in**: build it via `setup … -WithWebui` or `pwsh scripts/bootstrap.ps1 -WithWebui`.

---

## 7. Generate runtime configs

Two config files are generated from `config/models.psd1`. Both are overwritten on every
`bob serve` and `bob gen`; do not edit them by hand.

```powershell
.\scripts\gen-llama-swap.ps1   # writes config/llama-swap.yaml (local model routing)
.\scripts\gen-litellm.ps1      # writes config/litellm.yaml (LiteLLM proxy model list)
```

To target a specific VRAM profile for llama-swap (litellm.yaml is profile-agnostic):
```powershell
.\scripts\gen-llama-swap.ps1 12gb
.\scripts\gen-litellm.ps1
```

To customize model parameters or add API pro models, edit `config/models.psd1` or create
`config/user.psd1` (gitignored per-machine overrides). Re-run both generators after editing.

Verify:
```powershell
Test-Path config\llama-swap.yaml   # True
Test-Path config\litellm.yaml      # True
```

---

## 8. Download models

```powershell
.\scripts\fetch-models.ps1
```

This downloads all GGUF files for the active profile (~38 GB for 16gb, ~21 GB for 12gb).
Files go to `models/`. Downloads are resumable: if interrupted, re-run the same command.

Useful flags:

```powershell
# Preview what would be downloaded without downloading anything
.\scripts\fetch-models.ps1 -ListOnly

# Skip downloads entirely (if you'll provide models manually)
# (skip this step; copy .gguf files into models/ manually)

# Download for a specific profile without switching the active profile
.\scripts\fetch-models.ps1 -Profile 12gb
```

For gated HuggingFace repos, set `$env:HF_TOKEN` to your access token before running:
```powershell
$env:HF_TOKEN = "hf_..."
.\scripts\fetch-models.ps1
```

After download, verify SHA256 checksums:
```powershell
.\scripts\bob.ps1 verify-urls    # checks download URLs are still valid
```

---

## 9. Wire VS Code clients

### 9.1 Continue.dev config

Link the repo's Continue config into your home directory so Continue finds it automatically:

```powershell
# Create the directory if it doesn't exist
New-Item -ItemType Directory -Force "$HOME\.continue" | Out-Null

# Symlink (requires Developer Mode or admin, preferred)
New-Item -ItemType SymbolicLink `
    -Path "$HOME\.continue\config.yaml" `
    -Target "C:\bob\config\continue\config.yaml"

# Fallback, plain copy (re-run this whenever you edit the repo config)
Copy-Item "C:\bob\config\continue\config.yaml" "$HOME\.continue\config.yaml" -Force
```

### 9.2 aider config

```powershell
# Symlink
New-Item -ItemType SymbolicLink `
    -Path "$HOME\.aider.conf.yml" `
    -Target "C:\bob\config\aider\.aider.conf.yml"

# Fallback, copy
Copy-Item "C:\bob\config\aider\.aider.conf.yml" "$HOME\.aider.conf.yml" -Force
```

### 9.3 Install VS Code extensions

```powershell
code --install-extension Continue.continue
code --install-extension saoudrizwan.claude-dev    # Cline
```

---

## 10. Build and configure fabric

### 10.1 Build the binary

```powershell
cd C:\bob\external\fabric
go build -o ..\..\bin\fabric.exe .\cmd\fabric\
cd C:\bob
```

### 10.2 Configure fabric to use the local endpoint

```powershell
New-Item -ItemType Directory -Force "$HOME\.config\fabric" | Out-Null

@"
OPENAI_API_KEY=sk-local
OPENAI_API_BASE_URL=http://localhost:8081/v1
DEFAULT_VENDOR=OpenAI
DEFAULT_MODEL=coder
"@ | Set-Content "$HOME\.config\fabric\.env" -Encoding utf8
```

Replace `8081` if you changed `litellmPort` in `config/user.psd1`.

### 10.3 Link the patterns

```powershell
# Symlink (preferred, patterns update automatically with submodule bumps)
New-Item -ItemType SymbolicLink `
    -Path "$HOME\.config\fabric\patterns" `
    -Target "C:\bob\external\fabric\data\patterns"

# Fallback, copy
Copy-Item "C:\bob\external\fabric\data\patterns" "$HOME\.config\fabric\patterns" -Recurse
```

Verify: `bin\fabric.exe -l` lists 200+ patterns.

---

## 11. Install the `llm` CLI command

```powershell
.\scripts\install-cli.ps1
```

This creates a `.cmd` shim in your Scoop shims directory so `llm` is available from any
terminal (cmd or PowerShell), and registers tab completions in your PowerShell profile.

**Open a new terminal** after this step: the PATH change only takes effect in new sessions.

Verify: `bob help` prints the command list.

---

## 12. Docker services

Docker Desktop must be installed (step 1.11) and you must have logged out and back in for
group membership to take effect. Docker Desktop must be running (whale icon in system tray).

> **Before pulling images:** Docker Desktop → Settings → General → uncheck
> **"Use containerd for pulling and storing images"** → Apply & Restart.
> If enabled, SearXNG fails with `exec /bin/sh: exec format error`.

### 12.1 Run the setup script

```powershell
.\scripts\setup-docker.ps1
```

What it does, in order:

1. Checks `docker` is on PATH; adds the Docker Desktop bin directory if installed but PATH not yet refreshed
2. Waits up to 90 seconds for the Docker daemon; launches Docker Desktop automatically if not running
3. Reads port and timezone config from `config/models.psd1` (overridable via `config/user.psd1`)
4. Writes `tools/compose/.env` with `REPO_PATH`, `LANGFUSE_PORT`, `SEARXNG_PORT`, `N8N_PORT`, `N8N_TIMEZONE`
5. Creates `tools/langfuse-data/` and `tools/n8n-data/` (gitignored; persistent data lives here)
6. Writes `config/searxng/settings.yml` if it doesn't already exist
7. Pulls all four images from Docker Hub (~3 GB total on first run):
   - `postgres:17-alpine` (~80 MB): database for Langfuse
   - `langfuse/langfuse:2` (~200 MB): observability UI
   - `searxng/searxng:<date>` (~100 MB): search engine
   - `n8nio/n8n:latest` (~2.5 GB): workflow automation
8. Starts all four containers with `docker compose up -d`

### 12.2 Expected output

```
Checking Docker daemon...
  Docker ready.
  Ports: Langfuse=3001  SearXNG=8888  n8n=5678  Timezone=UTC
Pulling images (first run may take a few minutes)...
 Image postgres:17-alpine Pulled
 Image langfuse/langfuse:2 Pulled
 Image searxng/searxng:... Pulled
 Image n8nio/n8n:latest Pulled
Starting services...
 Container compose-langfuse-postgres-1 Started
 Container compose-langfuse-postgres-1 Healthy
 Container compose-langfuse-1 Started
 Container compose-searxng-1 Started
 Container compose-n8n-1 Started

Services running:
  Langfuse:  http://localhost:3001  (login: admin@local.dev / admin123)
  SearXNG:   http://localhost:8888
  n8n:       http://localhost:5678
```

### 12.3 Verify

```powershell
bob services status
# Expected: four rows, all "Up"
#   compose-langfuse-postgres-1
#   compose-langfuse-1
#   compose-searxng-1
#   compose-n8n-1
```

### 12.4 Day-to-day management

```powershell
bob services start    # start all containers (Docker Desktop must be running)
bob services stop     # stop containers, data is preserved
bob services status   # container names, state, uptime
bob services logs     # tail all container logs (Ctrl+C to stop)
```

The full `setup-docker.ps1` only needs to run once. Use `bob services start` afterward.

### 12.5 Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `exec /bin/sh: exec format error` on any container | Image layers corrupted by interrupted download | `docker system prune -af` then re-run `.\scripts\setup-docker.ps1` |
| `langfuse-postgres unhealthy` / `dependency failed to start` | Postgres failed, almost always the corrupted-layer issue | Same: `docker system prune -af`, re-run |
| `exec format error` only on SearXNG | Containerd snapshotter enabled | Docker Desktop → Settings → General → uncheck containerd → Apply & Restart → re-run |
| `docker info` returns 500 Internal Server Error | WSL2 backend crashed or still initializing | Wait 60 s; or restart Docker Desktop from system tray |
| `docker: command not found` | PATH not refreshed after install | Open new terminal, or add `C:\Program Files\Docker\Docker\resources\bin` to PATH |
| Port conflict, address already in use | Another process on 3001, 8888, or 5678 | Set `langfusePort`, `searxngPort`, or `n8nPort` in `config/user.psd1`, re-run `.\scripts\setup-docker.ps1` |
| Daemon timeout (90 s) | Docker Desktop very slow to start | Launch Docker Desktop manually from Start menu, wait for solid whale icon, re-run |

---

## 13. Verify the installation

Run these in order. Each one exercises a different part of the stack.

```powershell
# 1. Hardware and config summary
bob diagnose

# 2. Start the inference endpoint
bob serve

# (in a second terminal)

# 3. List all models and their load state
bob models

# 4. End-to-end inference test
bob chat coder "write a fizzbuzz in Rust"

# 5. Throughput benchmark (should show pp512 ≈ 4600 t/s on RTX 5080)
bob bench

# 6. Docker services (if installed)
bob services status    # all four containers should show "Up"
```

If `bob diagnose` shows the wrong CUDA version or GPU is not detected, re-run the CUDA
install step and restart your terminal.

If `bob bench` shows prefill around 1000 t/s rather than 4000+, the build is using a
CPU fallback path. Force a clean rebuild:
```powershell
.\scripts\build-llama.ps1 -Force
```

Make sure `$env:CUDA_PATH` points to CUDA 12.8 when you rebuild.

---

## Troubleshooting

| Problem | Likely cause | Fix |
|---------|-------------|-----|
| cmake fails: `No CUDA toolset found` | `CUDA_PATH` env var not set | Set `$env:CUDA_PATH = "C:\...\CUDA\v12.8"` before cmake |
| cmake fails: `unsupported Microsoft Visual Studio version` | MSVC toolset newer than CUDA supports | Add `-DCMAKE_CUDA_FLAGS="-allow-unsupported-compiler"` to the cmake configure command, or install MSVC v14.4x through VS Installer |
| cmake fails: `cmake version 4.x` | Wrong cmake on PATH | Use VS bundled cmake at the path shown in step 4.1 |
| `llama-server.exe` crashes immediately | CUDA DLLs not copied to `bin/` | Repeat step 4.4; confirm `bin/cublas64_12.dll` exists |
| `pip install` fails in webui venv | Python version mismatch | Confirm `$py --version` is `3.12.x`; do not use the system `python` |
| `llm` not found after step 11 | PATH not refreshed | Open a new terminal |
| Docker services: `exec format error` | Containerd snapshotter enabled | Docker Desktop → Settings → General → uncheck containerd → Apply & Restart |
| SearXNG `@web` returns nothing | Docker services stopped | `bob services start` |
| Langfuse shows no traces | LiteLLM not configured | Follow the tracing setup in [USAGE.md § Langfuse](USAGE.md#langfuse--llm-observability) |
