# Module G — Vision / Multimodal Support

**Depends on:** Module A (config tuneability, port from defaults).
Build pattern is similar to existing llama.cpp setup — no new build dependencies.

## Why Vision

llama.cpp has native multimodal support via `--mmproj` (multi-modal projection).
This allows llama-server to accept image inputs alongside text in the chat completions API.
Zero new dependencies — the same `llama-server.exe` already handles it.

Use cases:
- "Describe this screenshot"
- "Review this architecture diagram"
- "What's in this error window?"
- "Explain this UI layout"
- "Transcribe text from an image" (OCR-like)

## Model Selection

**Qwen2-VL-7B-Instruct** — recommended for this stack:
- 7B parameters fits in the swap group alongside fim + embed on 16 GB VRAM
- Two files: the base model GGUF + a projection model (`mmproj`) for image encoding
- Strong performance on technical images, diagrams, and code screenshots
- GGUF quants available from bartowski on HuggingFace

**Alternative models:**
| Model | VRAM | Quality | Notes |
|-------|------|---------|-------|
| Qwen2-VL-7B Q4_K_M | 4.4 GB + 0.9 GB mmproj | Good | Recommended |
| LLaVA-1.5-7B Q4_K_M | 4.1 GB + 0.7 GB mmproj | OK | Older, less capable |
| MiniCPM-V-2.6 Q4_K_M | 5.0 GB + 0.4 GB mmproj | Very good | Smaller mmproj |
| Qwen2-VL-72B Q4_K_M | ~43 GB | Excellent | 32 GB+ VRAM only |

## Step 1 — `config/models.psd1` Schema Extension

### New `mmproj` field

Add to model entries (vision models only):

```powershell
vision = @{
    repo   = 'bartowski/Qwen2-VL-7B-Instruct-GGUF'
    path   = 'Qwen2-VL-7B-Instruct-Q4_K_M.gguf'
    gguf   = 'qwen2-vl-7b-q4_k_m.gguf'
    ctx    = 8192
    kv     = $true
    sizeGB = 4.4
    mmproj = @{
        repo   = 'bartowski/Qwen2-VL-7B-Instruct-GGUF'
        path   = 'mmproj-Qwen2-VL-7B-Instruct-f16.gguf'
        gguf   = 'qwen2-vl-7b-mmproj-f16.gguf'
        sizeGB = 0.9
    }
}
```

### Profile placement

Add `vision` to 16 GB+ profiles in the swap group:

```powershell
# config/models.psd1 group.members update:
group = @{
    name    = 'ondemand'
    swap    = $true
    members = @('planner', 'coder', 'chat', 'vision')   # vision added to swap group
}
```

**VRAM impact (16 GB profile, vision active):**
- fim (3.4 GB) + embed (0.6 GB) = 4.0 GB always resident
- vision weights: 4.4 GB + mmproj: 0.9 GB = 5.3 GB
- KV cache at 8192 ctx: ~0.5 GB (quantized)
- Total: 9.8 GB — fits on 10 GB+ VRAM. Swaps normally with planner/coder/chat.

Add `vision` to the canonical role order in `_models.ps1`:

```powershell
# Current line 43 in _models.ps1:
@('planner', 'coder', 'chat', 'fim', 'embed')

# Updated:
@('planner', 'coder', 'chat', 'vision', 'fim', 'embed')
```

## Step 2 — `scripts/gen-llama-swap.ps1` — `mmproj` Support

Add inside the cmd-building loop, after `flags` are appended:

