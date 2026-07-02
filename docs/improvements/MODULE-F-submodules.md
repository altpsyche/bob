# Module F — Open-Source Submodules & Ecosystem Integrations

**Research basis:** 113-agent deep-research run (2026-06-26), 30 sources fetched, 144 claims
extracted, 25 adversarially verified (2/3 votes required to survive), 16 confirmed, 9 killed.
All claims below are verified. Open questions and research gaps are called out explicitly.

---

## Overview

| Sub | Name | Type | Effort | Depends on |
|-----|------|------|--------|-----------|
| F1 | whisper.cpp | Git submodule (compiled) | 6–8 h | A |
| F2 | Qdrant | Binary download | 3–4 h | — |
| F3 | Hermes model profiles | models.psd1 entries | 1–2 h | A |
| F4 | Tabby | Documented integration | 1 h | — |
| F5 | OpenHands | Documented integration | 1 h | — |

---

## F1 — whisper.cpp (GPU Speech-to-Text + OpenAI Audio Server)

### Why

Same author (ggml-org), same CMake + CUDA build stack, same GGML format as llama.cpp.
The big win: `whisper-server.exe` exposes an **OpenAI-compatible `/v1/audio/transcriptions`
endpoint** — Open WebUI's voice input and any OpenAI audio client work with zero code changes.

### Step 1 — Add submodule

```bash
git submodule add https://github.com/ggml-org/whisper.cpp external/whisper.cpp
git submodule update --init external/whisper.cpp
```

`.gitmodules` entry (auto-created):
```
[submodule "external/whisper.cpp"]
    path = external/whisper.cpp
    url = https://github.com/ggml-org/whisper.cpp
```

### Step 2 — `scripts/build-whisper.ps1`

```powershell
#Requires -Version 7.0
param([switch]$Force)
. "$PSScriptRoot\_models.ps1"
$repo     = Split-Path $PSScriptRoot
$src      = Join-Path $repo 'external\whisper.cpp'
$buildDir = Join-Path $src 'build'
$binDir   = Join-Path $repo 'bin'

if (-not (Test-Path $src)) {
    throw "external/whisper.cpp not found. Run: git submodule update --init external/whisper.cpp"
}

$whisperExe = Join-Path $binDir 'whisper-server.exe'
if ((Test-Path $whisperExe) -and -not $Force) {
    Write-Host "whisper-server.exe already built. Use -Force to rebuild."; return
}

$arch = Get-GpuArch
if ($arch) {
    $cudaRoot = Get-BestCudaRoot -CudaArch $arch.CudaArch
    $cudaArch = $arch.CudaArch
    Write-Host "GPU: $($arch.Gen) (arch $cudaArch)"
} else {
    Write-Warning "No GPU detected — building CPU-only whisper."
    $cudaRoot = $null; $cudaArch = $null
}

if ($Force -and (Test-Path $buildDir)) {
    Remove-Item $buildDir -Recurse -Force
}
New-Item -ItemType Directory $buildDir -Force | Out-Null

$cmakeArgs = @(
    '..', '-G', 'Visual Studio 17 2022', '-A', 'x64',
    '-DCMAKE_BUILD_TYPE=Release',
    '-DBUILD_SHARED_LIBS=OFF',
    '-DWHISPER_BUILD_TESTS=OFF',
    '-DWHISPER_BUILD_EXAMPLES=ON'
)
if ($cudaArch) {
    $env:CUDA_PATH = $cudaRoot
    $cmakeArgs += @(
        '-DGGML_CUDA=ON',
        "-DCMAKE_CUDA_ARCHITECTURES=$cudaArch",
        "-DCMAKE_CUDA_COMPILER=`"$cudaRoot\bin\nvcc.exe`""
    )
} else {
    $cmakeArgs += '-DGGML_CUDA=OFF'
}

