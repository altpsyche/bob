#requires -Version 7
# Install the 'bob' command on PATH. Windows: a .cmd shim in scoop\shims -> scripts/bob.ps1, plus
# PowerShell tab completions. Linux/macOS: symlink the repo-root ./bob POSIX shim into ~/.local/bin.
# Removes the retired 'llm' shim if a previous install left one (bob is the single CLI now). Idempotent.
$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
. "$PSScriptRoot\_models.ps1"   # NC1 seam: Get-BobOS, Get-BobExeName

if ((Get-BobOS) -ne 'windows') {
    # ── Linux/macOS: symlink the repo-root ./bob POSIX shim (and fabric) into ~/.local/bin (XDG user bin).
    #    The old body was Windows-only (scoop\shims) and THREW here, aborting setup.ps1 at step 9. ──
    $binDir = Join-Path $HOME '.local/bin'
    New-Item -ItemType Directory -Force -Path $binDir | Out-Null
    $bobLink = Join-Path $binDir 'bob'
    if (Test-Path $bobLink) { Remove-Item $bobLink -Force }
    New-Item -ItemType SymbolicLink -Path $bobLink -Target (Join-Path $repo 'bob') | Out-Null
    Write-Host "'bob' installed -> $bobLink" -ForegroundColor Green

    $fabricExe = Join-Path $repo (Join-Path 'bin' (Get-BobExeName 'fabric'))
    if (Test-Path $fabricExe) {
        $fabricLink = Join-Path $binDir 'fabric'
        if (Test-Path $fabricLink) { Remove-Item $fabricLink -Force }
        New-Item -ItemType SymbolicLink -Path $fabricLink -Target $fabricExe | Out-Null
        Write-Host "'fabric' installed -> $fabricLink" -ForegroundColor Green
    } else {
        Write-Host "'fabric' shim skipped — not built yet. Run: bob fabric-setup" -ForegroundColor DarkGray
    }

    if (($env:PATH -split [IO.Path]::PathSeparator) -notcontains $binDir) {
        Write-Host "NOTE: $binDir is not on PATH. fish: fish_add_path $binDir | bash/zsh: add it to your rc, then open a new shell." -ForegroundColor Yellow
    }
    Write-Host "Open a NEW terminal (with ~/.local/bin on PATH), then try:  bob help" -ForegroundColor Cyan
    return
}

# ── Windows: a bob.cmd shim in scoop\shims -> scripts/bob.ps1, plus PowerShell tab completions. ──
$bob  = Join-Path $repo "scripts\bob.ps1"
$pwsh = (Get-Command pwsh -ErrorAction Stop).Source

# locate a PATH dir to drop the shim into (prefer scoop\shims)
$shimDir = $null
$sc = Get-Command scoop -ErrorAction SilentlyContinue
if ($sc -and $sc.Source) { $shimDir = Split-Path $sc.Source }
if (-not $shimDir -or -not (Test-Path $shimDir)) { $shimDir = Join-Path $HOME "scoop\shims" }
if (-not (Test-Path $shimDir)) { throw "No scoop\shims dir found at $shimDir. Add scripts\ to PATH manually instead." }

# Primary shim: bob.cmd
$cmdPath = Join-Path $shimDir "bob.cmd"
@"
@echo off
"$pwsh" -NoProfile -ExecutionPolicy Bypass -File "$bob" %*
"@ | Set-Content -Path $cmdPath -Encoding ascii
Write-Host "'bob' installed -> $cmdPath" -ForegroundColor Green

# Remove old llm.cmd shim if present
$llmCmdPath = Join-Path $shimDir "llm.cmd"
if (Test-Path $llmCmdPath) { Remove-Item $llmCmdPath -Force; Write-Host "'llm' shim removed." -ForegroundColor DarkGray }

