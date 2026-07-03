#requires -Version 7
# One-shot setup for Phase 2 voice + vision.
# Downloads: whisper model, piper binary + voice model, Qwen2-VL mmproj.
# Builds: whisper-server.exe (via build-whisper.ps1) if not present.
# Installs: sounddevice + numpy into tools/venv-litellm.
# Usage: bob setup-voice
param([switch]$Force)  # re-download even if files already exist
$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
. "$PSScriptRoot\_models.ps1"

$bobCfg   = Get-BobConfig
$sttModel = $bobCfg.voice.sttModel ?? 'small'          # e.g. 'small', 'base.en', 'medium'
$ttsVoice = $bobCfg.voice.ttsVoice ?? 'en_GB-alan-medium'  # e.g. 'en_GB-alan-medium'

# Derive piper voice HF URL from voice name.
# Voice name format: {lang}_{REGION}-{name}-{quality}  e.g. en_GB-alan-medium
# HF path: rhasspy/piper-voices / v1.0.0 / {lang} / {lang}_{REGION} / {name} / {quality} / {file}
$voiceParts = $ttsVoice -split '-'   # ['en_GB', 'alan', 'medium']
$voiceLang  = ($voiceParts[0] -split '_')[0]   # 'en'
$voiceBase  = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/$voiceLang/$($voiceParts[0])/$($voiceParts[1])/$($voiceParts[2])/$ttsVoice"

$urls = @{
    # whisper model from ggerganov's official HF repo — config-driven
    whisperModel    = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-$sttModel.bin"
    # piper Windows AMD64 release (2023-11-14-2 is the stable release)
    piperZip        = 'https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_windows_amd64.zip'
    # piper voice model — config-driven (URL derived from voice name)
    piperVoice      = "$voiceBase.onnx"
    piperVoiceJson  = "$voiceBase.onnx.json"
}

function Download-File($url, $dest, $label) {
    if ((Test-Path $dest) -and -not $Force) {
        Write-Host "  $label already present — skipping." -ForegroundColor DarkGray
        return
    }
    Write-Host "  Downloading $label..." -ForegroundColor Cyan
    $parent = Split-Path $dest -Parent
    if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Force $parent | Out-Null }
    Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
    Write-Host "  Saved: $dest" -ForegroundColor Green
}

# ── Step 1: build whisper-server.exe if missing ──────────────────────────────
Write-Host "`n[1/5] whisper-server.exe" -ForegroundColor Yellow
$serverExe = Join-Path $repo 'bin\whisper-server.exe'
if ($Force -or -not (Test-Path $serverExe)) {
    Write-Host "  Building whisper.cpp..." -ForegroundColor Cyan
    & "$PSScriptRoot\build-whisper.ps1" $(if ($Force) { '-Force' })
} else {
    Write-Host "  whisper-server.exe already built — skipping." -ForegroundColor DarkGray
}

# ── Step 2: download whisper model (config-driven) ───────────────────────────
Write-Host "`n[2/5] Whisper model (ggml-$sttModel.bin)" -ForegroundColor Yellow
$whisperDir  = Join-Path $repo 'models\whisper'
$whisperFile = "ggml-$sttModel.bin"
New-Item -ItemType Directory -Force $whisperDir | Out-Null
Download-File $urls.whisperModel (Join-Path $whisperDir $whisperFile) $whisperFile

# ── Step 3: download piper binary + voice model ───────────────────────────────
Write-Host "`n[3/5] Piper TTS binary + voice model" -ForegroundColor Yellow
$voicesDir = Join-Path $repo 'bin\voices'
New-Item -ItemType Directory -Force $voicesDir | Out-Null
$piperExe  = Join-Path $repo 'bin\piper.exe'
if ($Force -or -not (Test-Path $piperExe)) {
    Write-Host "  Downloading piper Windows release zip..." -ForegroundColor Cyan
    $piperZip = [System.IO.Path]::GetTempFileName() + '.zip'
    Invoke-WebRequest -Uri $urls.piperZip -OutFile $piperZip -UseBasicParsing
    $piperTmp = [System.IO.Path]::GetTempPath() + 'piper_extract'
    Expand-Archive -Path $piperZip -DestinationPath $piperTmp -Force
    # The zip contains a piper/ folder with piper.exe inside
    $extracted = Get-ChildItem $piperTmp -Recurse -Filter 'piper.exe' | Select-Object -First 1
    if (-not $extracted) { throw "piper.exe not found in extracted zip at $piperTmp" }
    Copy-Item $extracted.FullName $piperExe -Force
    # Copy DLLs piper needs (same folder as piper.exe)
    Copy-Item (Join-Path $extracted.DirectoryName '*.dll') (Join-Path $repo 'bin') -Force -ErrorAction SilentlyContinue
    # Copy espeak-ng-data/ directory (required for phonemization; piper looks here by default)
    $piperDir = $extracted.DirectoryName
    $espeakSrc = Join-Path $piperDir 'espeak-ng-data'
    if (Test-Path $espeakSrc) {
        $espeakDest = Join-Path $repo 'bin\espeak-ng-data'
        Copy-Item $espeakSrc $espeakDest -Recurse -Force
        Write-Host "  espeak-ng-data/ copied to bin/" -ForegroundColor Green
    } else {
        Write-Warning "espeak-ng-data not found in piper zip — TTS phonemization may fail"
    }
    Remove-Item $piperZip, $piperTmp -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "  piper.exe extracted to bin/" -ForegroundColor Green
} else {
    Write-Host "  piper.exe already present — skipping." -ForegroundColor DarkGray
}
Download-File $urls.piperVoice     (Join-Path $voicesDir "$ttsVoice.onnx")      "$ttsVoice.onnx"
Download-File $urls.piperVoiceJson (Join-Path $voicesDir "$ttsVoice.onnx.json") "$ttsVoice.onnx.json"