Push-Location $buildDir
try {
    cmake @cmakeArgs; if ($LASTEXITCODE -ne 0) { throw "CMake configure failed" }
    cmake --build . --config Release --target whisper-server whisper-cli -j
    if ($LASTEXITCODE -ne 0) { throw "Build failed" }
} finally { Pop-Location }

# whisper.cpp sets CMAKE_RUNTIME_OUTPUT_DIRECTORY = build/bin; Release config appends /Release.
# Fallback: some versions place examples in per-target subdirectories instead.
$releaseBin = Join-Path $buildDir 'bin\Release'
foreach ($exe in @('whisper-server.exe', 'whisper-cli.exe')) {
    $s = Join-Path $releaseBin $exe
    if (-not (Test-Path $s)) {
        $found = Get-ChildItem $buildDir -Filter $exe -Recurse -ErrorAction SilentlyContinue |
                 Select-Object -First 1
        $s = if ($found) { $found.FullName } else { $null }
    }
    if ($s -and (Test-Path $s)) {
        Copy-Item $s (Join-Path $binDir $exe) -Force
        Write-Host "Staged: bin\$exe"
    } else {
        Write-Warning "$exe not found in build output — check $buildDir manually"
    }
}
Write-Host "`nwhisper.cpp build complete."
```

### Step 3 — Whisper model in `config/models.psd1`

Add a top-level `global` section (alongside `profiles`):

```powershell
global = @{
    whisper = @{
        repo   = 'ggerganov/whisper.cpp'   # model files live here, not ggml-org/whisper.cpp
        path   = 'ggml-large-v3-turbo.bin' # HF repo path — no 'models/' prefix
        gguf   = 'whisper-large-v3-turbo.bin'
        sizeGB = 0.8
    }
}
```

`gen-llama-swap.ps1` ignores `global` — whisper bypasses llama-swap entirely.
`fetch-models.ps1` downloads global models after profile models.

**Model size options:**
| Model | Size | Recommended for |
|-------|------|----------------|
| ggml-tiny.en.bin | 75 MB | Real-time mic (English only) |
| ggml-base.en.bin | 142 MB | Fast, English |
| ggml-large-v3-turbo.bin | 809 MB | Best quality, all languages |

### Step 4 — `scripts/start-whisper.ps1`

```powershell
#Requires -Version 7.0
param([int]$Port = 8082, [string]$Model = '')
. "$PSScriptRoot\_models.ps1"
$repo = Split-Path $PSScriptRoot
$cfg  = Get-ModelsConfig
if (-not $Model) {
    $Model = Join-Path $repo "models\$(($cfg.global.whisper.gguf) ?? 'whisper-large-v3-turbo.bin')"
}
$whisperSrv = Join-Path $repo 'bin\whisper-server.exe'
if (-not (Test-Path $whisperSrv)) { throw "whisper-server.exe not found. Run: .\scripts\build-whisper.ps1" }
if (-not (Test-Path $Model))       { throw "Whisper model not found: $Model. Run: bob fetch" }
if (Test-PortInUse -Port $Port) {
    Write-Warning "Port $Port in use — whisper-server may already be running."; return
}
Write-Host "Whisper audio transcription server"
Write-Host "  Endpoint: http://localhost:$Port/v1/audio/transcriptions"
Write-Host "  Model:    $Model"
Write-Host "  (Ctrl+C to stop)`n"
& $whisperSrv --model $Model --port $Port --host 127.0.0.1
```

### Step 5 — `scripts/up.ps1` integration

```powershell
param([switch]$NoOpen, [switch]$WithWhisper)
# ... existing launches ...
$whisperExe   = Join-Path $repo 'bin\whisper-server.exe'
$whisperModel = Join-Path $repo "models\$(((Get-ModelsConfig).global.whisper.gguf) ?? 'whisper-large-v3-turbo.bin')"
if ($WithWhisper -or (Test-Path $whisperExe -and Test-Path $whisperModel)) {
    if (Test-PortInUse -Port 8082) {
        Write-Host "  Whisper: already running on :8082"
    } else {
        Start-Process powershell -ArgumentList "-NoProfile -File `"$PSScriptRoot\start-whisper.ps1`""
        Write-Host "  Whisper: http://localhost:8082/v1/audio/transcriptions"
    }
}
if (-not $NoOpen) { Start-Sleep 2; Start-Process "http://localhost:$webuiPort" }
```

### Step 6 — CLI commands (`scripts/llm.ps1`)

```powershell
'transcribe' {
    $file = $rest[0]
    if (-not $file -or -not (Test-Path $file)) { Write-Host "Usage: bob transcribe <audio-file>"; return }
    . "$PSScriptRoot\_models.ps1"
    $repo  = Split-Path $PSScriptRoot
    $model = Join-Path $repo "models\$(((Get-ModelsConfig).global.whisper.gguf) ?? 'whisper-large-v3-turbo.bin')"
    & (Join-Path $repo 'bin\whisper-cli.exe') -m $model -f $file --output-txt
}
'whisper' { & "$PSScriptRoot\start-whisper.ps1" @rest }
'listen'  {
    Write-Host "Recording... Press Enter when done."
    $wav = Join-Path $env:TEMP 'llm-listen.wav'
    if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
        Write-Host "Install ffmpeg for microphone capture: scoop install ffmpeg"; return
    }
    # Start-Process gives us a real Process object so we can Kill() it cleanly.
    # dshow device name 'audio=default' works on most Windows systems; if it fails,
    # run: ffmpeg -list_devices true -f dshow -i dummy 2>&1 | Select-String 'audio'
    $proc = Start-Process ffmpeg `
        -ArgumentList "-y -f dshow -i `"audio=default`" `"$wav`"" `
        -PassThru -NoNewWindow
    Read-Host
    if (-not $proc.HasExited) { $proc.Kill(); $proc.WaitForExit(2000) | Out-Null }
    if (Test-Path $wav) { & "$PSScriptRoot\llm.ps1" transcribe $wav; Remove-Item $wav }
}
```

### Step 7 — Open WebUI voice input

After starting whisper-server, in Open WebUI:
- Settings → Audio → Speech to Text → OpenAI
- API Base URL: `http://localhost:8082/v1`
- API Key: `sk-local`
- Model: `whisper-1`

### Step 8 — `scripts/bootstrap.ps1` integration

```powershell
$whisperExe = Join-Path $repo 'bin\whisper-server.exe'
if (-not (Test-Path $whisperExe)) {
    Write-Host "Building whisper.cpp..."
    & "$PSScriptRoot\build-whisper.ps1"
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "whisper.cpp build failed — voice transcription unavailable. Retry: .\scripts\build-whisper.ps1"
    }
} else { Write-Host "whisper.cpp already built." }
```

---

## F2 — Qdrant (Production Vector Store for RAG)

### Why

Open WebUI's built-in RAG uses ChromaDB (SQLite-backed): no index, linear search, degrades past
~5000 chunks. Qdrant provides HNSW approximate nearest-neighbor index, payload filtering, named
collections, REST + gRPC, and a Windows native binary — no Docker, no Python, no compilation.

### Step 1 — Bootstrap download

```powershell
# In scripts/bootstrap.ps1:
$qdrantExe = Join-Path $repo 'bin\qdrant.exe'
if (-not (Test-Path $qdrantExe)) {
    Write-Host "Downloading Qdrant vector database..."
    $url = 'https://github.com/qdrant/qdrant/releases/latest/download/qdrant-x86_64-pc-windows-msvc.zip'
    $zip = Join-Path $env:TEMP 'qdrant.zip'
    try {
        Invoke-WebRequest $url -OutFile $zip -TimeoutSec 120
        Expand-Archive $zip -DestinationPath "$env:TEMP\qdrant_tmp" -Force
        $exe = Get-ChildItem "$env:TEMP\qdrant_tmp" -Filter 'qdrant.exe' -Recurse | Select-Object -First 1
        if ($exe) { Move-Item $exe.FullName $qdrantExe -Force; Write-Host "Qdrant installed: bin\qdrant.exe" }
        else       { Write-Warning "qdrant.exe not found in archive." }
    } catch { Write-Warning "Failed to download Qdrant: $_" }
    finally { Remove-Item $zip,"$env:TEMP\qdrant_tmp" -Recurse -Force -ErrorAction SilentlyContinue }
} else { Write-Host "Qdrant already installed." }
```

### Step 2 — `scripts/start-qdrant.ps1`

```powershell
#Requires -Version 7.0
param([int]$HttpPort = 6333, [int]$GrpcPort = 6334)
. "$PSScriptRoot\_models.ps1"
$repo      = Split-Path $PSScriptRoot
$qdrantExe = Join-Path $repo 'bin\qdrant.exe'
$dataDir   = Join-Path $repo 'tools\qdrant-data'
$cfgFile   = Join-Path $repo 'config\qdrant.yaml'
if (-not (Test-Path $qdrantExe)) { throw "qdrant.exe not found. Run: .\setup.bat" }
if (Test-PortInUse -Port $HttpPort) { Write-Warning "Port $HttpPort in use."; return }
New-Item -ItemType Directory $dataDir -Force | Out-Null
@"
storage:
  storage_path: "$($dataDir -replace '\\','/')"
service:
  host: 127.0.0.1
  http_port: $HttpPort
  grpc_port: $GrpcPort
"@ | Set-Content $cfgFile
Write-Host "Qdrant  REST: http://localhost:$HttpPort"
Write-Host "        Dashboard: http://localhost:$HttpPort/dashboard"
& $qdrantExe --config-path $cfgFile
```

### Step 3 — `.gitignore` additions

```
tools/qdrant-data/
config/qdrant.yaml
```

### Step 4 — CLI + `up.ps1`

```powershell
# llm.ps1:
'qdrant' { & "$PSScriptRoot\start-qdrant.ps1" @rest }

# up.ps1 (add -WithQdrant switch):
param([switch]$NoOpen, [switch]$WithWhisper, [switch]$WithQdrant)
if ($WithQdrant -and (Test-Path (Join-Path $repo 'bin\qdrant.exe'))) {
    if (Test-PortInUse -Port 6333) { Write-Host "  Qdrant: already running on :6333" }
    else {
        Start-Process powershell -ArgumentList "-NoProfile -File `"$PSScriptRoot\start-qdrant.ps1`""
        Write-Host "  Qdrant: http://localhost:6333"
    }
}
```

### Open WebUI wiring

Settings → Documents → Vector Database → Qdrant → URL: `http://localhost:6333`
bge-m3 produces 1024-dimensional vectors; Qdrant auto-detects on first insert.

