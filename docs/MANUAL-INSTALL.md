# Manual Installation Guide

This guide reproduces, one command at a time, every step that `install_prereqs.sh` /
`install_prereqs.bat` and `setup.sh` / `setup.bat` perform, building the inference **engine from source**.
Use it when you want full control, are troubleshooting, want a source build (`--from-source`), or want to
understand what the scripts do.

**To get running quickly, use the one-command installer instead.** It downloads a prebuilt, driver-only
engine (no CUDA toolkit, nothing to compile) and needs none of the CUDA-build steps below. On Linux:
`curl -fsSL https://raw.githubusercontent.com/altpsyche/bob/main/install/install.sh | sh`; on Windows
PowerShell: `irm https://raw.githubusercontent.com/altpsyche/bob/main/install/install.ps1 | iex`. It
clones the repo, runs both entry scripts, and verifies the result. See the
[README](../README.md#quick-start) and [SETUP](SETUP.md) for details. The CUDA-toolkit and
build-from-source steps here apply to the `--from-source` path.

You can also run the two entry scripts directly. Each is a thin shell stub that hands off to the
Python cold-start kernel:

- `install_prereqs.sh` / `install_prereqs.bat` → `python -m bob.kernel prereqs` (Tier 0: toolchain)
- `setup.sh` / `setup.bat` → `python -m bob.kernel setup` (Tier 1: build, configure, start)

Everything below reproduces those two kernel runs by hand, for advanced users who prefer to drive each
step manually.

> **OS coverage.** Linux (glibc; apt/dnf/pacman/zypper, plus rpm-ostree on atomic Fedora) and Windows
> 11 are supported. macOS is not supported, there is no package-manager provisioning path and the GPU
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
10. [Wire the editor clients (Continue, optional aider)](#10-wire-the-editor-clients-continue-optional-aider)
11. [Build and configure fabric (optional)](#11-build-and-configure-fabric-optional)
12. [Voice and vision (faster-whisper + piper)](#12-voice-and-vision-faster-whisper--piper)
13. [Optional add-on services (Langfuse, SearXNG, n8n)](#13-optional-add-on-services-langfuse-searxng-n8n)
14. [Verify the installation](#14-verify-the-installation)

---

## 1. Install the toolchain (prerequisites)

The manual equivalent of `python -m bob.kernel prereqs --from-source`. It installs the build toolchain
(compiler, `make`, `cmake`, `ninja`, `go`, Python 3.12) plus, for a source GPU build, the CUDA toolkit.
`node`/`npm` are optional (`--with-node`: n8n and Continue's npx MCP servers need them). The default
prebuilt path installs only `git`, `curl` and Python: no compiler, cmake, Go or CUDA toolkit, since the
engine is a driver-only prebuilt and llama-swap is a pinned, SHA-verified release binary. A platform with
no prebuilt engine (arm64 Linux) needs the compiler and cmake even without `--from-source`, because it
compiles llama.cpp automatically; its llama-swap still comes from the pinned arm64 release. Only **Git**
must exist before you start.

The kernel resolves the concrete package names per distro from a single table
(`PACKAGE_MAP` in `scripts/osenv.py`); the commands below are that table, expanded.

### Linux

Pick the block for your package manager. Add the CUDA toolkit only for a GPU build (skip it for the
CPU-only tier). A default install is 100% Docker-free. Cron is optional (needed only for scheduled
agents, `bob agent install`), as is Docker (needed only if you later opt into the SearXNG or Langfuse
add-on services in step 13).

**Debian / Ubuntu (apt):**
```bash
sudo apt-get update
sudo apt-get install -y git curl python3 python3-pip python3-venv
# source build only:
sudo apt-get install -y build-essential make cmake ninja-build golang-go
# optional (n8n, Continue's npx MCP servers):
sudo apt-get install -y nodejs npm
# GPU source build only:
sudo apt-get install -y nvidia-cuda-toolkit
# Optional extras:
sudo apt-get install -y cron docker.io
```

**Fedora / RHEL (dnf):**
```bash
sudo dnf install -y git curl python3 python3-pip
# source build only:
sudo dnf install -y gcc-c++ make cmake ninja-build golang
# optional (n8n, Continue's npx MCP servers):
sudo dnf install -y nodejs npm
# GPU source build only:
sudo dnf install -y cuda-toolkit
# Optional extras:
sudo dnf install -y cronie docker
```

**Arch / CachyOS (pacman):**
```bash
sudo pacman -S --needed --noconfirm git curl python
# source build only:
sudo pacman -S --needed --noconfirm base-devel cmake ninja go
# optional (n8n, Continue's npx MCP servers):
sudo pacman -S --needed --noconfirm nodejs npm
# GPU source build only:
sudo pacman -S --needed --noconfirm cuda
# Optional extras:
sudo pacman -S --needed --noconfirm cronie docker
```

**openSUSE (zypper):**
```bash
sudo zypper --non-interactive install git curl python3 python3-pip
# source build only:
sudo zypper --non-interactive install gcc-c++ make cmake ninja go
# optional (n8n, Continue's npx MCP servers):
sudo zypper --non-interactive install nodejs-default npm-default
# GPU source build only:
sudo zypper --non-interactive install cuda
# Optional extras:
sudo zypper --non-interactive install cronie docker
```

**Atomic Fedora (Bazzite / Silverblue / Kinoite, rpm-ostree):** the base OS is immutable, so packages
are *layered* and apply on the next boot. The prereq step layers only what the image lacks, and on the
default prebuilt path that is usually nothing (git, curl and Python ship in the image), so there is no
transaction and no reboot. A source build needs the toolchain, which is layered with the `dnf` names
above and applies after a reboot:
```bash
sudo rpm-ostree install --idempotent --allow-inactive gcc-c++ make cmake ninja-build golang
systemctl reboot        # layered packages apply on the next boot
```
Or skip layering entirely and do the source build inside a Fedora distrobox (below).
CUDA is deliberately **not** layered on an atomic host (it needs NVIDIA's repo + akmods and is fragile
there). For GPU work on Bazzite/Silverblue, use a Fedora distrobox: plain `dnf` inside, native build
and CUDA passthrough just work, and nothing touches the immutable host:
```bash
distrobox create --name bob --image fedora:latest --nvidia
distrobox enter bob
cd /path/to/bob && ./install_prereqs.sh --from-source && ./setup.sh --from-source
```

**cmake version note (rolling distros, source builds only).** llama.cpp rejects cmake **4.x**, which is all
Arch/CachyOS and other rolling distros ship. If `cmake --version` reports 4.x, the kernel downloads a
pinned Kitware **cmake 3.31.7** into `tools/` and uses that. To do it by hand:
```bash
cd tools
curl -L -O https://github.com/Kitware/CMake/releases/download/v3.31.7/cmake-3.31.7-linux-x86_64.tar.gz
tar -xzf cmake-3.31.7-linux-x86_64.tar.gz
# use tools/cmake-3.31.7-linux-x86_64/bin/cmake wherever `cmake` is called below
cd ..
```

### Windows

The Windows path uses winget / scoop to install the **toolchain** (not Bob itself). The default prebuilt
path needs only Python 3.12 and uv; the rest is for a source build or an optional feature. Install these
once, then open a new terminal so each lands on PATH.

```bat
:: Git, install from https://git-scm.com if not already present, then:
winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements
winget install astral-sh.uv --accept-package-agreements --accept-source-agreements
:: optional (n8n, Continue's npx MCP servers):
winget install OpenJS.NodeJS --accept-package-agreements --accept-source-agreements
:: source build only (llama-swap from source, fabric):
winget install GoLang.Go --accept-package-agreements --accept-source-agreements
:: source build only, cmake 3.x (4.x is rejected by llama.cpp; VS2022 also bundles a usable 3.31.x):
winget install Kitware.CMake --version 3.31.7 --accept-package-agreements --accept-source-agreements
```

VS2022 with the **Desktop development with C++** workload is required only to compile llama.cpp
(`--from-source`) and cannot be fully automated:
```bat
winget install Microsoft.VisualStudio.2022.Community --accept-package-agreements --accept-source-agreements
:: Then: open "Visual Studio Installer" -> Modify -> check "Desktop development with C++" -> Modify
```

For a GPU source build, install CUDA (12.8 covers Blackwell, Ada, and Ampere):
```bat
winget install Nvidia.CUDA --version 12.8 --accept-package-agreements --accept-source-agreements
```
Restart your terminal after CUDA installs to pick up the new PATH entries. Docker Desktop is not a
prerequisite: `bob services searxng|langfuse start` installs it the first time you start a Docker
service. After it installs, **log out of Windows and back in**: Docker adds your user to the
`docker-users` group, which only takes effect at login.

---

## 2. Clone the repository and its submodules

Bob vendors three submodules: `external/llama.cpp` (the engine), `external/llama-swap` (the model-swap
proxy, built from here only with `--from-source`), and `external/fabric` (prompt patterns, opt-in).

Linux:
```bash
git clone --recurse-submodules https://github.com/altpsyche/bob.git bob
cd bob
```

Windows:
```bat
git clone --recurse-submodules https://github.com/altpsyche/bob.git C:\bob
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

Identify your GPU's compute architecture (needed for the cmake step):
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

**Linux, put the toolkit on PATH** (a convenience; the build probes disk regardless):
```bash
export CUDA_PATH=/usr/local/cuda        # or wherever your toolkit lives
export PATH="$CUDA_PATH/bin:$PATH"
nvcc --version                          # confirm it resolves
```

On rolling distros, the default `g++`/`gcc` is often newer than nvcc accepts. If so, point nvcc at an
older host compiler (as `install_prereqs` wires into `/etc/profile.d/cuda.sh` and the fish drop-in):
```bash
export NVCC_CCBIN=/usr/bin/g++-13       # an nvcc-compatible g++, if the default is too new
```

**Windows, set the toolkit path for the session:**
```bat
set "CUDA_PATH=C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.8"
set "PATH=%CUDA_PATH%\bin;%PATH%"
nvcc --version
```

---

## 4. Build llama.cpp

This produces `bin/llama-server`. Both OSes use the **Ninja** generator (single-config; it also lets the
build be ccache'd). These are the exact flags `scripts/tools/build.py` (`build_llama`) passes.

> **Windows:** Ninja needs the MSVC toolchain (`cl.exe`) + Ninja on `PATH`. `bob build` activates it for you
> (`osenv.ensure_msvc_env` runs `VsDevCmd.bat`), so a normal build works from any shell. Only when you run the
> `cmake` commands **by hand** (below) do you need to start from a **"Developer Command Prompt for VS 2022"**
> (or run `VsDevCmd.bat` first) — that is what puts `cl.exe`, `cmake`, and `ninja` on `PATH`.

### Linux, CUDA build

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

### Linux, CPU build

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

### Windows, CUDA build

Run these from a **"Developer Command Prompt for VS 2022"** (so `cl.exe`, `cmake`, and `ninja` are on `PATH`).

```bat
cd external\llama.cpp
if exist build rmdir /s /q build
cmake -B build -G Ninja ^
    -DGGML_CUDA=ON ^
    -DCMAKE_CUDA_COMPILER="%CUDA_PATH%\bin\nvcc.exe" ^
    -DCMAKE_CUDA_ARCHITECTURES=120 ^
    -DGGML_CUDA_FORCE_CUBLAS=OFF ^
    -DCUDAToolkit_ROOT="%CUDA_PATH%" ^
    -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release -j
cd ..\..
```

For a **CPU** build on Windows, use `-G Ninja -DGGML_CUDA=OFF -DCMAKE_BUILD_TYPE=Release` instead.

Stage the server binary and the CUDA runtime DLLs into `bin\` (Ninja is single-config, so output lands in
`build\bin`):
```bat
if not exist bin mkdir bin
copy external\llama.cpp\build\bin\llama-server.exe bin\
copy "%CUDA_PATH%\bin\cublas64_12.dll"   bin\
copy "%CUDA_PATH%\bin\cublasLt64_12.dll" bin\
copy "%CUDA_PATH%\bin\cudart64_12.dll"   bin\
bin\llama-server.exe --version
```

> **MSVC / nvcc compatibility:** if cmake fails with `unsupported Microsoft Visual Studio version`, add
> `-DCMAKE_CUDA_FLAGS="-allow-unsupported-compiler"` to the configure command, or install MSVC v14.4x
> through the VS Installer to match CUDA 12.8.

Once you've verified a good build, `bob build` (and `bob build --force`) rebuilds through the same code
path in future.

---

## 5. Build llama-swap

llama-swap is a small Go binary that fronts llama.cpp and swaps models on demand. By default
`build_llama_swap` downloads the release binary pinned in `versions.lock` (`binaries.llama-swap`: a URL
and SHA-256 per OS and CPU, including arm64 Linux) and installs it into `bin/` only after the checksum
matches; no Go is needed. It builds from the submodule with Go only for `--from-source`, for a platform
with no pinned asset, or when the pinned release no longer matches the submodule commit. The source
build is the same command on every OS (`go build -o bin/llama-swap .`):

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

Without Go, run `python -m bob.kernel build-swap` to install the pinned release binary instead.

---

## 6. Create the Python virtual environments

Bob keeps its Python tools in isolated venvs under `tools/` because Open WebUI, aider, and LiteLLM have
conflicting dependency pins. Build them with **Python 3.11 or 3.12** (3.13+ has Open WebUI conflicts).
The kernel uses `osenv.new_bob_venv`; the manual equivalent is `python -m venv` plus a `pip install -r`
of the matching requirements file.

One venv is built by default; `venv-webui` and `venv-aider` are opt-in; `venv-eval` is provisioned on
demand by the first `bob eval`.

| Venv | Requirements file | Built by default? |
|---|---|---|
| `venv-litellm` | `tools/litellm-requirements.txt` | yes, the LiteLLM proxy **and** the `bob` CLI's runtime deps live here |
| `venv-aider` | `tools/aider-requirements.txt` | no, opt-in (`--with-aider` or `bob aider-setup`) |
| `venv-webui` | `tools/webui-requirements.txt` | no, opt-in (large: torch/transformers, multi-GB) |
| `venv-eval` | `tools/eval-requirements.txt` | no, on demand for `bob eval` |

> The `bob` command itself runs under `tools/venv-litellm/bin/python`, so build **venv-litellm first**:
> nothing else works until it exists. The kernel installs every venv from its pinned `.lock` file on
> every OS (platform-only rows carry environment markers) and uses the `.txt` only when no lock exists. On
> Windows the venv layout is `tools\<venv>\Scripts\`.

Linux (repeat per venv, changing the two names):
```bash
python3 -m venv tools/venv-litellm
tools/venv-litellm/bin/python -m pip install --upgrade pip
tools/venv-litellm/bin/python -m pip install -r tools/litellm-requirements.lock

# opt-in aider venv (or: bob aider-setup):
python3 -m venv tools/venv-aider
tools/venv-aider/bin/python -m pip install --upgrade pip
tools/venv-aider/bin/python -m pip install -r tools/aider-requirements.lock

# opt-in Open WebUI venv (only if you want the browser UI):
python3 -m venv tools/venv-webui
tools/venv-webui/bin/python -m pip install --upgrade pip
tools/venv-webui/bin/python -m pip install -r tools/webui-requirements.lock
```

Windows:
```bat
python -m venv tools\venv-litellm
tools\venv-litellm\Scripts\python.exe -m pip install --upgrade pip
tools\venv-litellm\Scripts\python.exe -m pip install -r tools\litellm-requirements.lock

:: opt-in aider venv (or: bob aider-setup):
python -m venv tools\venv-aider
tools\venv-aider\Scripts\python.exe -m pip install --upgrade pip
tools\venv-aider\Scripts\python.exe -m pip install -r tools\aider-requirements.lock
```

Each install takes 2 to 10 minutes; `venv-webui` is by far the largest.

---

## 7. Install the `bob` CLI

This puts `bob` on your PATH so the remaining steps (`bob gen`, `bob fetch`, …) resolve.

**Linux.** The repo-root `./bob` shim runs `tools/venv-litellm/bin/python -m bob`. Symlink it into
`~/.local/bin`:
```bash
mkdir -p ~/.local/bin
ln -sf "$(pwd)/bob" ~/.local/bin/bob
# ensure ~/.local/bin is on PATH:
#   fish:      fish_add_path ~/.local/bin
#   bash/zsh:  add 'export PATH="$HOME/.local/bin:$PATH"' to your rc
```
Open a new terminal, then `bob help` should print the catalog. To avoid a global install, run any
command in-place as `./bob <verb>`.

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
writes the runtime configs: `config/llama-swap.yaml` (local model routing) and `config/litellm.yaml`
(the OpenAI-compatible proxy's model list), plus the client configs. They are overwritten on every
`bob gen`: do not edit them by hand. The first run also generates the LiteLLM key (`sk-bob-...`, kept in
`data/secrets.json`); every config that carries it is written mode 0600.

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
SHA256 pinned in the registry. Downloads are resumable; re-run if interrupted.

```bash
bob fetch                    # download the active profile (~18 GB for 16gb, ~44 GB for 12gb)
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

## 10. Wire the editor clients (Continue, optional aider)

This points VS Code's Continue extension at the repo's generated config (symlink, with a copy fallback
where symlinks aren't permitted). The kernel does this in `setup_clients`. By hand:

Linux:
```bash
bob gen                                                    # regenerates config/continue/config.yaml too
mkdir -p ~/.continue
ln -sf "$(pwd)/config/continue/config.yaml" ~/.continue/config.yaml
```

Windows (symlinks need Developer Mode or admin; otherwise copy):
```bat
if not exist "%USERPROFILE%\.continue" mkdir "%USERPROFILE%\.continue"
mklink "%USERPROFILE%\.continue\config.yaml" "C:\bob\config\continue\config.yaml"
:: fallback if mklink is not permitted:
::   copy "C:\bob\config\continue\config.yaml" "%USERPROFILE%\.continue\config.yaml"
```

aider needs no home-dir wiring: after the opt-in venv (step 6, or `bob aider-setup`), `bob gen` writes
`config/aider/.aider.conf.yml` and `bob aider` passes it with `--config`. Nothing goes in
`~/.aider.conf.yml`.

Install the VS Code extensions (same on every OS):
```bash
code --install-extension Continue.continue
code --install-extension saoudrizwan.claude-dev    # Cline
```

---

## 11. Build and configure fabric (optional)

fabric is a Go binary that runs 250+ named LLM prompt patterns. It is opt-in: `bob fabric-setup` (or
`./setup.sh --with-fabric`) builds it and wires `~/.config/fabric`. The manual equivalent
(`setup_fabric` in `scripts/tools/build.py`):

Linux:
```bash
cd external/fabric
go build -o ../../bin/fabric ./cmd/fabric/
cd ../..

mkdir -p ~/.config/fabric
ln -sf "$(pwd)/external/fabric/data/patterns" ~/.config/fabric/patterns
bin/fabric -l            # lists 250+ patterns
```

Windows:
```bat
cd external\fabric
go build -o ..\..\bin\fabric.exe .\cmd\fabric\
cd ..\..

if not exist "%USERPROFILE%\.config\fabric" mkdir "%USERPROFILE%\.config\fabric"
mklink /D "%USERPROFILE%\.config\fabric\patterns" "C:\bob\external\fabric\data\patterns"
bin\fabric.exe -l
```

Then add Bob as fabric's LiteLLM vendor in `~/.config/fabric/.env`, editing only these lines and keeping
the rest of the file (this is what `bob fabric-setup` does, mode 0600):

```
LITELLM_API_KEY=<Bob's litellm key, the litellmKey entry in data/secrets.json>
LITELLM_API_BASE_URL=http://localhost:8081/v1
DEFAULT_VENDOR=LiteLLM
DEFAULT_MODEL=coder
```

`bob fabric-setup` sets `DEFAULT_VENDOR` and `DEFAULT_MODEL` only when they are unset or still Bob's, so
a default you chose wins. Replace `8081` if you changed `litellmPort` in `config/user.json`.

---

## 12. Voice and vision (faster-whisper + piper)

Optional. `bob setup-voice` fetches the faster-whisper STT model, downloads the piper TTS binary + voice,
and installs the audio deps into `venv-litellm`, which must already exist (step 6).

```bash
bob setup-voice              # fetch STT model + piper voice + audio deps
bob setup-voice --force      # re-download everything
```

It fetches the CTranslate2 STT model into `models/faster-whisper/<size>/` and installs `faster-whisper`;
the server runs under `venv-litellm` on port 8082 (bound to `voiceBindHost`, loopback by default), exposing `POST /inference` and
the OpenAI-compatible `POST /v1/audio/transcriptions`. On an NVIDIA GPU it also installs the CUDA-12
runtime wheels (`nvidia-cublas-cu12`, `nvidia-cudnn-cu12`) so CTranslate2 runs on the GPU; the server
preloads them and falls back to CPU int8 when they are absent or mismatched, so voice works either way.
It extracts piper into `bin/` and drops the voice model into `bin/voices/`. The vision model and its
mmproj download with the profile's models in step 9.

`voice.enabled` and `vision.enabled` (both `true` by default) are real switches: with `voice.enabled`
false, `bob voice` and the shell's `/voice` refuse with a message; with `vision.enabled` false, every
image input is refused.

---

## 13. Optional add-on services (Langfuse, SearXNG, n8n)

Entirely opt-in. A default install is 100% Docker-free and needs none of these; add them only for their
specific capability. Each starts on demand:

- **n8n** (workflow automation) runs **natively** on the Node toolchain (no Docker). Start it with
  `bob services n8n start`.
- **SearXNG** (private metasearch) is a **Docker opt-in**. Web search already works out of the box with
  no Docker via the built-in in-process `ddgs` metasearch provider; SearXNG is the self-hosted
  alternative. `bob services searxng start` runs a guided Docker install (through the package-manager
  seam) if Docker is missing, then `docker compose up`. Port 8888.
- **Langfuse** (LLM tracing dashboard) is a **Docker opt-in**. Tracing is on by default without it: the
  default sink is a local file sink at `logs/traces/<trace_id>.jsonl`, viewed with `bob traces`.
  Langfuse is an upgrade for a hosted dashboard. `bob services langfuse start` brings up Langfuse v3
  (web + worker) with its pinned Postgres, ClickHouse, Redis and MinIO from the compose file. Port 3001,
  login `admin@local.dev` with the generated `langfuseAdminPassword` from `data/secrets.json`.

`bob services <name> start` writes `tools/compose/.env` (ports and the bind address only), creates the
persistent data dirs, writes a default `config/searxng/settings.yml`, installs Docker if needed for the
Docker services, then pulls and starts that service. The service secrets (SearXNG secret, Langfuse
passwords and keys) are generated on first start into `data/secrets.json` and passed in the environment
of `docker compose up`, never written next to the compose file. Manage them with `bob services start|stop|status|logs`.

To drive the Docker opt-ins by hand, install Docker and ensure its daemon is running. `bob services
<name> start` is the supported path, because `docker compose up` needs the generated secrets in its
environment (`SEARXNG_SECRET`, `LANGFUSE_*`); by hand you must export them from `data/secrets.json`
first:

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

The compose file reads the repo path, bind address and ports from `tools/compose/.env` (defaults:
`BIND_HOST=127.0.0.1`, Langfuse `3001`, SearXNG `8888`), which `bob services <name> start` prepares. To
create it by hand:
```bash
printf 'REPO_PATH=%s\nBIND_HOST=127.0.0.1\nLANGFUSE_PORT=3001\nSEARXNG_PORT=8888\n' \
    "$(pwd)" > tools/compose/.env
```

Once up:

- **Langfuse**: http://localhost:3001 (login `admin@local.dev`, password: the `langfuseAdminPassword` entry in `data/secrets.json`)
- **SearXNG**: http://localhost:8888
- **n8n**: http://localhost:5678

Verify and manage:
```bash
bob services status          # service names, state, uptime
bob services logs            # tail all service logs
bob services stop            # stop services (data is preserved)
```

> **Windows / Docker Desktop (SearXNG and Langfuse only):** disable the containerd snapshotter before
> pulling images (Settings, General, uncheck "Use containerd for pulling and storing images", then
> Apply & Restart), otherwise SearXNG fails with `exec format error`. After installing Docker Desktop,
> log out of Windows and back in so the `docker-users` group membership takes effect.

---

## 14. Verify the installation

First confirm the install is complete and correct. `python -m bob.kernel verify-install` checks the
installed submodules and downloaded model SHAs against `versions.lock` (the one-command installer runs
this automatically at the end):
```bash
python -m bob.kernel verify-install
```

Then run these in order; each exercises a different part of the stack.

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

# 6. Optional add-on services, if you opted into any (step 13)
bob services status
```

You don't need to keep `bob serve` running for everyday use: inference **auto-starts on demand** the
first time you talk to Bob (`bob`, `bob chat`, `bob agent …`). `bob serve` (foreground) and `bob up`
(background) are for when you want the stack pre-warmed or serving outside-terminal clients.

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
| `llama-swap` missing after setup | the pinned release download failed and Go is absent | Re-run with network access, or install Go and run `bob build --from-source` |
| cmake fails: `unsupported Microsoft Visual Studio version` | MSVC newer than CUDA supports | Add `-DCMAKE_CUDA_FLAGS="-allow-unsupported-compiler"`, or install MSVC v14.4x |
| nvcc errors about host compiler being too new (Linux) | default `g++` newer than nvcc accepts | Set `NVCC_CCBIN` / `-DCMAKE_CUDA_HOST_COMPILER` to an older g++ |
| `llama-server` crashes immediately (Windows) | CUDA DLLs not staged into `bin\` | Re-copy `cublas64_12.dll`, `cublasLt64_12.dll`, `cudart64_12.dll` (step 4) |
| `pip install` fails in a venv | wrong Python | Confirm the venv's Python is 3.11/3.12, not the system default |
| `bob` not found after step 7 | PATH not refreshed | Open a new terminal; ensure `~/.local/bin` (or the shim dir) is on PATH |
| `bob gen`/`bob fetch` error importing deps | `venv-litellm` missing | Build `venv-litellm` first (step 6), the CLI runs under it |
| `bench` shows ~1000 t/s prefill | CPU fallback build | `bob build --force` with `CUDA_PATH` on 12.8+ |
| SearXNG `exec format error` (Windows) | containerd snapshotter enabled | Docker Desktop → uncheck containerd → Apply & Restart |
| Langfuse dashboard shows no traces | tracing still going to the default file sink | Traces default to `logs/traces/*.jsonl` (view with `bob traces`); to send them to Langfuse set `agent.tracing: true` and `agent.tracingSink: otlp` in `config/user.json` (an empty `agent.otlpEndpoint` targets the local Langfuse). See [USAGE § Observability](USAGE.md#observability-file-traces-and-langfuse) |

For alternatives when a build or install won't cooperate (prebuilt binaries, CPU tier, offline models),
see [FALLBACKS.md](FALLBACKS.md).
