#requires -Version 7
# Benchmark a model role for quality using lm-evaluation-harness.
# Usage: bob eval <role> [task] [--shots N] [--limit N]
# Examples:
#   bob eval coder gsm8k             # math word problems, full run (~90 min sequential)
#   bob eval coder gsm8k --limit 100 # quick smoke test (~8 min)
#   bob eval coder humaneval         # code generation (~180 min)
#   bob eval planner mmlu            # general knowledge (5-shot)
#   bob eval planner hellaswag       # common-sense reasoning
# Results are saved as JSON to results/eval-<role>-<task>-<timestamp>/
# Uses local-chat-completions → /v1/chat/completions (required for chat-tuned models)
# Speed: sequential by default (parallel=1 server). For faster runs, set defaults.parallel
# in config/user.psd1 and pass num_concurrent via $env:LM_EVAL_CONCURRENT.
param(
    [string]$Role  = 'coder',
    [string]$Task  = 'mmlu',
    [int]   $Shots = 0,
    [int]   $Limit = 0   # 0 = full benchmark; >0 limits to N samples (quick smoke test)
)
$ErrorActionPreference = "Stop"
$repo   = Split-Path $PSScriptRoot -Parent
$lmEval = if ($IsWindows) { Join-Path $repo 'tools\venv-eval\Scripts\lm_eval.exe' }
          else            { Join-Path $repo 'tools/venv-eval/bin/lm_eval' }   # OS-aware venv layout

if (-not (Test-Path $lmEval)) {
    throw "lm-eval not installed. Run: .\scripts\bootstrap-eval.ps1"
}

. "$PSScriptRoot\_models.ps1"
$cfg  = Get-ModelsConfig
$port = $cfg.defaults.port ?? (Get-BobPortDefault 'port')

$profile   = $cfg.profiles.($cfg.activeProfile)
$modelCfg  = $profile.$Role
$tokenizer = $modelCfg.tokenizer
if (-not $tokenizer) {
    throw "No tokenizer configured for role '$Role' in profile '$($cfg.activeProfile)'. Add 'tokenizer = ...' to config/models.psd1."
}

try { Invoke-RestMethod "http://localhost:$port/v1/models" -TimeoutSec 3 | Out-Null }
catch { throw "Endpoint not running at http://localhost:$port/v1 — start it first: bob serve" }

$resultsDir = Join-Path $repo 'results'
if (-not (Test-Path $resultsDir)) { New-Item -ItemType Directory $resultsDir | Out-Null }
$outPath = Join-Path $resultsDir "eval-$Role-$Task-$(Get-Date -f yyyyMMdd-HHmm)"

$limitNote = if ($Limit -gt 0) { " (limit=$Limit)" } else { '' }
Write-Host "Benchmarking '$Role' on '$Task' (shots=$Shots)$limitNote..." -ForegroundColor Cyan
Write-Host "Endpoint:  http://localhost:$port/v1/chat/completions" -ForegroundColor DarkGray
Write-Host "Tokenizer: $tokenizer" -ForegroundColor DarkGray
Write-Host "Results:   $outPath\$Role\" -ForegroundColor DarkGray
Write-Host ""

$limitArgs = if ($Limit -gt 0) { @('--limit', $Limit) } else { @() }

$env:PYTHONUTF8 = '1'   # prevent UnicodeEncodeError on Windows cp1252 console
& $lmEval `
    --model local-chat-completions `
    --model_args "base_url=http://localhost:$port/v1/chat/completions,model=$Role,tokenizer=$tokenizer,tokenized_requests=False" `
    --tasks $Task `
    --apply_chat_template `
    --num_fewshot $Shots `
    --output_path $outPath `
    --log_samples `
    @limitArgs