---

## F3 — Hermes Model Profiles (Verified: NousResearch, 2026-06-26)

### What is Hermes

NousResearch Hermes is a fine-tuning series on top of Llama (and later Qwen) base models.
Its defining feature: **purpose-built function calling** using single-token XML delimiters
(`<tools>`, `<tool_call>`, `<tool_response>`) added to the tokenizer vocabulary, enabling
reliable streaming and parsing in agentic pipelines without fragile regex matching.

**Verified characteristics (primary sources, 3-0 or 2-1 vote):**
- ChatML prompt format (`<|im_start|>` / `<|im_end|>`)
- JSON tool call payloads inside `<tool_call>...</tool_call>` XML blocks
- Special tokens added to vocab for streaming-safe parsing
- GGUF quants hosted on HuggingFace, fully llama.cpp compatible
- Reference inference library: `github.com/NousResearch/Hermes-Function-Calling`

**Refuted claims (killed 0-3 in adversarial verification — do not rely on):**
- "Hermes 3 is competitive with/superior to Llama 3.1 Instruct on standard evals" — FALSE
- "Hermes 3 405B is state-of-the-art among open models" — FALSE
- "Hermes 2 Pro scores 84% on a structured JSON eval" — FALSE (90% figure is vendor-internal only)

**Honest assessment:** Hermes is not a general-capability upgrade over Llama 3.1 Instruct.
Its value is the **dedicated function-calling training and tokenizer tokens** — for agentic
pipelines that parse tool calls from streamed output, Hermes is reliably better structured
than models that weren't specifically trained for it. Use it as a dedicated `agent` role,
not as a replacement for general-purpose planner/coder/chat.

