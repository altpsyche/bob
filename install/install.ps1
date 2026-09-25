# Bob one-command installer (Windows). Fetched and piped to PowerShell:
#
#   irm https://get.bob.sh/install.ps1 | iex
#
# Clones Bob (with submodules) into $env:BOB_HOME (default %USERPROFILE%\bob), runs the prereq + setup
# steps (the .bat stubs, which hand off to `python -m bob.kernel`), then verifies against versions.lock.
# Idempotent: a re-run fast-forwards an existing clone. This is an OS-shell bootstrap, not a return of
# the retired PowerShell harness: it mirrors how install_prereqs.bat bootstraps today.
#
# Flags: --dev / --channel <stable|latest> are consumed here; --cpu, --from-source and --with-node also
# reach the prereq step; every other flag (--with-aider, --with-fabric, --with-webui, --skip-models, ...)
# passes through to setup.
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

# Parse the release channel (consumed here). Default 'stable'; --dev / '--channel latest' tracks main.
$channel = 'stable'
$setupArgs = @()
for ($i = 0; $i -lt $args.Count; $i++) {
  $a = $args[$i]
  if ($a -eq '--dev') { $channel = 'latest' }
  elseif ($a -eq '--channel') { $i++; if ($i -lt $args.Count) { $channel = $args[$i] } }
  elseif ($a -like '--channel=*') { $channel = $a.Substring(10) }
  else { $setupArgs += $a }
}

# Stable channel: check out the latest release tag (carries the prebuilt engines) for driver-only plug-and-play.
if ($channel -eq 'stable') {
  git -C $BobHome fetch --tags --quiet 2>$null
  # Pick the newest release whose engines.json is actually published, so an install DURING a release's publish
  # window (tag exists but assets not up yet) lands on the newest READY release, not a source build. Same rule
  # as lifecycle.latest_ready_release_tag (which cannot run before Python exists): the newest 5 v* tags by
  # version order (_MAX_TAG_WALK), each probed at https://github.com/<slug>/releases/download/<tag>/engines.json,
  # ready when that manifest carries at least one real row (a top-level key not starting with '_'). Fall back
  # to the newest tag if none look ready (offline / no origin).
  $slug = ((git -C $BobHome remote get-url origin 2>$null) -replace '.*github\.com[:/]','' -replace '\.git$','' -replace '/$','')
  $tag = $null
  foreach ($t in (git -C $BobHome tag --list 'v*' --sort=-v:refname | Select-Object -First 5)) {
    if ($slug) {
      try {
        $m = Invoke-RestMethod -UseBasicParsing -TimeoutSec 20 `
          -Uri "https://github.com/$slug/releases/download/$t/engines.json"
        if ($m -and @($m.PSObject.Properties.Name | Where-Object { -not $_.StartsWith('_') }).Count -gt 0) {
          $tag = $t; break
        }
      } catch { }
    }
  }
  if (-not $tag) { $tag = (git -C $BobHome tag --list 'v*' --sort=-v:refname | Select-Object -First 1) }
  if ($tag) {
    Log "Stable channel: checking out release $tag  (use --dev to track the latest main)."
    git -C $BobHome checkout --quiet $tag
    git -C $BobHome submodule update --init --recursive
  } else {
    Log 'Stable channel requested but no release tag exists yet; staying on the default branch.'
  }
} else {
  Log 'Dev channel: tracking the latest main (prebuilt engine when one matches this commit, else a source build).'
}

# Forward the prereq-relevant flags (--cpu, --from-source, --with-node) to the prereq step. --with-node is
# prereq-only, so it is not passed on to setup.
$prereqFlags = @()
if ($setupArgs -contains '--cpu') { $prereqFlags += '--cpu' }
if ($setupArgs -contains '--from-source') { $prereqFlags += '--from-source' }
if ($setupArgs -contains '--with-node') { $prereqFlags += '--with-node' }
$setupArgs = @($setupArgs | Where-Object { $_ -ne '--with-node' })
Log 'Installing prerequisites ...'
& .\install_prereqs.bat @prereqFlags
Log 'Running setup ...'
& .\setup.bat @setupArgs
Log 'Verifying against versions.lock ...'
# scripts\ on PYTHONPATH so `python -m bob.kernel` resolves (the .bat stubs set this internally only).
$env:PYTHONPATH = "$BobHome\scripts;$env:PYTHONPATH"
python -m bob.kernel verify-install
Log 'Done. Start Bob with:  bob'