```powershell
if ($m.mmproj) {
    $mpPath = '${env.LLAMA_LOCAL_ROOT}/models/' + $m.mmproj.gguf
    $parts += "--mmproj `"$mpPath`""
}
```

The full vision model cmd string will look like:

```
${srv} -m ${env.LLAMA_LOCAL_ROOT}/models/qwen2-vl-7b-q4_k_m.gguf -c 8192 ${kv} --mmproj "${env.LLAMA_LOCAL_ROOT}/models/qwen2-vl-7b-mmproj-f16.gguf"
```

**Note on KV quant and mmproj:** KV quantization works with multimodal models. No special
flag needed — the `kv = $true` field on the vision model is correct.

## Step 3 — `scripts/fetch-models.ps1` — Download `mmproj` Files

Current download loop iterates over `$m.gguf` (the model file). Add mmproj download:

```powershell
foreach ($m in $models.models) {
    # ... existing model download ...

    # Download mmproj file if present
    if ($m.mmproj -and $m.mmproj.repo -and $m.mmproj.path) {
        $mpGguf = $m.mmproj.gguf
        $mpDest = Join-Path $modelsDir $mpGguf
        if (Test-Path $mpDest) {
            Write-Host "  [skip] $mpGguf (already downloaded)"
        } else {
            $mpUrl = "https://huggingface.co/$($m.mmproj.repo)/resolve/main/$($m.mmproj.path)"
            Write-Host "  Downloading mmproj: $mpGguf ($($m.mmproj.sizeGB) GB)..."
            # Same curl.exe download logic as main model
            $mpPart = "$mpDest.part"
            & curl.exe -L -C - -o $mpPart --progress-bar $mpUrl
            if ($LASTEXITCODE -eq 0 -and (Test-Path $mpPart)) {
                Move-Item $mpPart $mpDest -Force
                Write-Host "  Downloaded: $mpGguf"
            } else {
                $failures++
                Write-Warning "Failed to download $mpGguf"
            }
        }
    }
}
```

Also update the dry-run (list-only) output to show mmproj alongside the model:

```powershell
if ($m.mmproj) {
    $totalGB += $m.mmproj.sizeGB
    Write-Host "  $($m.role)/mmproj  $($m.mmproj.gguf)  ($($m.mmproj.sizeGB) GB)"
}
```

## Step 4 — `scripts/llm.ps1` — `bob describe` Command

```powershell
'describe' {
    # Usage: bob describe <image-path> [<prompt>]
    # Default prompt: "Describe this image in detail."
    if ($rest.Count -lt 1) {
        Write-Host "Usage: bob describe <image-path> [prompt]"
        Write-Host "       bob describe screenshot.png"
        Write-Host "       bob describe diagram.jpg 'What does this architecture show?'"
        return
    }

    $imgPath = $rest[0]
    $prompt  = if ($rest.Count -gt 1) { $rest[1..$rest.Count] -join ' ' } else { 'Describe this image in detail.' }

    if (-not (Test-Path $imgPath)) {
        Write-Host "Image not found: $imgPath" -ForegroundColor Red
        return
    }

    . "$PSScriptRoot\_models.ps1"
    $cfg  = Get-ModelsConfig
    $port = $cfg.defaults.port ?? 8080
    $base = "http://localhost:$port/v1"
    $maxTok = $cfg.defaults.maxTokens ?? 512

    # Base64-encode the image
    $bytes  = [System.IO.File]::ReadAllBytes((Resolve-Path $imgPath))
    $b64    = [System.Convert]::ToBase64String($bytes)
    $ext    = [System.IO.Path]::GetExtension($imgPath).TrimStart('.').ToLower()
    $mime   = switch ($ext) {
        'jpg'  { 'image/jpeg' }
        'jpeg' { 'image/jpeg' }
        'png'  { 'image/png' }
        'gif'  { 'image/gif' }
        'webp' { 'image/webp' }
        default { 'image/png' }
    }
    $dataUrl = "data:$mime;base64,$b64"

    $body = @{
        model      = 'vision'
        stream     = $true
        max_tokens = $maxTok
        messages   = @(@{
            role    = 'user'
            content = @(
                @{ type = 'image_url'; image_url = @{ url = $dataUrl } },
                @{ type = 'text'; text = $prompt }
            )
        })
    } | ConvertTo-Json -Depth 10 -Compress

    # Stream response (same SSE streaming as bob chat)
    try {
        curl.exe --no-buffer --silent `
            -X POST "$base/chat/completions" `
            -H 'Content-Type: application/json' `
            -d $body |
        ForEach-Object {
            if ($_ -match '^data: (.+)$') {
                $data = $Matches[1]
                if ($data -ne '[DONE]') {
                    try {
                        $chunk = $data | ConvertFrom-Json
                        $text  = $chunk.choices[0].delta.content
                        if ($text) { Write-Host -NoNewline $text }
                    } catch {}
                }
            }
        }
        Write-Host ""
    } catch {
        Write-Host "describe failed: $_" -ForegroundColor Red
        Write-Host "Is vision model loaded? bob status"
    }
}
```

## Step 5 — `scripts/diagnose.ps1` — Vision Model Check

Add to the model file validation section:

```powershell
# Check mmproj files for vision models
foreach ($m in $models) {
    if ($m.mmproj) {
        $mpPath = Join-Path $modelsDir $m.mmproj.gguf
        if (Test-Path $mpPath) {
            $mpSize = (Get-Item $mpPath).Length / 1GB
            $mpExp  = $m.mmproj.sizeGB
            if ([math]::Abs($mpSize - $mpExp) / $mpExp -gt $SizeTolPct) {
                Write-Host "  $($m.role)/mmproj: SIZE MISMATCH ($([math]::Round($mpSize,2)) vs $mpExp GB expected)" -ForegroundColor Red
                $issues++
            } else {
                Write-Host "  $($m.role)/mmproj: OK ($([math]::Round($mpSize,2)) GB)" -ForegroundColor Green
            }
        } else {
            Write-Host "  $($m.role)/mmproj: MISSING ($($m.mmproj.gguf))" -ForegroundColor Yellow
            $issues++
        }
    }
}
```

## Step 6 — `docs/USAGE.md` — Vision Section

```markdown
## Vision / Image Analysis

The vision model (Qwen2-VL-7B) allows sending images alongside text prompts.
It understands diagrams, screenshots, code, UI layouts, and natural images.

**Describe an image:**
```powershell
bob describe screenshot.png
bob describe diagram.jpg "What services does this architecture show?"
bob describe error.png "What is wrong in this error message?"
```

**Direct API (for programmatic use):**
```powershell
$b64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes("image.png"))
$body = @{
    model = 'vision'
    messages = @(@{
        role = 'user'
        content = @(
            @{ type = 'image_url'; image_url = @{ url = "data:image/png;base64,$b64" } },
            @{ type = 'text'; text = 'Describe this image' }
        )
    })
} | ConvertTo-Json -Depth 10

Invoke-RestMethod http://localhost:8080/v1/chat/completions `
    -Method POST -ContentType 'application/json' -Body $body
```

**In Open WebUI:** Click the paperclip icon in the chat input to attach an image.
Select model `vision` from the model dropdown.

**Supported formats:** PNG, JPEG, GIF, WebP.

**Context limit:** 8192 tokens for vision model. Large images are resized internally by
llama.cpp to fit within the model's image resolution limits (~448×448 for Qwen2-VL-7B).

**VRAM:** Vision model uses ~5.3 GB (4.4 GB weights + 0.9 GB mmproj). It swaps
with planner/coder/chat — switching to vision model takes 2–5 seconds on 16 GB VRAM.
```

---

## Edge Cases

| Case | Behavior |
|------|----------|
| Large image (10 MB PNG) | llama.cpp resizes internally before encoding; no explicit limit |
| Non-image file passed to `bob describe` | Base64 encodes anyway; model returns garbage or error |
| `vision` not in active profile | llama-swap returns 404 for the `vision` model id; `bob describe` shows clear error |
| mmproj missing but model GGUF present | llama-server fails to start with "mmproj file not found" — caught by `bob diagnose` |
| KV quant with mmproj | Fully supported by llama.cpp; no special handling needed |
| Vision model in swap group | llama-swap evicts previous big model before loading vision — normal swap behavior |

---

## Verification

```powershell
# 1. Fetch vision model
bob fetch
# Should download qwen2-vl-7b-q4_k_m.gguf AND qwen2-vl-7b-mmproj-f16.gguf

# 2. Verify config generation
bob gen
Select-String 'mmproj' config\llama-swap.yaml
# Should show: vision cmd contains --mmproj .../qwen2-vl-7b-mmproj-f16.gguf

# 3. Diagnose
bob diagnose
# Should show vision model + mmproj file both OK

# 4. Live test
bob serve
# Take a screenshot, save as test.png
bob describe test.png
# Should print a detailed description of the image

# 5. Custom prompt
bob describe diagram.png "List all the components shown"

# 6. API test
$b64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes('test.png'))
$r = Invoke-RestMethod http://localhost:8080/v1/chat/completions -Method POST `
     -ContentType 'application/json' `
     -Body (@{model='vision';messages=@(@{role='user';content=@(
         @{type='image_url';image_url=@{url="data:image/png;base64,$b64"}},
         @{type='text';text='What is in this image?'}
     )})} | ConvertTo-Json -Depth 10)
$r.choices[0].message.content

# 7. Full test suite
.\scripts\test-dry-run.ps1
```

## Files Modified

| File | Change |
|------|--------|
| `config/models.psd1` | Add `vision` role with `mmproj` field to 16gb+ profiles; update group members |
| `scripts/_models.ps1` | Add `vision` to stable role order |
| `scripts/gen-llama-swap.ps1` | Handle `mmproj` field in cmd building |
| `scripts/fetch-models.ps1` | Download mmproj sub-files for vision models |
| `scripts/diagnose.ps1` | Validate mmproj files present and correct size |
| `scripts/llm.ps1` | Add `describe` command |
| `docs/USAGE.md` | Vision section |

## Estimated Effort

- config/models.psd1 changes: 30 min
- gen-llama-swap.ps1 mmproj: 30 min
- fetch-models.ps1 mmproj: 45 min
- bob describe command: 1 hour (base64 + SSE streaming)
- diagnose.ps1: 30 min
- docs: 30 min
- Testing (download + live test): 1 hour

Total: ~5 hours