### F3a — Hermes 3 8B as `agent` role (function calling specialist)

Add to VRAM profiles **12 GB+** only. On 8 GB: agent (4.92 GB) + fim (3.4 GB) + embed (0.6 GB) = 8.92 GB
before any big model — exceeds available VRAM. At 12 GB+, agent fits comfortably alongside fim + embed:

```powershell
# In each profile (8gb, 12gb, 16gb, 24gb, 32gb):
agent = @{
    repo      = 'NousResearch/Hermes-3-Llama-3.1-8B-GGUF'
    path      = 'Hermes-3-Llama-3.1-8B-Q4_K_M.gguf'
    gguf      = 'hermes-3-8b-q4_k_m.gguf'
    ctx       = 8192
    kv        = $true
    sizeGB    = 4.92
    # ChatML format — explicitly set temperature via flags
    flags     = @('--temp', '0.1')   # Low temp for reliable tool-call JSON
    # NOTE: Hermes uses ChatML, not Qwen3 format. No /no_think needed.
    # setParams intentionally omitted — let client control sampling for agentic use
}
```

**Group membership:** Add `agent` to the swap group members alongside planner/coder/chat:

```powershell
group = @{
    name    = 'ondemand'
    swap    = $true
    members = @('planner', 'coder', 'chat', 'agent')
}
```