# ── Step 4: install Python audio deps into venv-litellm ──────────────────────
Write-Host "`n[4/5] Python audio deps (sounddevice, numpy)" -ForegroundColor Yellow
$pip = Get-VenvExe -Venv 'venv-litellm' -Exe 'pip'   # NC4: OS-aware (Scripts\pip.exe | bin/pip)
if (-not (Test-Path $pip)) { throw "venv-litellm not found — run: bob bootstrap first" }
& $pip install --quiet sounddevice numpy
if ($LASTEXITCODE -ne 0) { throw "pip install sounddevice numpy failed" }
Write-Host "  sounddevice + numpy installed." -ForegroundColor Green

# ── Step 5: smoke test ────────────────────────────────────────────────────────
Write-Host "`n[5/5] Smoke test (start whisper-server, POST silence WAV)" -ForegroundColor Yellow
$sttPort = $bobCfg.voice.sttPort ?? (Get-BobPortDefault 'sttPort')

# Create minimal 2-second silent WAV (44100 Hz mono 16-bit)
$sampleRate = 44100; $seconds = 2; $numSamples = $sampleRate * $seconds
$wavBytes = [System.Collections.Generic.List[byte]]::new()
# RIFF header
$dataSize   = $numSamples * 2   # 16-bit = 2 bytes/sample
$chunkSize  = 36 + $dataSize
foreach ($b in [BitConverter]::GetBytes([int32]$chunkSize))  { $wavBytes.Add($b) }
# Prepend "RIFF" and "WAVE"
$header = [System.Text.Encoding]::ASCII.GetBytes("RIFF") + [BitConverter]::GetBytes([int32]$chunkSize) +
          [System.Text.Encoding]::ASCII.GetBytes("WAVE") +
          [System.Text.Encoding]::ASCII.GetBytes("fmt ") +
          [BitConverter]::GetBytes([int32]16) +       # subchunk1 size
          [BitConverter]::GetBytes([int16]1) +        # PCM
          [BitConverter]::GetBytes([int16]1) +        # mono
          [BitConverter]::GetBytes([int32]$sampleRate) +
          [BitConverter]::GetBytes([int32]($sampleRate * 2)) +  # byte rate
          [BitConverter]::GetBytes([int16]2) +        # block align
          [BitConverter]::GetBytes([int16]16) +       # bits/sample
          [System.Text.Encoding]::ASCII.GetBytes("data") +
          [BitConverter]::GetBytes([int32]$dataSize) +
          [byte[]]::new($dataSize)                    # silence
$silenceWav = [System.IO.Path]::GetTempFileName() + '.wav'
[System.IO.File]::WriteAllBytes($silenceWav, $header)

# Start whisper-server in background for the test
& "$PSScriptRoot\start-whisper.ps1" -NoWindow
try {
    $result = Invoke-RestMethod -Uri "http://localhost:$sttPort/inference" -Method Post `
        -Form @{ file = Get-Item $silenceWav; temperature = '0.0'; response_format = 'json' } `
        -ErrorAction Stop
    if ($null -ne $result.text) {
        Write-Host "  Smoke test passed. Transcript of silence: '$($result.text)'" -ForegroundColor Green
    } else {
        Write-Warning "Whisper responded but .text field missing — server may be working anyway"
    }
} catch {
    Write-Warning "Smoke test failed: $_"
    Write-Warning "whisper-server may need more time to start. Try: bob transcribe <file>"
} finally {
    Remove-Item $silenceWav -ErrorAction SilentlyContinue
    # Stop the test whisper-server process
    $pidFile = Join-Path $repo 'logs\whisper.pid'
    if (Test-Path $pidFile) {
        $wPid = [int](Get-Content $pidFile -Raw -ErrorAction SilentlyContinue)
        if ($wPid) { Stop-Process -Id $wPid -Force -ErrorAction SilentlyContinue }
        Remove-Item $pidFile -ErrorAction SilentlyContinue
    }
}

Write-Host @"

Voice setup complete.

Next steps:
  1. bob fetch                             Download vision GGUF + mmproj (+ any other missing models)
  2. Flip 'voice.enabled = `$true'         in config/bob.psd1 to auto-start whisper on 'bob up'
  3. Flip 'vision.enabled = `$true'        in config/bob.psd1 to enable vision features
  4. Test:
       bob speak "Hello, I am Bob."
       bob listen
       bob voice
       bob describe <image.png>
"@ -ForegroundColor Cyan
