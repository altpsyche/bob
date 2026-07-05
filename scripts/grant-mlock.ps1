#requires -Version 7
# Grant SeLockMemoryPrivilege to the current Windows user via secedit.
# Without this privilege, llama-server accepts --mlock but silently does nothing.
#
#   .\scripts\grant-mlock.ps1           # Grant (prompts UAC if not admin)
#   .\scripts\grant-mlock.ps1 -Check    # Report status only (no elevation needed)
#
# After granting: restart your terminal, then bob serve.

param([switch]$Check)
$ErrorActionPreference = "Stop"
. "$PSScriptRoot\_platform.ps1"   # Get-BobOS

# Windows-only: the whole script uses WindowsIdentity / secedit. On Linux `bob mlock` reached this
# directly and crashed with PlatformNotSupportedException — guard and point at the Linux equivalent.
if ((Get-BobOS) -ne 'windows') {
    Write-Host "mlock (SeLockMemoryPrivilege) is a Windows privilege. On Linux, raise the memlock limit instead:" -ForegroundColor Yellow
    Write-Host "  session:    ulimit -l unlimited" -ForegroundColor DarkGray
    Write-Host "  persistent: add '<user> - memlock unlimited' to /etc/security/limits.conf, then re-login" -ForegroundColor DarkGray
    exit 0
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    ([Security.Principal.WindowsPrincipal]$id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-MlockStatus {
    # Exports the USER_RIGHTS section of local security policy (no admin needed).
    # Returns a hashtable: { Granted: bool, Sid: string, PolicyLine: string }
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $tmpBase = [IO.Path]::GetTempFileName(); Remove-Item $tmpBase -ErrorAction SilentlyContinue
    $tmp = [IO.Path]::ChangeExtension($tmpBase, '.inf')
    try {
        secedit /export /cfg $tmp /areas USER_RIGHTS /quiet 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) {
            return @{ Granted = $false; Sid = $sid; PolicyLine = $null
                      Error = "secedit /export failed (exit $LASTEXITCODE) — check Group Policy restrictions" }
        }
        $lines   = Get-Content $tmp -Encoding Unicode -ErrorAction SilentlyContinue
        $privLine = $lines | Where-Object { $_ -match '^\s*SeLockMemoryPrivilege\s*=' } |
                   Select-Object -First 1
        return @{
            Granted    = [bool]($privLine -and $privLine -match [regex]::Escape("*$sid"))
            Sid        = $sid
            PolicyLine = $privLine
        }
    } finally {
        Remove-Item $tmp -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------------------
# -Check: report only, no modification
# ---------------------------------------------------------------------------

if ($Check) {
    $s = Get-MlockStatus
    if ($s.Error) { Write-Warning $s.Error }
    if ($s.Granted) {
        Write-Host "  mlock privilege: granted  ($($s.Sid))" -ForegroundColor Green
        exit 0
    } else {
        Write-Host "  mlock privilege: NOT granted — run: bob mlock" -ForegroundColor Yellow
        exit 1
    }
}

# ---------------------------------------------------------------------------
# Grant mode
# ---------------------------------------------------------------------------

# Re-launch as admin if needed (UAC prompt)
if (-not (Test-IsAdmin)) {
    Write-Host "Requesting admin rights to modify security policy..." -ForegroundColor Yellow
    try {
        $proc = [System.Diagnostics.Process]::Start([System.Diagnostics.ProcessStartInfo]@{
            FileName        = 'pwsh.exe'
            Arguments       = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$PSCommandPath`""
            Verb            = 'RunAs'
            UseShellExecute = $true
            WindowStyle     = 'Normal'
        })
        $proc.WaitForExit()
        # Parent process just shows the result message — the elevated child printed the details.
        if ($proc.ExitCode -eq 0) {
            Write-Host ""
            Write-Host "Privilege granted." -ForegroundColor Green
            Write-Host "IMPORTANT: close this terminal and open a new one, then run: bob serve" -ForegroundColor Yellow
        } else {
            Write-Host "Grant failed or was cancelled (exit $($proc.ExitCode))." -ForegroundColor Red
            Write-Host "Fallback: secpol.msc -> Local Policies -> User Rights Assignment -> Lock pages in memory"
        }
        exit $proc.ExitCode
    } catch [System.ComponentModel.Win32Exception] {
        Write-Host "UAC cancelled — mlock not granted." -ForegroundColor Yellow
        Write-Host "Fallback: secpol.msc -> Local Policies -> User Rights Assignment -> Lock pages in memory"
        exit 1
    }
}

# Running as admin: apply the change via secedit
$sid      = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$userName = [Security.Principal.WindowsIdentity]::GetCurrent().Name
Write-Host "Granting SeLockMemoryPrivilege to $userName ($sid)..."

$tmpInfBase = [IO.Path]::GetTempFileName(); Remove-Item $tmpInfBase -ErrorAction SilentlyContinue
$tmpInf = [IO.Path]::ChangeExtension($tmpInfBase, '.inf')
$tmpDbBase  = [IO.Path]::GetTempFileName(); Remove-Item $tmpDbBase  -ErrorAction SilentlyContinue
$tmpDb  = [IO.Path]::ChangeExtension($tmpDbBase,  '.sdb')
try {
    # Export current policy
    secedit /export /cfg $tmpInf /areas USER_RIGHTS /quiet 2>$null | Out-Null
    $lines    = Get-Content $tmpInf -Encoding Unicode
    $existing = $lines | Where-Object { $_ -match '^\s*SeLockMemoryPrivilege\s*=' } | Select-Object -First 1

    if ($existing -and $existing -match [regex]::Escape("*$sid")) {
        Write-Host "Already granted — no change needed." -ForegroundColor Green
        exit 0
    }

    # Build modified lines
    if ($existing) {
        # Append SID to existing privilege list
        $modified = $lines | ForEach-Object {
            if ($_ -match '^\s*SeLockMemoryPrivilege\s*=') { "$_,*$sid" } else { $_ }
        }
    } else {
        # No entry yet — insert after [Privilege Rights] section header
        $modified = [System.Collections.Generic.List[string]]::new()
        foreach ($line in $lines) {
            $modified.Add($line)
            if ($line -match '^\[Privilege Rights\]') {
                $modified.Add("SeLockMemoryPrivilege = *$sid")
            }
        }
    }

    # Write and apply
    [IO.File]::WriteAllLines($tmpInf, $modified, [Text.Encoding]::Unicode)
    secedit /configure /db $tmpDb /cfg $tmpInf /areas USER_RIGHTS /quiet 2>$null | Out-Null

    Write-Host "Done. SeLockMemoryPrivilege granted to $userName." -ForegroundColor Green
    exit 0
} catch {
    Write-Host "Error: $_" -ForegroundColor Red
    Write-Host "Fallback: secpol.msc -> Local Policies -> User Rights Assignment -> Lock pages in memory"
    exit 1
} finally {
    Remove-Item $tmpInf, $tmpDb -ErrorAction SilentlyContinue
}