**VRAM on 16 GB (agent active):** agent (4.92 GB) + fim (3.4 GB) + embed (0.6 GB) = 8.92 GB.
Leaves ~7 GB for KV cache at 8192 ctx. Comfortable.

**Update `_models.ps1` role order:**
```powershell
@('planner', 'coder', 'chat', 'agent', 'vision', 'fim', 'embed')
```

### F3b — Hermes 4.3-36B as `planner` alternative (24 GB+ profiles)

**Verified (3-0):** Hermes 4.3-36B is a hybrid reasoning model producing `<think>...</think>`
traces before tool calls. Q4_K_M = 21.76 GB (fits 24 GB profile), Q5_K_M = 25.59 GB (fits 32 GB).

This is an **alternative** to Qwen3-30B-A3B for the planner role in 24 GB+ profiles.
Trade-off: Hermes 4.3-36B has stronger agentic/tool-calling structure; Qwen3-30B-A3B
has stronger general reasoning benchmarks. Choose based on use case.

Add as a named alternative profile `'hermes-24gb'` in `models.psd1`:

```powershell
'hermes-24gb' = @{
    _targetVRAM = '24gb'
    _notes      = 'Hermes-focused profile. agent=Hermes 4.3-36B (stronger tool calling). Swap group includes all big models.'

    # Hermes 4.3-36B: hybrid reasoning + tool calling. Q4_K_M=21.76GB.
    planner = @{
        repo   = 'NousResearch/Hermes-4.3-36B-GGUF'
        path   = 'Hermes-4.3-36B-Q4_K_M.gguf'
        gguf   = 'hermes-4.3-36b-q4_k_m.gguf'
        ctx    = 16384
        kv     = $true
        sizeGB = 21.76
        flags  = @('--temp', '0.3')
        # Produces <think>...</think> before responding — similar to Qwen3 /no_think pattern.
        # To suppress thinking: add 'thinking_mode: off' in system prompt (model-specific behavior).
    }

    # Hermes 3 8B as dedicated agent/tool-use role
    agent = @{
        repo   = 'NousResearch/Hermes-3-Llama-3.1-8B-GGUF'
        path   = 'Hermes-3-Llama-3.1-8B-Q4_K_M.gguf'
        gguf   = 'hermes-3-8b-q4_k_m.gguf'
        ctx    = 8192
        kv     = $true
        sizeGB = 4.92
        flags  = @('--temp', '0.1')
    }

    # Coder, chat, fim, embed: identical to standard 24gb profile (from MODULE-C)
    coder = @{
        repo   = 'bartowski/Qwen2.5-Coder-14B-Instruct-GGUF'
        path   = 'Qwen2.5-Coder-14B-Instruct-Q6_K.gguf'
        gguf   = 'qwen-coder-14b-q6_k.gguf'
        ctx    = 16384
        kv     = $true
        sizeGB = 10.7
    }
    chat = @{
        repo   = 'bartowski/Qwen3-14B-GGUF'
        path   = 'Qwen3-14B-Q6_K.gguf'
        gguf   = 'qwen3-14b-q6_k.gguf'
        ctx    = 16384
        kv     = $true
        sizeGB = 11.0
    }
    fim = @{
        repo   = 'bartowski/Qwen2.5-Coder-3B-Instruct-GGUF'
        path   = 'Qwen2.5-Coder-3B-Instruct-Q8_0.gguf'
        gguf   = 'qwen-coder-3b-q8_0.gguf'
        ctx    = 8192
        kv     = $false
        mlock  = $true
        sizeGB = 3.4
    }
    embed = @{
        repo   = 'gpustack/bge-m3-GGUF'
        path   = 'bge-m3-Q8_0.gguf'
        gguf   = 'bge-m3-q8_0.gguf'
        ctx    = 8192
        kv     = $false
        mlock  = $true
        sizeGB = 0.6
    }
}
```

