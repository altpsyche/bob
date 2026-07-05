#requires -Version 7
# Bob scheduled agent runner — invoked every minute by Windows Task Scheduler.
# Reads data/schedules.json, evaluates cron expressions, calls bob_loop.py for due schedules.
# Registered via: bob agent install

$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
. "$PSScriptRoot\_models.ps1"

try {
  $bobCfg = Get-BobConfig   # also writes data/config.json for Python
} catch {
  Write-Warning "bob-agent: failed to load config — $_"
  exit 1
}

if (-not $bobCfg.agent.enabled) { exit 0 }

$schedFile = Join-Path $repo $bobCfg.agent.scheduleFile
if (-not (Test-Path $schedFile)) { exit 0 }

$logFile   = Join-Path $repo $bobCfg.agent.logFile
$venvPy    = Get-VenvExe -Venv 'venv-litellm' -Exe 'python'   # NC4: OS-aware (Scripts\python.exe | bin/python)
$loopScript = Join-Path $repo 'scripts\bob_loop.py'

if (-not (Test-Path $venvPy)) {
  Write-Warning "bob-agent: venv-litellm python not found: $venvPy"
  exit 1
}

# Ensure log directory exists
$logDir = Split-Path $logFile
if (-not (Test-Path $logDir)) { New-Item $logDir -ItemType Directory -Force | Out-Null }

# Load schedules
try {
  $raw       = Get-Content $schedFile -Raw -Encoding UTF8
  $schedules = $raw | ConvertFrom-Json
} catch {
  Write-Warning "bob-agent: failed to parse $schedFile — $_"
  exit 1
}

$now     = [DateTime]::UtcNow
$changed = $false

foreach ($entry in $schedules) {
  if (-not $entry.enabled) { continue }

  $lastRun = if ($entry.lastRun) {
    [DateTime]::Parse($entry.lastRun, $null, [System.Globalization.DateTimeStyles]::RoundtripKind)
  } else {
    [DateTime]::MinValue
  }

  if (-not (Test-CronDue -Cron $entry.cron -Now $now -LastRun $lastRun)) { continue }

  $goal = $entry.action.goal
  $role = if ($entry.action.role) { $entry.action.role } else { $bobCfg.routing.agentRole }

  Add-Content $logFile -Value "[$($now.ToString('o'))] Running: $($entry.name)" -Encoding UTF8

  $env:PYTHONIOENCODING = 'utf-8'
  try {
    $result = & $venvPy $loopScript $goal --role $role --agency 'silent' 2>> $logFile | Out-String
    $result = $result.Trim()
  } catch {
    $result = "Agent error: $_"
    Add-Content $logFile -Value "ERROR: $result" -Encoding UTF8
  }
  $env:PYTHONIOENCODING = $null

  # Update schedule entry
  $entry.lastRun = $now.ToString('o')
  $maxChars = [int]($bobCfg.agent.maxResultChars ?? 500)
  $entry.lastRunResult = if ($result.Length -gt $maxChars) {
    $result.Substring(0, $maxChars)
  } else { $result }

  $changed = $true

  # Desktop notification via the OS seam (Windows toast | Linux notify-send), not the raw WinRT
  # bob-toast.ps1 directly — so agent notifications actually appear on Linux too.
  if ($entry.notify -and $result) {
    $toastTitle = if ($entry.notifyTitle) { $entry.notifyTitle } else { $entry.name }
    Send-Notification -Title $toastTitle -Body $result
  }

  Add-Content $logFile -Value "[$($now.ToString('o'))] Done: $($entry.name)" -Encoding UTF8
}

# Atomic write-back only if something ran
if ($changed) {
  $tmp = [System.IO.Path]::GetTempFileName()
  try {
    $schedules | ConvertTo-Json -Depth 10 | Set-Content $tmp -Encoding UTF8
    Move-Item $tmp $schedFile -Force
  } catch {
    Remove-Item $tmp -ErrorAction SilentlyContinue
    Write-Warning "bob-agent: failed to write $schedFile — $_"
  }
}
