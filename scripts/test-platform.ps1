#requires -Version 7
# NC1 — unit tests for the OS-abstraction seam (scripts/_platform.ps1). Pure resolvers are asserted
# for BOTH -Os values regardless of host, so the Linux branches are proven on a Windows box; a small
# $IsLinux-guarded block exercises the real Linux executors on the CI Linux runner. No venv, no
# models, no network — this is why check.ps1 can run it on ubuntu AND windows.
#
#   .\scripts\test-platform.ps1            # show failures only
#   .\scripts\test-platform.ps1 -Verbose   # show every result
param([switch]$Verbose)

$ErrorActionPreference = "Stop"
. "$PSScriptRoot\_platform.ps1"

$pass = 0; $fail = 0
function Assert {
  param([string]$Label, [bool]$Ok, [string]$Got = '', [string]$Expected = '')
  if ($Ok) {
    $script:pass++
    if ($Verbose) { Write-Host "  pass  $Label" -ForegroundColor DarkGreen }
  } else {
    $script:fail++
    Write-Host "  FAIL  $Label" -ForegroundColor Red
    if ($Got)      { Write-Host "        got:      $Got"      -ForegroundColor DarkRed }
    if ($Expected) { Write-Host "        expected: $Expected" -ForegroundColor DarkRed }
  }
}

# Preserve + clear the env we mutate; restore in the finally.
$savedForceOs  = $env:BOB_FORCE_OS
$savedDataDir  = $env:BOB_DATA_DIR
$savedLitellm  = $env:BOB_LITELLMKEY
$savedLitellm2 = $env:litellmKey
$env:BOB_FORCE_OS = $null; $env:BOB_DATA_DIR = $null; $env:BOB_LITELLMKEY = $null; $env:litellmKey = $null