### F3c — ChatML format documentation for agentic clients

Add to `docs/USAGE.md` (use `~~~` fences inside the markdown file to avoid nesting issues):

~~~markdown
## Hermes Agent Role (Function Calling)

The `agent` model (Hermes-3-Llama-3.1-8B) is purpose-built for function calling and
agentic pipelines. Unlike Qwen3 models, it uses **ChatML format** and emits tool calls
as single-token XML delimiters for streaming-safe parsing.

**Direct API tool call example:**

    {
      "model": "agent",
      "messages": [
        {"role": "system", "content": "You are a helpful assistant with access to tools."},
        {"role": "user", "content": "What files are in the current directory?"}
      ],
      "tools": [{
        "type": "function",
        "function": {
          "name": "list_directory",
          "description": "List files in a directory",
          "parameters": {
            "type": "object",
            "properties": {
              "path": {"type": "string", "description": "Directory path"}
            },
            "required": ["path"]
          }
        }
      }]
    }

The model responds with:

    <tool_call>{"name": "list_directory", "arguments": {"path": "."}}</tool_call>

**In aider:** aider uses its own tool protocol — point at `coder` role, not `agent`.
**In OpenHands:** configure with model `agent` (see F6).
**Note:** No `/no_think` needed for Hermes — it doesn't run a scratchpad by default.
~~~

---

## F4 — Tabby (TabbyML) — Code Completion Alternative

### What it is (verified, 3-0 vote)

`TabbyML/tabby` (Apache 2.0, ~30k+ stars) is a self-hosted code completion server.
It provides a **native Windows executable** (`tabby_x86_64-windows-msvc.zip` → `tabby.exe`)
with no Docker requirement.

**Key distinction from this stack's FIM role:**
- This stack uses llama-swap + llama-server for FIM (tab-complete via Continue.dev)
- Tabby is an **alternative** approach: separate server, own model management, VS Code extension
- Trade-off: Tabby has a dedicated VS Code extension with richer UX (accept/reject per-line);
  Continue.dev FIM is simpler but integrated with our existing llama-swap stack