# Shim for fabric so 'git diff | fabric --pattern X' works directly in any shell.
$fabricExe = Join-Path $repo "bin\fabric.exe"
$fabricCmd = Join-Path $shimDir "fabric.cmd"
if (Test-Path $fabricExe) {
    @"
@echo off
"$fabricExe" %*
"@ | Set-Content -Path $fabricCmd -Encoding ascii
    Write-Host "'fabric' installed -> $fabricCmd" -ForegroundColor Green
} else {
    Write-Host "'fabric' shim skipped — bin\fabric.exe not built yet. Run: bob fabric-setup" -ForegroundColor DarkGray
}

# Register tab completions in the user's PowerShell profile (idempotent)
$profilePath = $PROFILE.CurrentUserAllHosts
if (-not $profilePath) {
    # $PROFILE is null in non-interactive batch contexts — derive the standard path manually
    $profilePath = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'PowerShell\profile.ps1'
}
$modelsFile = Join-Path $repo 'config\models.json'   # ONE-C C0c: neutral JSON registry
if (-not (Test-Path $profilePath)) { New-Item -Path $profilePath -Force | Out-Null }

$completerBlock = @"

# bob CLI tab completions (added by install-cli.ps1)
Register-ArgumentCompleter -Native -CommandName bob -ScriptBlock {
    param(`$wordToComplete, `$commandAst, `$cursorPosition)
    `$tokens = @(`$commandAst.CommandElements | Select-Object -Skip 1 | ForEach-Object { "`$_" })
    `$cmd = if (`$tokens.Count -gt 0) { `$tokens[0] } else { '' }
    `$subCmds = @('serve','up','stop','restart','status','logs','models','chat','bench',
                 'profiles','profile','fetch','verify-urls','update','gen','aider','webui',
                 'diagnose','ps','show','version')
    if (`$tokens.Count -le 1) {
        `$subCmds | Where-Object { `$_ -like "`$wordToComplete*" } |
            ForEach-Object { [System.Management.Automation.CompletionResult]::new(`$_, `$_, 'ParameterValue', `$_) }
    } elseif (`$cmd -in @('chat','bench','show')) {
        @('planner','coder','chat','fim','embed') | Where-Object { `$_ -like "`$wordToComplete*" } |
            ForEach-Object { [System.Management.Automation.CompletionResult]::new(`$_, `$_, 'ParameterValue', `$_) }
    } elseif (`$cmd -eq 'profile') {
        `$profiles = @('auto')
        if (Test-Path '$modelsFile') {
            `$profiles += (Get-Content -Raw '$modelsFile' | ConvertFrom-Json -AsHashtable).profiles.Keys | Sort-Object
        }
        `$profiles | Where-Object { `$_ -like "`$wordToComplete*" } |
            ForEach-Object { [System.Management.Automation.CompletionResult]::new(`$_, `$_, 'ParameterValue', `$_) }
    } elseif (`$cmd -eq 'fetch') {
        @('--list') | Where-Object { `$_ -like "`$wordToComplete*" } |
            ForEach-Object { [System.Management.Automation.CompletionResult]::new(`$_, `$_, 'ParameterValue', `$_) }
    } elseif (`$cmd -eq 'up') {
        @('-NoOpen') | Where-Object { `$_ -like "`$wordToComplete*" } |
            ForEach-Object { [System.Management.Automation.CompletionResult]::new(`$_, `$_, 'ParameterValue', `$_) }
    }
}
"@

$existing = Get-Content $profilePath -Raw -ErrorAction SilentlyContinue
if (-not $existing -or -not $existing.Contains('bob CLI tab completions')) {
    Add-Content -Path $profilePath -Value $completerBlock -Encoding utf8
    Write-Host "Tab completions added to: $profilePath" -ForegroundColor Green
    Write-Host "(Restart terminal or: . `$PROFILE)" -ForegroundColor DarkGray
} else {
    Write-Host "Tab completions already registered." -ForegroundColor DarkGray
}

Write-Host "Open a NEW terminal, then try:  bob help" -ForegroundColor Cyan