try {
  # ------------------------------------------------------------------------
  Write-Host "`n[1] Get-BobOS — host detection + BOB_FORCE_OS override" -ForegroundColor Cyan
  # ------------------------------------------------------------------------
  $hostExpected = if ($IsWindows) { 'windows' } elseif ($IsLinux) { 'linux' } elseif ($IsMacOS) { 'macos' } else { 'windows' }
  Assert "unset -> host OS ('$hostExpected')" ((Get-BobOS) -eq $hostExpected) (Get-BobOS) $hostExpected
  $env:BOB_FORCE_OS = 'linux'
  Assert "BOB_FORCE_OS=linux honored"   ((Get-BobOS) -eq 'linux')
  $env:BOB_FORCE_OS = 'WINDOWS'
  Assert "BOB_FORCE_OS case-insensitive" ((Get-BobOS) -eq 'windows')
  $env:BOB_FORCE_OS = 'plan9'
  Assert "invalid override -> host OS"   ((Get-BobOS) -eq $hostExpected)   # warns + ignores
  $env:BOB_FORCE_OS = $null

  # ------------------------------------------------------------------------
  Write-Host "`n[2] Resolve-DataDir / Get-CacheDir (C4)" -ForegroundColor Cyan
  # ------------------------------------------------------------------------
  $p = Resolve-DataDir -Os linux -Override $null -Repo '/repo'
  Assert "default data dir = repo/data"      (($p.Dir -replace '\\','/') -eq '/repo/data')
  Assert "default -> no migration"           (-not $p.Migrate)
  $p = Resolve-DataDir -Os linux -Override '/xdg/bob' -Repo '/repo'
  Assert "override -> that dir"              ($p.Dir -eq '/xdg/bob')
  Assert "override -> migrate from repo/data" ($p.Migrate -and (($p.From -replace '\\','/') -eq '/repo/data'))
  $p = Resolve-DataDir -Os linux -Override '~/bobdata' -Repo '/repo'
  Assert "~ is expanded to HOME"             ($p.Dir -eq (Join-Path $HOME 'bobdata'))

  # ------------------------------------------------------------------------
  Write-Host "`n[3] Invoke-DataDirMigration — copies once, stamps, never clobbers" -ForegroundColor Cyan
  # ------------------------------------------------------------------------
  $tmp = Join-Path ([IO.Path]::GetTempPath()) "bobplat-$([Guid]::NewGuid().ToString('N').Substring(0,8))"
  $src = Join-Path $tmp 'src'; $dst = Join-Path $tmp 'dst'
  New-Item $src -ItemType Directory -Force | Out-Null
  New-Item $dst -ItemType Directory -Force | Out-Null
  Set-Content (Join-Path $src 'bob.db') 'original' -Encoding utf8
  Invoke-DataDirMigration -Src $src -Dst $dst
  Assert "copied file into dst"      (Test-Path (Join-Path $dst 'bob.db'))
  Assert ".migrated stamp written"   (Test-Path (Join-Path $dst '.migrated'))
  Set-Content (Join-Path $dst 'bob.db') 'modified' -Encoding utf8
  Invoke-DataDirMigration -Src $src -Dst $dst   # second call must be a no-op
  Assert "second call does not clobber" ((Get-Content (Join-Path $dst 'bob.db') -Raw).Trim() -eq 'modified')
  Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue

  # ------------------------------------------------------------------------
  Write-Host "`n[4] Get-Secret — precedence env > file > default (C3)" -ForegroundColor Cyan
  # ------------------------------------------------------------------------
  $sdir = Join-Path ([IO.Path]::GetTempPath()) "bobsec-$([Guid]::NewGuid().ToString('N').Substring(0,8))"
  New-Item $sdir -ItemType Directory -Force | Out-Null
  $env:BOB_DATA_DIR = $sdir
  Assert "default when nothing set" ((Get-Secret -Name litellmKey -Default 'sk-local') -eq 'sk-local')
  '{ "litellmKey": "from-file" }' | Set-Content (Join-Path $sdir 'secrets.json') -Encoding utf8
  Assert "file beats default"       ((Get-Secret -Name litellmKey -Default 'sk-local') -eq 'from-file')
  $env:BOB_LITELLMKEY = 'from-env'
  Assert "env (BOB_ prefix) beats file" ((Get-Secret -Name litellmKey -Default 'sk-local') -eq 'from-env')
  $env:BOB_LITELLMKEY = $null; $env:BOB_DATA_DIR = $null
  Remove-Item $sdir -Recurse -Force -ErrorAction SilentlyContinue

  Assert "keychain: linux -> secret-tool" ((Resolve-KeychainCmd -Name k -Os linux).Exe -eq 'secret-tool')
  Assert "keychain: windows -> none"       ($null -eq (Resolve-KeychainCmd -Name k -Os windows))

  # ------------------------------------------------------------------------
  Write-Host "`n[5] Resolve-CudaRootCandidates — Windows vs Linux layout" -ForegroundColor Cyan
  # ------------------------------------------------------------------------
  $c = Resolve-CudaRootCandidates -CudaArch 89 -Os windows
  Assert "win base is Program Files CUDA" ($c.Base -eq 'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA')
  Assert "win Ada minMajor 11"            ($c.MinMajor -eq 11)
  $c = Resolve-CudaRootCandidates -CudaArch 120 -Os windows
  Assert "win Blackwell pins v12.8"       ($c.Pin -eq 'v12.8')
  Assert "win Blackwell MinVer 12.8"      ($c.MinVer -eq '12.8')
  $c = Resolve-CudaRootCandidates -CudaArch 89 -Os linux
  Assert "linux base is /usr/local"       ($c.Base -eq '/usr/local')
  Assert "linux probes /usr/local/cuda"   ($c.Fixed -contains '/usr/local/cuda')
  Assert "linux probes /opt/cuda (Arch)"  ($c.Fixed -contains '/opt/cuda')
  Assert "linux Ada MinVer 11.0"          ($c.MinVer -eq '11.0')
  Assert "linux dir prefix cuda-"         ($c.DirPrefix -eq 'cuda-')
  $c = Resolve-CudaRootCandidates -CudaArch 120 -Os linux
  Assert "linux Blackwell pins cuda-12.8" ($c.Pin -eq '/usr/local/cuda-12.8')
  Assert "linux Blackwell MinVer 12.8"    ($c.MinVer -eq '12.8')

  # ------------------------------------------------------------------------
  Write-Host "`n[6] Get-AgentTaskSpec — scheduled task vs cron" -ForegroundColor Cyan
  # ------------------------------------------------------------------------
  $s = '/repo/scripts/bob-agent.ps1'
  $win = Get-AgentTaskSpec -ScriptPath $s -Os windows
  Assert "win spec is schtasks"       ($win.Kind -eq 'schtasks')
  Assert "win argument byte-identical" ($win.Argument -eq "-WindowStyle Hidden -NonInteractive -File `"$s`"")
  Assert "win interval 1 min"          ($win.IntervalMinutes -eq 1)
  $lin = Get-AgentTaskSpec -ScriptPath $s -Os linux
  Assert "linux spec is cron"          ($lin.Kind -eq 'cron')
  Assert "linux crontab every minute"  ($lin.Crontab -match '^\* \* \* \* \* ')
  Assert "linux runs pwsh -File"       ($lin.Crontab -match 'pwsh -NonInteractive -File')
  Assert "linux crontab tagged"        ($lin.Crontab -match '# BobAgent$')

  # ------------------------------------------------------------------------
  Write-Host "`n[7] Resolve-BgLaunchSpec — hidden vs nohup" -ForegroundColor Cyan
  # ------------------------------------------------------------------------
  $l = Resolve-BgLaunchSpec -PwshArgs @('-File', 'x.ps1') -Os windows
  Assert "win launch = pwsh hidden"    ($l.Exe -eq 'pwsh' -and $l.Hidden)
  $l = Resolve-BgLaunchSpec -PwshArgs @('-File', 'x.ps1') -Os linux
  Assert "linux launch = nohup pwsh"   ($l.Exe -eq 'nohup' -and -not $l.Hidden -and $l.Args[0] -eq 'pwsh')

  # ------------------------------------------------------------------------
  Write-Host "`n[8] Resolve-PackageCmd — winget vs apt/dnf/pacman" -ForegroundColor Cyan
  # ------------------------------------------------------------------------
  Assert "win uses winget"       ((Resolve-PackageCmd -Package git -Os windows).Exe -eq 'winget')
  Assert "linux apt uses apt-get" ((Resolve-PackageCmd -Package git -Os linux -Manager apt).Exe -eq 'apt-get')
  Assert "linux dnf uses dnf"     ((Resolve-PackageCmd -Package git -Os linux -Manager dnf).Exe -eq 'dnf')
  Assert "linux pacman uses -S"   ((Resolve-PackageCmd -Package git -Os linux -Manager pacman).Args -contains '-S')
  Assert "linux install needs sudo" ((Resolve-PackageCmd -Package git -Os linux -Manager apt).Sudo)

  # ------------------------------------------------------------------------
  Write-Host "`n[9] Resolve-BuildCmakeFlags — CPU vs CUDA, per OS" -ForegroundColor Cyan
  # ------------------------------------------------------------------------
  Assert "cpu -> CUDA off"          (-not (Resolve-BuildCmakeFlags -Cpu -Os linux).Cuda)
  Assert "cpu -> no DLL staging"    (-not (Resolve-BuildCmakeFlags -Cpu -Os windows).StageDlls)
  Assert "cpu linux -> Ninja"       ((Resolve-BuildCmakeFlags -Cpu -Os linux).Generator -eq 'Ninja')
  Assert "cpu win -> VS generator"  ((Resolve-BuildCmakeFlags -Cpu -Os windows).Generator -eq 'Visual Studio 17 2022')
  Assert "gpu win -> CUDA on"       ((Resolve-BuildCmakeFlags -Arch 120 -Os windows).Cuda)
  Assert "gpu win -> DLL staging"   ((Resolve-BuildCmakeFlags -Arch 120 -Os windows).StageDlls)
  Assert "gpu linux -> Ninja"       ((Resolve-BuildCmakeFlags -Arch 89 -Os linux).Generator -eq 'Ninja')
  Assert "gpu linux -> no staging"  (-not (Resolve-BuildCmakeFlags -Arch 89 -Os linux).StageDlls)

  # ------------------------------------------------------------------------
  Write-Host "`n[10] Path / exe helpers" -ForegroundColor Cyan
  # ------------------------------------------------------------------------
  Assert "exe name win -> .exe"     ((Get-BobExeName 'llama-server' -Os windows) -eq 'llama-server.exe')
  Assert "exe name linux -> bare"   ((Get-BobExeName 'llama-server' -Os linux) -eq 'llama-server')
  Assert "curl win -> curl.exe"     ((Get-CurlExe -Os windows) -eq 'curl.exe')
  Assert "curl linux -> curl"       ((Get-CurlExe -Os linux) -eq 'curl')
  Assert "config dir linux uses .config" ((Get-HomeConfigDir -App fabric -Os linux) -match '[\\/]\.config[\\/]fabric$')
  Assert "venv exe win -> Scripts\\x.exe" ((Get-VenvExe -Venv venv-litellm -Exe litellm -Os windows) -match '[\\/]venv-litellm[\\/]Scripts[\\/]litellm\.exe$')
  Assert "venv exe linux -> bin/x"        ((Get-VenvExe -Venv venv-litellm -Exe litellm -Os linux) -match '[\\/]venv-litellm[\\/]bin[\\/]litellm$')
  Assert "bin exe win -> .exe"            ((Get-BinExe -Base whisper-server -Os windows) -match '[\\/]bin[\\/]whisper-server\.exe$')
  Assert "bin exe linux -> bare"          ((Get-BinExe -Base whisper-server -Os linux) -match '[\\/]bin[\\/]whisper-server$')

  # ------------------------------------------------------------------------
  Write-Host "`n[11] Live host executors (real OS)" -ForegroundColor Cyan
  # ------------------------------------------------------------------------
  $ram = Get-SystemRamGB
  Assert "Get-SystemRamGB returns TotalGB > 0" ($ram -and $ram.TotalGB -gt 0)
  if ($IsLinux) {
    $st = Get-AgentTaskStatus
    Assert "linux Get-AgentTaskStatus returns a shape" ($st.ContainsKey('Registered'))
  }
}
finally {
  $env:BOB_FORCE_OS  = $savedForceOs
  $env:BOB_DATA_DIR  = $savedDataDir
  $env:BOB_LITELLMKEY = $savedLitellm
  $env:litellmKey    = $savedLitellm2
}

Write-Host "`n$pass passed, $fail failed" -ForegroundColor $(if ($fail) { 'Red' } else { 'Green' })
exit $(if ($fail) { 1 } else { 0 })
