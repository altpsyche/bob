#requires -Version 7
# Wrapper: runs bob_memory.py inside venv-litellm.
# Reads memory.dbPath from config/bob.psd1 and passes it as --db.
# Usage: bob-memory.ps1 <store|recall|status|clear|init-profile> [args]
$repo   = Split-Path $PSScriptRoot -Parent
. "$PSScriptRoot\_models.ps1"                                 # Get-VenvExe (must precede $py)
$py     = Get-VenvExe -Venv 'venv-litellm' -Exe 'python'     # NC4: OS-aware (Scripts\python.exe | bin/python)
$script = Join-Path $PSScriptRoot 'bob_memory.py'

if (-not (Test-Path $py)) {
  Write-Error "venv-litellm python not found: $py  (run scripts/bootstrap-litellm.ps1 first)"
  exit 1
}

try {
  $bobCfg = Get-BobConfig
  # Normalize to forward slashes (works on both OSes); the old '/ -> \' forced a literal-backslash
  # filename on Linux.
  $dbRel  = ($bobCfg.memory.dbPath ?? 'data/bob.db') -replace '\\', '/'
  $dbPath = Join-Path $repo $dbRel
} catch {
  $dbPath = Join-Path $repo 'data/bob.db'
}

& $py $script --db $dbPath @args