**Open question (not verified in research):** Whether Tabby can be configured to use
an external OpenAI-compatible FIM endpoint (e.g., our llama-swap's `fim` role) rather
than its own model backend. If yes, it would give us Tabby's UX with our existing fim model.
Check: `https://tabby.tabbyml.com/docs/configuration`

### Integration approach

Tabby is best treated as a **documented complement** (not a submodule), because:
1. It has its own model download and serving pipeline
2. It conflicts with Continue.dev autocomplete if both are active in VS Code
3. The CUDA version requirement for Windows GPU build is unverified (research refuted the specific claim)

**If user wants to try Tabby:**

```powershell
# Download native Windows exe
$url = 'https://github.com/TabbyML/tabby/releases/latest/download/tabby_x86_64-windows-msvc.zip'
Invoke-WebRequest $url -OutFile tabby.zip
Expand-Archive tabby.zip -DestinationPath $env:USERPROFILE\tabby
# Add $env:USERPROFILE\tabby to PATH or run directly

# Start Tabby on port 8088 — avoids conflict with llama-swap (:8080)
tabby.exe serve --model TabbyML/DeepseekCoder-6.7B --port 8088

# VS Code: install Tabby extension → Settings → Server Endpoint: http://localhost:8088
# NOTE: disable Continue.dev autocomplete while testing Tabby — both hooks the same keypress
```

Add to `docs/FALLBACKS.md` as an alternative to Continue.dev FIM.

---

## F5 — OpenHands (Agentic Coding Framework)

### What it is (verified, 3-0 vote)

`All-Hands-AI/OpenHands` (MIT, ~50k+ stars) is the leading open-source agentic coding
framework — it can browse the web, edit files, run tests, and submit PRs autonomously.

**Verified (3-0):** OpenHands currently recommends **Qwen3.6-35B-A3B** (a 35B MoE model with
3B active params, 262K context) as the best local model — which is the same family as this
stack's planner model (Qwen3-30B-A3B). The fit is excellent.

**Verified (3-0):** OpenHands supports OpenAI-compatible endpoints via Base URL + `openai/<model>`
prefix pattern, compatible with llama-swap at `localhost:8080`.

**Verified (3-0):** Warning from official docs: "local models — even recommended ones — may have
limited functionality due to unreliable tool use." Expect degraded reliability vs cloud APIs.

### Windows reality

OpenHands primarily deploys via Docker on Windows. A native Windows path exists but is less
well-documented. For this stack, treat it as a **documented integration** (not a submodule).

### Configuration

```powershell
# If using Docker Desktop:
# Check latest runtime tag at: https://github.com/All-Hands-AI/OpenHands/releases
docker pull ghcr.io/all-hands-ai/openhands:main
docker run -it `
  -e SANDBOX_RUNTIME_CONTAINER_IMAGE=docker.all-hands.ai/all-hands-ai/runtime:latest `
  -v "${HOME}\openhands-state:/root/.openhands" `
  -p 3001:3000 `
  ghcr.io/all-hands-ai/openhands:main
# NOTE: --add-host host.docker.internal:host-gateway is Linux-only.
# On Windows Docker Desktop, host.docker.internal resolves automatically — omit the flag.

# Then in OpenHands UI:
# bob Provider: OpenAI
# bob Base URL: http://host.docker.internal:8080/v1  (resolves to host on Windows Docker Desktop)
# Model: openai/planner
# API Key: sk-local
```

**Without Docker (experimental):**
```powershell
pip install openhands-ai
python -m openhands.core.main --llm-base-url http://localhost:8080/v1 --llm-model openai/planner
```

### `docs/USAGE.md` addition

~~~markdown
## OpenHands (Agentic Coding)

OpenHands is an autonomous coding agent that can read/write files, run tests, and commit code.
It works best with frontier models but is usable locally with the planner model.

**Start OpenHands (Docker Desktop required on Windows):**

    bob serve    # Start local endpoint first
    # Then run Docker command from docs/improvements/MODULE-F-submodules.md F6

**Point at local endpoint in OpenHands UI:**
- bob Provider: OpenAI
- bob Base URL: `http://host.docker.internal:8080/v1`
- Model: `openai/planner` (or `openai/agent` for Hermes tool-calling model)
- API Key: `sk-local`

**Expectation setting:** OpenHands with local models will occasionally fail tool calls or
loop on errors. Use it for well-scoped, single-file tasks. For complex multi-file refactors,
cloud APIs are more reliable.
~~~

---

## Research Gaps (Not Verified — Investigate Separately)

The following areas were researched but produced no claims surviving adversarial verification.
This does NOT mean they're bad — it means the research pass didn't find primary-source evidence
strong enough to survive 2-of-3 adversarial votes. Treat as "check before depending on":

| Tool | Gap | What to check |
|------|-----|--------------|
| **docling** (IBM) | Windows-native CUDA support unverified | GitHub releases page for Windows binary |
| **marker** | Windows support unclear | Docs/issues for Windows install |
| **Unstructured** | API vs Docker deployment on Windows | unstructured.io docs |
| **Perplexica/SearXNG** | Windows-native vs Docker-only | GitHub README |
| **mem0** | OpenAI-compatible API surface unclear | mem0.ai docs |
| **MemGPT/Letta** | Windows native unclear | docs.letta.com |
| **stable-diffusion.cpp** | GPU support on Windows unverified | GitHub releases |
| **ComfyUI** | Windows installer quality (1 unreliable source found) | comfy.org official docs |
| **AutoGen, CrewAI** | Windows native vs Docker unclear | GitHub README per project |
| **n8n, Flowise, Dify** | n8n confirmed to work on Windows without Docker (blog source only, not verified) | Official install docs |

**Highest-value gaps to resolve:** docling (document processing is high-value for RAG),
ComfyUI (image generation pairs well with vision model in Module G), n8n (workflow automation).

---

## Updated `scripts/setup-clients.ps1`

After the existing symlink/copy code, also call `setup-fabric.ps1` if fabric is installed:

```powershell
$fabricCmd = Get-Command fabric -ErrorAction SilentlyContinue
if ($fabricCmd) {
    Write-Host "Configuring fabric for local endpoint..."
    & "$PSScriptRoot\setup-fabric.ps1"
}
```

---

## Summary: New Files

| File | Type |
|------|------|
| `external/whisper.cpp` | Git submodule |
| `scripts/build-whisper.ps1` | New script |
| `scripts/start-whisper.ps1` | New script |
| `scripts/start-qdrant.ps1` | New script |
| `scripts/setup-fabric.ps1` | New script |

## Summary: Modified Files

| File | Changes |
|------|---------|
| `.gitmodules` | Add whisper.cpp submodule |
| `config/models.psd1` | Add `global.whisper`; add `agent` role to all profiles; add `hermes-24gb` profile |
| `scripts/_models.ps1` | Add `agent` and `vision` to stable role order |
| `scripts/gen-llama-swap.ps1` | Handle `mmproj` (from Module G if implemented) |
| `scripts/bootstrap.ps1` | Build whisper, download Qdrant |
| `scripts/up.ps1` | Launch whisper, Qdrant optionally |
| `scripts/llm.ps1` | Add transcribe, listen, whisper, qdrant, fabric-setup commands |
| `scripts/setup-clients.ps1` | Auto-configure fabric if installed |
| `docs/USAGE.md` | Hermes agent role, fabric patterns, OpenHands, whisper, Qdrant |
| `docs/FALLBACKS.md` | Tabby as Continue.dev FIM alternative |
| `.gitignore` | qdrant-data/, config/qdrant.yaml |

## Total Estimated Effort

| Sub | Hours |
|-----|-------|
| F1 whisper.cpp | 6–8 |
| F2 Qdrant | 3–4 |
| F3 Hermes profiles | 1–2 |
| F4 fabric | 2–3 |
| F5 Tabby (docs only) | 1 |
| F6 OpenHands (docs only) | 1 |
| **Total** | **14–19 hours** |
