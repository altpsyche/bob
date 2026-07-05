# Manual Installation Guide

This guide walks through every step that `install_prereqs.sh` / `install_prereqs.bat` and
`setup.sh` / `setup.bat` perform, one command at a time. Use it when you want full control, are
troubleshooting a failed automated install, or want to understand what the scripts actually do.

**If you just want to get running quickly, use the two entry scripts instead** — see the
[README](../README.md#quick-start) and [SETUP](SETUP.md). Each is a thin shell stub that hands off to
the Python cold-start kernel:

- `install_prereqs.sh` / `install_prereqs.bat` → `python -m bob.kernel prereqs` (Tier 0: toolchain)
- `setup.sh` / `setup.bat` → `python -m bob.kernel setup` (Tier 1: build, configure, start)

Everything below reproduces those two kernel runs by hand. This document is for advanced users who
prefer to drive each step manually.

> **OS coverage.** Linux (glibc; apt/dnf/pacman/zypper, plus rpm-ostree on atomic Fedora) and Windows
> 11 are supported. macOS is not supported — there is no package-manager provisioning path and the GPU
> build is CUDA-only. The bash steps below are a starting point if you adapt them by hand (e.g. with
> Homebrew and a CPU build), but nothing here is tested on macOS. See the
> [supported matrix](../README.md#supported-matrix).

---

## Table of Contents

1. [Install the toolchain (prerequisites)](#1-install-the-toolchain-prerequisites)
2. [Clone the repository and its submodules](#2-clone-the-repository-and-its-submodules)
3. [Set up the CUDA environment](#3-set-up-the-cuda-environment)
4. [Build llama.cpp](#4-build-llamacpp)
5. [Build llama-swap](#5-build-llama-swap)
6. [Create the Python virtual environments](#6-create-the-python-virtual-environments)
7. [Install the `bob` CLI](#7-install-the-bob-cli)
8. [Generate the runtime configs](#8-generate-the-runtime-configs)
9. [Download models](#9-download-models)
10. [Wire the editor clients (Continue + aider)](#10-wire-the-editor-clients-continue--aider)
11. [Build and configure fabric](#11-build-and-configure-fabric)
12. [Voice and vision (whisper + piper)](#12-voice-and-vision-whisper--piper)
13. [Docker services (Langfuse, SearXNG, n8n)](#13-docker-services-langfuse-searxng-n8n)
14. [Verify the installation](#14-verify-the-installation)

---

## 1. Install the toolchain (prerequisites)

This is the manual equivalent of `python -m bob.kernel prereqs`. It installs the build toolchain
(compiler, `make`, `cmake`, `ninja`, `go`, `node`/`npm`, Python 3.12) plus, for a GPU build, the CUDA
toolkit. Only **Git** must exist before you start.

The kernel resolves the concrete package names per distro from a single table
(`PACKAGE_MAP` in `scripts/osenv.py`). The commands below are that table, expanded.

### Linux

Pick the block for your package manager. Add the CUDA toolkit only for a GPU build (skip it for the
CPU-only tier). Cron is optional — needed only for scheduled agents (`bob agent install`); Docker is
optional — needed only for the compose services in step 13.

**Debian / Ubuntu (apt):**
```bash
sudo apt-get update
sudo apt-get install -y git curl build-essential make cmake ninja-build \
    golang-go nodejs npm python3 python3-pip python3-venv
# GPU build only:
sudo apt-get install -y nvidia-cuda-toolkit
# Optional extras:
sudo apt-get install -y cron docker.io
```

**Fedora / RHEL (dnf):**
```bash
sudo dnf install -y git curl gcc-c++ make cmake ninja-build \
    golang nodejs npm python3 python3-pip
# GPU build only:
sudo dnf install -y cuda-toolkit
# Optional extras:
sudo dnf install -y cronie docker
```

**Arch / CachyOS (pacman):**
```bash
sudo pacman -S --needed --noconfirm git curl base-devel cmake ninja \
    go nodejs npm python
# GPU build only:
sudo pacman -S --needed --noconfirm cuda
# Optional extras:
sudo pacman -S --needed --noconfirm cronie docker
```

**openSUSE (zypper):**
```bash
sudo zypper --non-interactive install git curl gcc-c++ make cmake ninja \
    go nodejs-default npm-default python3 python3-pip
# GPU build only:
sudo zypper --non-interactive install cuda
# Optional extras:
sudo zypper --non-interactive install cronie docker
```

**Atomic Fedora (Bazzite / Silverblue / Kinoite — rpm-ostree):** the base OS is immutable, so packages
are *layered* and apply on the next boot. The kernel reuses the `dnf` package names above:
```bash
sudo rpm-ostree install --idempotent --allow-inactive git curl gcc-c++ make cmake \
    ninja-build golang nodejs npm python3 python3-pip
systemctl reboot        # layered packages apply on the next boot
```
CUDA is deliberately **not** layered on an atomic host (it needs NVIDIA's repo + akmods and is fragile
there). The recommended path for GPU work on Bazzite/Silverblue is a Fedora distrobox — plain `dnf`
inside, native build and CUDA passthrough just work, and nothing touches the immutable host:
```bash
distrobox create --name bob --image fedora:latest --nvidia
distrobox enter bob
cd /path/to/bob && ./install_prereqs.sh && ./setup.sh
```

**cmake version note (rolling distros).** llama.cpp and whisper.cpp reject cmake **4.x**. Arch/CachyOS
and other rolling distros ship only 4.x. If `cmake --version` reports 4.x, the kernel downloads a pinned
Kitware **cmake 3.31.7** into `tools/` and uses that. To do it by hand:
```bash
cd tools
curl -L -O https://github.com/Kitware/CMake/releases/download/v3.31.7/cmake-3.31.7-linux-x86_64.tar.gz
tar -xzf cmake-3.31.7-linux-x86_64.tar.gz
# use tools/cmake-3.31.7-linux-x86_64/bin/cmake wherever `cmake` is called below
cd ..
```

### Windows

The Windows path uses winget / scoop to install the **toolchain** (not Bob itself). Install these once;
open a new terminal afterward so each lands on PATH.

```bat
:: Git — install from https://git-scm.com if not already present, then:
winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements
winget install OpenJS.NodeJS --accept-package-agreements --accept-source-agreements
winget install astral-sh.uv --accept-package-agreements --accept-source-agreements
winget install GoLang.Go --accept-package-agreements --accept-source-agreements
:: cmake 3.x (4.x is rejected by llama.cpp; VS2022 also bundles a usable 3.31.x):
winget install Kitware.CMake --version 3.31.7 --accept-package-agreements --accept-source-agreements
```

VS2022 with the **Desktop development with C++** workload is required to compile llama.cpp and cannot
be fully automated:
```bat
winget install Microsoft.VisualStudio.2022.Community --accept-package-agreements --accept-source-agreements
:: Then: open "Visual Studio Installer" -> Modify -> check "Desktop development with C++" -> Modify
```

For a GPU build, install CUDA (12.8 covers Blackwell, Ada, and Ampere) and, optionally, Docker Desktop:
```bat
winget install Nvidia.CUDA --version 12.8 --accept-package-agreements --accept-source-agreements
winget install Docker.DockerDesktop --accept-package-agreements --accept-source-agreements
```
After installing Docker Desktop, **log out of Windows and back in** — Docker adds your user to the
`docker-users` group and that only takes effect at login. Restart your terminal after CUDA installs to
pick up the new PATH entries.

---

## 2. Clone the repository and its submodules

Bob vendors four submodules: `external/llama.cpp` (the engine), `external/llama-swap` (the model-swap
proxy), `external/whisper.cpp` (STT), and `external/fabric` (prompt patterns).

Linux:
```bash
git clone --recurse-submodules <your-remote> bob
cd bob
```

Windows:
```bat
git clone --recurse-submodules <your-remote> C:\bob
cd C:\bob
```

If you cloned without `--recurse-submodules`, populate them now (works the same on every OS):
```bash
git submodule update --init --recursive
```

Verify the submodules are populated:
```bash
ls external/llama.cpp/CMakeLists.txt external/llama-swap/main.go external/fabric/cmd/fabric/main.go
```

---

## 3. Set up the CUDA environment

Skip this section for a CPU-only build (jump to [step 4](#4-build-llamacpp) and use the CPU block).

The build finds the CUDA toolkit by probing disk (`/usr/local/cuda*`, `/opt/cuda`, `$CUDA_PATH` on
Linux; `C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\vX.Y` on Windows). Blackwell (sm_120) needs
CUDA **12.8+**.

Identify your GPU's compute architecture — you need it for the cmake step:
```bash
nvidia-smi --query-gpu=compute_cap --format=csv,noheader
# e.g. 12.0 (Blackwell), 8.9 (Ada), 8.6 (Ampere)
```

Convert to the cmake `CUDA_ARCHITECTURES` value (drop the dot):

| GPU generation | Example cards | `nvidia-smi` output | cmake value |
|---|---|---|---|
| Blackwell | RTX 5080, 5090 | `12.0` | `120` |
| Ada Lovelace | RTX 4090, 4080, 4070 Ti | `8.9` | `89` |
| Ampere | RTX 3090, 3080, 3070 | `8.6` | `86` |

**Linux — put the toolkit on PATH** (a convenience; the build probes disk regardless):
```bash
export CUDA_PATH=/usr/local/cuda        # or wherever your toolkit lives
export PATH="$CUDA_PATH/bin:$PATH"
nvcc --version                          # confirm it resolves
```

On rolling distros, the default `g++`/`gcc` is often newer than nvcc accepts. If so, point nvcc at an
older host compiler (this is what `install_prereqs` wires into `/etc/profile.d/cuda.sh` and the fish
drop-in):
```bash
export NVCC_CCBIN=/usr/bin/g++-13       # an nvcc-compatible g++, if the default is too new
```

**Windows — set the toolkit path for the session:**
```bat
set "CUDA_PATH=C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.8"
set "PATH=%CUDA_PATH%\bin;%PATH%"
nvcc --version
```

---

## 4. Build llama.cpp

This produces `bin/llama-server`. On Linux the build uses the **Ninja** generator; on Windows it uses
the **Visual Studio 17 2022** generator. These are the exact flags `scripts/tools/build.py`
(`build_llama`) passes.

### Linux — CUDA build

Replace `120` with your GPU's value from the table above. If you provisioned the pinned cmake in step 1,
use its full path instead of `cmake`.
```bash
cd external/llama.cpp
rm -rf build
cmake -B build -G Ninja \
    -DGGML_CUDA=ON \
    -DCMAKE_CUDA_COMPILER="$CUDA_PATH/bin/nvcc" \
    -DCMAKE_CUDA_ARCHITECTURES=120 \
    -DGGML_CUDA_FORCE_CUBLAS=OFF \
    -DCUDAToolkit_ROOT="$CUDA_PATH" \
    -DCMAKE_BUILD_TYPE=Release
    # if nvcc needs an older host compiler, add:
    # -DCMAKE_CUDA_HOST_COMPILER=/usr/bin/g++-13
cmake --build build --config Release -j
cd ../..
```

### Linux — CPU build

```bash
cd external/llama.cpp
rm -rf build
cmake -B build -G Ninja -DGGML_CUDA=OFF -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release -j
cd ../..
```

### Stage the binaries into `bin/` (Linux)

The Ninja build drops binaries in `build/bin/`. Copy them (and the shared GGML libs beside them) into
the repo's `bin/`:
```bash
mkdir -p bin
cp external/llama.cpp/build/bin/* bin/
bin/llama-server --version              # sanity check
```

### Windows — CUDA build

```bat
cd external\llama.cpp
if exist build rmdir /s /q build
cmake -B build -G "Visual Studio 17 2022" -T "cuda=%CUDA_PATH%" ^
    -DGGML_CUDA=ON ^
    -DCMAKE_CUDA_ARCHITECTURES=120 ^
    -DGGML_CUDA_FORCE_CUBLAS=OFF ^
    -DCUDAToolkit_ROOT="%CUDA_PATH%"
cmake --build build --config Release -j
cd ..\..
```

For a **CPU** build on Windows, use `-G "Visual Studio 17 2022" -DGGML_CUDA=OFF` instead.

Stage the server binary and the CUDA runtime DLLs into `bin\` (the VS build is multi-config, so output
lands in `build\bin\Release`):
```bat
if not exist bin mkdir bin
copy external\llama.cpp\build\bin\Release\llama-server.exe bin\
copy "%CUDA_PATH%\bin\cublas64_12.dll"   bin\
copy "%CUDA_PATH%\bin\cublasLt64_12.dll" bin\
copy "%CUDA_PATH%\bin\cudart64_12.dll"   bin\
bin\llama-server.exe --version
```

> **MSVC / nvcc compatibility:** if cmake fails with `unsupported Microsoft Visual Studio version`, add
> `-DCMAKE_CUDA_FLAGS="-allow-unsupported-compiler"` to the configure command, or install MSVC v14.4x
> through the VS Installer to match CUDA 12.8.

Once you've verified a good build, `bob build` (and `bob build --force`) will rebuild through the same
code path in future.

---

## 5. Build llama-swap

llama-swap is a small Go binary that fronts llama.cpp and swaps models on demand. Same command on every
OS (`build_llama_swap` runs `go build -o bin/llama-swap .`):

Linux:
```bash
cd external/llama-swap
go build -o ../../bin/llama-swap .
cd ../..
bin/llama-swap --version
```

Windows:
```bat
cd external\llama-swap
go build -o ..\..\bin\llama-swap.exe .
cd ..\..
bin\llama-swap.exe --version
```

If you don't have Go, drop a prebuilt llama-swap release binary into `bin/` instead.

---

## 6. Create the Python virtual environments

Bob keeps its Python tools in isolated venvs under `tools/` because Open WebUI, aider, and LiteLLM have
conflicting dependency pins. They must be built with **Python 3.11 or 3.12** (3.13+ has Open WebUI
conflicts). The kernel builds them with `osenv.new_bob_venv`; the manual equivalent is `python -m venv`
plus a `pip install -r` of the matching requirements file.

Two venvs are built by default; `venv-webui` is opt-in; `venv-eval` is provisioned on demand by the
first `bob eval`.

| Venv | Requirements file | Built by default? |
|---|---|---|
| `venv-litellm` | `tools/litellm-requirements.txt` | yes — the LiteLLM proxy **and** the `bob` CLI's runtime deps live here |
| `venv-aider` | `tools/aider-requirements.txt` | yes |
| `venv-webui` | `tools/webui-requirements.txt` | no — opt-in (large: torch/transformers, multi-GB) |
| `venv-eval` | `tools/eval-requirements.txt` | no — on demand for `bob eval` |

> The `bob` command itself runs under `tools/venv-litellm/bin/python`, so build **venv-litellm first** —
> nothing else works until it exists. On Windows the venv layout is `tools\<venv>\Scripts\` and the
> pinned `.lock` files are used in place of `.txt`.

Linux (repeat per venv, changing the two names):
```bash
python3 -m venv tools/venv-litellm
tools/venv-litellm/bin/python -m pip install --upgrade pip
tools/venv-litellm/bin/python -m pip install -r tools/litellm-requirements.txt

python3 -m venv tools/venv-aider
tools/venv-aider/bin/python -m pip install --upgrade pip
tools/venv-aider/bin/python -m pip install -r tools/aider-requirements.txt

# opt-in Open WebUI venv (only if you want the browser UI):
python3 -m venv tools/venv-webui
tools/venv-webui/bin/python -m pip install --upgrade pip
tools/venv-webui/bin/python -m pip install -r tools/webui-requirements.txt
```

Windows (uses the pinned `.lock` files):
```bat
python -m venv tools\venv-litellm
tools\venv-litellm\Scripts\python.exe -m pip install --upgrade pip
tools\venv-litellm\Scripts\python.exe -m pip install -r tools\litellm-requirements.lock

python -m venv tools\venv-aider
tools\venv-aider\Scripts\python.exe -m pip install --upgrade pip
tools\venv-aider\Scripts\python.exe -m pip install -r tools\aider-requirements.lock
```

Each install takes 2–10 minutes; `venv-webui` is by far the largest.

---

## 7. Install the `bob` CLI

This puts `bob` on your PATH so the remaining steps (`bob gen`, `bob fetch`, …) resolve.

**Linux.** The repo-root `./bob` shim runs `tools/venv-litellm/bin/python -m bob`. Symlink it
into `~/.local/bin`:
```bash
mkdir -p ~/.local/bin
ln -sf "$(pwd)/bob" ~/.local/bin/bob
# ensure ~/.local/bin is on PATH:
#   fish:      fish_add_path ~/.local/bin
#   bash/zsh:  add 'export PATH="$HOME/.local/bin:$PATH"' to your rc
```
Open a new terminal, then `bob help` should print the catalog. If you'd rather not install it globally,
you can run any command in-place as `./bob <verb>`.

**Windows.** The kernel writes a `bob.cmd` shim (`python -m bob`) into your scoop shims directory. If
you use scoop, `install_cli` does this; by hand, create `bob.cmd` somewhere on PATH:
```bat
:: create %USERPROFILE%\scoop\shims\bob.cmd (or any folder on PATH) containing:
::   @echo off
::   set "PYTHONPATH=C:\bob\scripts"
::   "C:\bob\tools\venv-litellm\Scripts\python.exe" -m bob %*
```
If you don't use scoop, add the repo folder to PATH and invoke `bob` from there. Open a new terminal,
then `bob help`.

---

## 8. Generate the runtime configs

`bob gen` reads the model registry (`config/models.json`, plus your `config/user.json` overrides) and
writes the generated runtime configs: `config/llama-swap.yaml` (local model routing) and
`config/litellm.yaml` (the OpenAI-compatible proxy's model list). These are overwritten on every
`bob gen` — do not edit them by hand.

```bash
bob gen                 # for the active profile
bob gen 12gb            # target a specific VRAM profile
```

To customize model parameters or add cloud "pro" models, edit `config/models.json` or create
`config/user.json` (a deep-merged per-machine override, e.g. `{"agent":{"maxSteps":3}}` or
`{"peers":{"deepseek":{"apiKey":"…"}}}`), then re-run `bob gen`.

Verify the outputs exist:
```bash
ls config/llama-swap.yaml config/litellm.yaml
```

---

## 9. Download models

`bob fetch` downloads the GGUF files for the active profile into `models/`, verifying each against the
SHA256 pinned in the registry. Downloads are resumable — re-run if interrupted.

```bash
bob fetch                    # download the active profile (~38 GB for 16gb, ~21 GB for 12gb)
bob fetch --list             # preview what would be downloaded, download nothing
bob fetch 12gb               # download a specific profile
```

For gated HuggingFace repos, set `HF_TOKEN` first:

Linux:
```bash
export HF_TOKEN=hf_...
bob fetch
```

Windows:
```bat
set HF_TOKEN=hf_...
bob fetch
```

To provide models yourself, copy the `.gguf` files into `models/` manually and skip this step.

---

## 10. Wire the editor clients (Continue + aider)

This points VS Code's Continue extension and the aider CLI at the repo's config files (symlink, with a
copy fallback where symlinks aren't permitted). The kernel does this in `setup_clients`; by hand:

Linux:
```bash
bob gen                                                    # regenerates config/continue/config.yaml too
mkdir -p ~/.continue
ln -sf "$(pwd)/config/continue/config.yaml" ~/.continue/config.yaml
ln -sf "$(pwd)/config/aider/.aider.conf.yml" ~/.aider.conf.yml
```

Windows (symlinks need Developer Mode or admin; otherwise copy):
```bat
if not exist "%USERPROFILE%\.continue" mkdir "%USERPROFILE%\.continue"
mklink "%USERPROFILE%\.continue\config.yaml" "C:\bob\config\continue\config.yaml"
mklink "%USERPROFILE%\.aider.conf.yml" "C:\bob\config\aider\.aider.conf.yml"
:: fallback if mklink is not permitted:
::   copy "C:\bob\config\continue\config.yaml" "%USERPROFILE%\.continue\config.yaml"
::   copy "C:\bob\config\aider\.aider.conf.yml" "%USERPROFILE%\.aider.conf.yml"
```

Install the VS Code extensions (same on every OS):
```bash
code --install-extension Continue.continue
code --install-extension saoudrizwan.claude-dev    # Cline
```

---

## 11. Build and configure fabric

fabric is a Go binary that runs 250+ named LLM prompt patterns. `bob fabric-setup` builds it and wires
`~/.config/fabric`; the manual equivalent (`setup_fabric` in `scripts/tools/build.py`):

Linux:
```bash
cd external/fabric
go build -o ../../bin/fabric ./cmd/fabric/
cd ../..

mkdir -p ~/.config/fabric
cat > ~/.config/fabric/.env <<'EOF'
OPENAI_API_KEY=sk-local
OPENAI_API_BASE_URL=http://localhost:8081/v1
DEFAULT_VENDOR=OpenAI
DEFAULT_MODEL=coder
EOF
ln -sf "$(pwd)/external/fabric/data/patterns" ~/.config/fabric/patterns
bin/fabric -l            # lists 250+ patterns
```

Windows:
```bat
cd external\fabric
go build -o ..\..\bin\fabric.exe .\cmd\fabric\
cd ..\..

if not exist "%USERPROFILE%\.config\fabric" mkdir "%USERPROFILE%\.config\fabric"
(
  echo OPENAI_API_KEY=sk-local
  echo OPENAI_API_BASE_URL=http://localhost:8081/v1
  echo DEFAULT_VENDOR=OpenAI
  echo DEFAULT_MODEL=coder
) > "%USERPROFILE%\.config\fabric\.env"
mklink /D "%USERPROFILE%\.config\fabric\patterns" "C:\bob\external\fabric\data\patterns"
bin\fabric.exe -l
```

Replace `8081` if you changed `litellmPort` in `config/user.json`.

---

## 12. Voice and vision (whisper + piper)

Optional Phase-2 feature. `bob setup-voice` builds `whisper.cpp` (STT), downloads the whisper model and
the piper TTS binary + voice, and installs the audio deps into `venv-litellm`. It requires
`venv-litellm` to already exist (step 6).

```bash
bob setup-voice              # build whisper-server, fetch STT model + piper voice
bob setup-voice --force      # rebuild / re-download everything
```

Under the hood this builds `whisper.cpp` with the same CUDA/cmake seams as llama.cpp
(`-DWHISPER_CUDA=ON` for a GPU, CPU fallback otherwise) into `bin/whisper-server` + `bin/whisper-cli`,
downloads `ggml-small.bin` into `models/whisper/`, extracts piper into `bin/`, and drops the voice model
into `bin/voices/`. Enable `voice.enabled` / `vision.enabled` in `config/user.json` to use it.

---

## 13. Docker services (Langfuse, SearXNG, n8n)

Optional. These run in Docker; skip if you don't need observability, private search, or workflow
automation. Docker must be installed and its daemon running. After setup, manage them with
`bob services start|stop|status|logs`.

The kernel's `setup_docker` writes `tools/compose/.env`, creates the persistent data dirs, writes a
default `config/searxng/settings.yml`, then pulls and starts the stack. To do it by hand, ensure Docker
is up, then:

Linux:
```bash
docker info                                  # confirm the daemon responds
docker compose -f tools/compose/docker-compose.yml pull
docker compose -f tools/compose/docker-compose.yml up -d
```

Windows:
```bat
docker info
docker compose -f tools\compose\docker-compose.yml pull
docker compose -f tools\compose\docker-compose.yml up -d
```

The compose file reads ports from `tools/compose/.env` (defaults: Langfuse `3001`, SearXNG `8888`,
n8n `5678`). If that file is missing, create it:
```bash
printf 'REPO_PATH=%s\nLANGFUSE_PORT=3001\nSEARXNG_PORT=8888\nN8N_PORT=5678\nN8N_TIMEZONE=UTC\n' \
    "$(pwd)" > tools/compose/.env
```

Once up:

- **Langfuse** — http://localhost:3001 (login `admin@local.dev` / `admin123`)
- **SearXNG** — http://localhost:8888
- **n8n** — http://localhost:5678

Verify and manage:
```bash
bob services status          # container names, state, uptime
bob services logs            # tail all container logs
bob services stop            # stop containers (data is preserved)
```

> **Windows / Docker Desktop:** disable the containerd snapshotter before pulling images (Settings →
> General → uncheck "Use containerd for pulling and storing images" → Apply & Restart) — otherwise
> SearXNG fails with `exec format error`.

---

## 14. Verify the installation

Run these in order; each exercises a different part of the stack.

```bash
# 1. Hardware, CUDA, and config summary
bob diagnose

# 2. Start the inference stack (llama-swap :8080 + LiteLLM :8081). Ctrl-C stops it.
#    Or run `bob up` to start it in the background.
bob serve

# 3. In another terminal: list models and their load state
bob models

# 4. End-to-end inference
bob chat "write a fizzbuzz in Rust"

# 5. Throughput benchmark (≈ pp512 4600 t/s, tg128 89 t/s on an RTX 5080)
bob bench

# 6. Docker services, if installed
bob services status
```

You don't need to keep `bob serve` running for everyday use — inference **auto-starts on demand** the
first time you talk to Bob (`bob`, `bob chat`, `bob agent …`). `bob serve` (foreground) and `bob up`
(background) are there for when you want the stack pre-warmed or serving outside-terminal clients.

If `bob bench` shows prefill around 1000 t/s rather than 4000+, the build fell back to a CPU path. Force
a clean rebuild and confirm `CUDA_PATH` points at 12.8+:
```bash
bob build --force
```

---

## Troubleshooting

| Problem | Likely cause | Fix |
|---|---|---|
| cmake fails: `No CUDA toolset found` / can't find nvcc | `CUDA_PATH` not set | Set `CUDA_PATH` and `PATH` (step 3), then re-run the configure |
| cmake fails: version `4.x` rejected | rolling-distro cmake is 4.x | Use the pinned cmake 3.31.7 from step 1 |
| cmake fails: `unsupported Microsoft Visual Studio version` | MSVC newer than CUDA supports | Add `-DCMAKE_CUDA_FLAGS="-allow-unsupported-compiler"`, or install MSVC v14.4x |
| nvcc errors about host compiler being too new (Linux) | default `g++` newer than nvcc accepts | Set `NVCC_CCBIN` / `-DCMAKE_CUDA_HOST_COMPILER` to an older g++ |
| `llama-server` crashes immediately (Windows) | CUDA DLLs not staged into `bin\` | Re-copy `cublas64_12.dll`, `cublasLt64_12.dll`, `cudart64_12.dll` (step 4) |
| `pip install` fails in a venv | wrong Python | Confirm the venv's Python is 3.11/3.12, not the system default |
| `bob` not found after step 7 | PATH not refreshed | Open a new terminal; ensure `~/.local/bin` (or the shim dir) is on PATH |
| `bob gen`/`bob fetch` error importing deps | `venv-litellm` missing | Build `venv-litellm` first (step 6) — the CLI runs under it |
| `bench` shows ~1000 t/s prefill | CPU fallback build | `bob build --force` with `CUDA_PATH` on 12.8+ |
| SearXNG `exec format error` (Windows) | containerd snapshotter enabled | Docker Desktop → uncheck containerd → Apply & Restart |
| Langfuse shows no traces | LiteLLM tracing not configured | See [USAGE § Langfuse](USAGE.md#langfuse--llm-observability) |

For alternatives when a build or install won't cooperate (prebuilt binaries, CPU tier, offline models),
see [FALLBACKS.md](FALLBACKS.md).
