# Bob one-command installer (Windows). Fetched and piped to PowerShell:
#
#   irm https://get.bob.sh/install.ps1 | iex
#
# Clones Bob (with submodules) into $env:BOB_HOME (default %USERPROFILE%\bob), runs the prereq + setup
# steps (the .bat stubs, which hand off to `python -m bob.kernel`), then verifies against versions.lock.
# Idempotent: a re-run fast-forwards an existing clone. This is an OS-shell bootstrap, not a return of
# the retired PowerShell harness - it mirrors how install_prereqs.bat bootstraps today.
$ErrorActionPreference = 'Stop'

$RepoUrl = if ($env:BOB_REPO_URL) { $env:BOB_REPO_URL } else { 'https://github.com/altpsyche/bob.git' }
$BobHome = if ($env:BOB_HOME) { $env:BOB_HOME } else { Join-Path $env:USERPROFILE 'bob' }

function Log($m) { Write-Host "[bob-install] $m" }

# Ensure git (winget), mirroring the .bat prereq bootstrap.
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
  Log 'git not found - installing via winget...'
  winget install --id Git.Git -e --accept-package-agreements --accept-source-agreements --disable-interactivity
  $env:Path = [System.Environment]::GetEnvironmentVariable('Path','Machine') + ';' +
              [System.Environment]::GetEnvironmentVariable('Path','User')
}

# Clone with submodules, or fast-forward an existing clone.
if (Test-Path (Join-Path $BobHome '.git')) {
  Log "Updating existing Bob at $BobHome ..."
  git -C $BobHome pull --ff-only 2>$null
  git -C $BobHome submodule update --init --recursive
} else {
  Log "Cloning Bob into $BobHome ..."
  git clone --recurse-submodules $RepoUrl $BobHome
}

Set-Location $BobHome

$cpu = if ($args -contains '--cpu') { '--cpu' } else { '' }
Log 'Installing prerequisites ...'
& .\install_prereqs.bat $cpu
Log 'Running setup ...'
& .\setup.bat @args
Log 'Verifying against versions.lock ...'
# scripts\ on PYTHONPATH so `python -m bob.kernel` resolves (the .bat stubs set this internally only).
$env:PYTHONPATH = "$BobHome\scripts;$env:PYTHONPATH"
python -m bob.kernel verify-install
Log 'Done. Start Bob with:  bob'
