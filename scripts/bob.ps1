#requires -Version 7
# 'bob' — personal AI assistant CLI. Put on PATH by scripts/install-cli.ps1.
# Usage:  bob <command> [args]   (run `bob help` for the list)
$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
. "$PSScriptRoot\_models.ps1"
$d        = (Get-ModelsConfig).defaults
# Port literals live only in $script:BobPortDefaults (M6) — read via Get-BobPortDefault, never re-inline.
$port        = $d.port        ?? (Get-BobPortDefault 'port')
$litellmPort = $d.litellmPort ?? (Get-BobPortDefault 'litellmPort')
$base        = "http://localhost:$port/v1"
$litellmBase = "http://localhost:$litellmPort/v1"
# Copy out of the automatic $args (whose slicing unwraps oddly) into plain arrays.
$argv = @($args)
if ($argv.Count -eq 0) {
  # NE2 Decision C — no-arg on an interactive terminal launches the REPL/TUI (`bob shell`, a python
  # verb routed below); a piped/redirected/CI invocation keeps today's help so scripts are unaffected.
  if (-not [Console]::IsInputRedirected -and -not [Console]::IsOutputRedirected) { $argv = @('shell') }
  else { $argv = @('help') }
}
$cmd  = $argv[0]
$rest = @($argv | Select-Object -Skip 1)   # always an array, even for a single arg

# NB4 (contract C1) — front-door dispatch. config/verbs.json (generated from the C6 command
# registry) declares each command's runtime; runtime commands are handled by `python -m bob`,
# everything else falls through to the orchestration switch below. Resolution is per
# fully-qualified command ("agent serve" vs "agent schedule") so subcommands split correctly.
# This is the "shim reads verbs.json without Python" step (bootstrap `setup` stays pwsh, no venv).
$verbsFile = Join-Path $repo 'config\verbs.json'
if (Test-Path $verbsFile) {
  try {
    $verbs = Get-Content -Raw -LiteralPath $verbsFile | ConvertFrom-Json -AsHashtable
    $route = $null
    $two   = if ($rest.Count) { "$cmd $($rest[0])" } else { $null }
    if     ($two -and $verbs.commands.Contains($two)) { $route = $verbs.commands[$two] }
    elseif ($verbs.commands.Contains($cmd))           { $route = $verbs.commands[$cmd] }
    if ($route -eq 'python') {
      $venvPy = Get-VenvExe -Venv 'venv-litellm' -Exe 'python'   # NC4: OS-aware (Scripts\python.exe | bin/python)
      if (-not (Test-Path $venvPy)) {
        Write-Host "Error: venv-litellm not found. Run: scripts/bootstrap-litellm.ps1" -ForegroundColor Red
        exit 1
      }
      # Regenerate data/config.json from the single source so the runtime sees fresh config
      # (M17 timestamp check makes this near-free), matching the old per-verb Get-BobConfig call.
      try { Get-BobConfig | Out-Null } catch {}
      $env:PYTHONPATH = Join-Path $repo 'scripts'
      $env:PYTHONIOENCODING = 'utf-8'
      & $venvPy -m bob @argv
      exit $LASTEXITCODE
    }
  } catch {
    Write-Warning "verbs.json dispatch failed ($_); falling back to the built-in switch."
  }
}

switch ($cmd) {
  # ONE-C: status/ps/up/serve/restart/logs/webui are runtime=python (scripts/tools/stack.py via cli.py
  # handlers); the dispatch prologue routes them to `python -m bob` before this switch. Deleted.
  # ('status' ported after Slice 6 — the registry read it needs became Python-native in Slice 4.)
  # 'diagnose' -> runtime=python (cli.py _handle_diagnose over scripts/tools/health.py:diagnose — the
  # SPLIT port: registry + light discovery. scripts/diagnose.ps1 stays for the DEEP OS discovery
  # (CUDA/RAM/NUMA/mlock/package) called by setup.ps1/fetch-models.ps1; ports to Python in ONE-D.)
  # Routed by the dispatch prologue before this switch. (ONE-C Slice 3)
  # 'aider' -> runtime=python (cli.py _handle_aider); routed by the dispatch prologue. (ONE-C Slice 1)
  # 'models' and 'show' -> runtime=python (cli.py handlers over scripts/tools/models.py, built on the
  # neutral config/models.json); routed by the dispatch prologue before this switch. (ONE-C Slice 4)
  # 'gen' -> runtime=python (cli.py _handle_gen over scripts/tools/generate.py:gen_all — byte-parity
  # port of the four gen-*.ps1). Routed by the dispatch prologue before this switch. The gen-*.ps1
  # scripts STAY: the pre-venv cold-start/bootstrap path (bootstrap.ps1/start.ps1/setup-clients.ps1)
  # still calls them directly and can't depend on the venv; they retire in ONE-D with that path.
  # (ONE-C Slice 6)
  # 'fetch' -> runtime=python (cli.py _handle_fetch over scripts/tools/provision.py:fetch_models — curl
  # resume + SHA256-verify vs versions.lock + manifest). Routed by the dispatch prologue before this
  # switch. fetch-models.ps1 is KEPT: the pre-venv cold-start path (bootstrap.ps1) still calls it and
  # can't depend on the venv; it retires in ONE-D Slice D8 when the kernel calls provision.fetch_models.
  # (ONE-D Slice D1)
  # 'profiles', 'profile', and 'bench' -> runtime=python (cli.py handlers over scripts/tools/models.py:
  # profiles_list/profile_switch/bench); routed by the dispatch prologue before this switch. profile
  # switch writes data/active-profile.json (D4) + regenerates configs best-effort. (ONE-C Slice 4)
  # 'chat'/'code'/'think' (S2), 'describe'/'screenshot' (ONE-B2) and 'voice'/'listen'/'transcribe'/
  # 'speak' (ONE-B5) — MIGRATED to Python. They are runtime=python in config/verbs.json, so the front
  # door routes them to `python -m bob` (the agent loop: chat mode for text, images=[…] + vision role
  # for describe/screenshot, STT→loop→TTS for /voice) and they never reach this switch. The old pwsh
  # REPL, System.Drawing vision, the voice loop, and Invoke-BobStream/Format-ForSpeech are all deleted —
  # one loop, one memory path.
  # 'stop' -> runtime=python (cli.py _handle_stop over scripts/tools/stack.py:stack_stop); routed by
  # the dispatch prologue before this switch. Case deleted. (ONE-C Slice 2)
  'build' {
    # NC3/NC8 — (re)build llama.cpp. Auto-selects the CPU tier when no GPU is present (or with --cpu);
    # otherwise a CUDA build for the detected arch. Cross-platform via build-llama.ps1's seam.
    $cpu   = ($rest -contains '--cpu') -or (-not (Get-GpuInfo))
    $force = $rest -contains '--force'
    $bArgs = @{}
    if ($cpu)   { $bArgs['Cpu'] = $true }
    if ($force) { $bArgs['Force'] = $true }
    if ($cpu -and -not ($rest -contains '--cpu')) {
      Write-Host "No GPU detected — building the CPU-only tier. Use 'bob build --cpu' to force, or install CUDA for a GPU build." -ForegroundColor Yellow
    }
    & "$repo\scripts\build-llama.ps1" @bArgs
  }
  'lock' {
    # ND1 — (re)generate versions.lock from the single sources (git gitlinks + models.psd1 +
    # manifest.json + pip freeze). `bob lock --check` is the staleness gate wired into check.ps1.
    if ($rest -contains '--check') {
      $rc = Test-VersionsLockSync
      if ($rc -eq 0) { Write-Host "versions.lock in sync" -ForegroundColor Green }
      exit $rc
    }
    $p = Write-VersionsLock
    Write-Host "wrote $p" -ForegroundColor Green
  }
  'update' {
    # ND3 — release-aware, cross-platform update with rollback. Moves the working tree to a target
    # release (default: fast-forward the current branch; `--tag <ref>` for a specific release), syncs
    # submodules to the NEW lock's pinned commits, rebuilds ONLY what changed with a bin/ snapshot,
    # verifies the rebuilt binary, and rolls the build output back on failure. Regenerates versions.lock
    # on success. Cross-platform via the NC1 seam (Get-BinExe / Backup-/Restore-BuildOutput).
    $targetRef = $null
    for ($i = 0; $i -lt $rest.Count; $i++) { if ($rest[$i] -eq '--tag' -and ($i + 1) -lt $rest.Count) { $targetRef = $rest[$i + 1] } }

    $binDir = Join-Path $repo 'bin'
    $llmCpp = Join-Path $repo 'external\llama.cpp'
    $before = & git -C $llmCpp rev-parse HEAD 2>$null

    Write-Host "Fetching updates..."
    & git -C $repo fetch --tags --quiet
    if ($targetRef) {
      Write-Host "Checking out release '$targetRef'..."
      & git -C $repo checkout $targetRef
    } else {
      Write-Host "Fast-forwarding the current branch..."
      & git -C $repo pull --ff-only
    }
    if ($LASTEXITCODE -ne 0) { Write-Host "Fetch/checkout failed — nothing changed." -ForegroundColor Red; break }

    Write-Host "Syncing submodules to the pinned commits..."
    & git -C $repo submodule update --init --recursive
    if ($LASTEXITCODE -ne 0) { Write-Host "Submodule sync failed." -ForegroundColor Red; break }
    $after = & git -C $llmCpp rev-parse HEAD 2>$null

    # Reinstall the venv from the (possibly updated) requirements lock. Idempotent — a no-op if unchanged.
    if (Test-Path (Join-Path $repo 'scripts\bootstrap-litellm.ps1')) {
      Write-Host "Ensuring the Python runtime venv matches the lock..."
      & "$repo\scripts\bootstrap-litellm.ps1"
    }

    # Rebuild ONLY changed components. llama.cpp is the heavy one; rebuild only if its commit moved.
    $short = { param($s) if ($s) { "$s".Substring(0, [Math]::Min(8, "$s".Length)) } else { '(none)' } }
    if ($before -eq $after) {
      Write-Host "llama.cpp unchanged ($(& $short $after)) — no rebuild needed." -ForegroundColor DarkGray
    } else {
      Write-Host "llama.cpp $(& $short $before) -> $(& $short $after); rebuilding (bin/ snapshotted for rollback)..."
      $bak = Backup-BuildOutput -Path $binDir
      & "$repo\scripts\build-llama.ps1" -Force
      $buildOk = ($LASTEXITCODE -eq 0)

      # Verify the rebuild produced a working server binary (the concrete post-build gate). `bob doctor`
      # is run afterward for a full readout, but this is what decides rollback.
      $srv = Get-BinExe 'llama-server'
      $verifyOk = $false
      if ($buildOk -and (Test-Path $srv)) {
        try { & $srv --version 2>&1 | Out-Null; $verifyOk = ($LASTEXITCODE -eq 0) } catch { $verifyOk = $false }
      }

      if (-not $verifyOk) {
        Write-Host "Update verification failed (build ok=$buildOk, binary ok=$verifyOk) — rolling back the build output." -ForegroundColor Red
        if (Restore-BuildOutput -Path $binDir -BakPath $bak) {
          Write-Host "Rolled bin/ back to the previous build. Your install is unchanged." -ForegroundColor Yellow
        }
        break
      }
      Remove-BuildOutputBackup -Path $binDir -BakPath $bak
      Write-Host "Rebuild verified." -ForegroundColor Green
    }

    # Regenerate versions.lock so it reflects the new installed set, and give a full doctor readout.
    Write-VersionsLock | Out-Null
    Write-Host "Running bob doctor..." -ForegroundColor DarkGray
    & "$repo\scripts\bob.ps1" doctor
    Write-Host "Update complete (release $(Get-BobVersion))." -ForegroundColor Green
  }
  # 'version' -> runtime=python (cli.py _handle_version over scripts/tools/health.py:version_info);
  # routed by the dispatch prologue before this switch. (ONE-C Slice 3)
  # 'verify-urls' -> runtime=python (cli.py _handle_verify_urls over scripts/tools/models.py:verify_urls);
  # routed by the dispatch prologue before this switch. (ONE-C Slice 4)
  'mlock' {
    # Check current status first (no admin needed)
    $mlockStatus = & "$repo\scripts\grant-mlock.ps1" -Check 2>&1
    Write-Host $mlockStatus
    if ($LASTEXITCODE -ne 0) {
      Write-Host ""
      Write-Host "This grants the Windows SeLockMemoryPrivilege to your user account." -ForegroundColor DarkGray
      Write-Host "Required for --mlock to actually pin model weights in RAM." -ForegroundColor DarkGray
      Write-Host "A UAC prompt will appear. After granting, restart this terminal." -ForegroundColor DarkGray
      Write-Host ""
      $ans = Read-Host "Grant now? [y/N]"
      if ($ans -match '^[Yy]') {
        & "$repo\scripts\grant-mlock.ps1"
      }
    }
  }
  'fabric-setup' { & "$repo\scripts\setup-fabric.ps1" }
  # 'fabric' -> runtime=python (cli.py _handle_fabric); routed by the dispatch prologue. (ONE-C Slice 1)
  # 'litellm' and 'services' -> runtime=python (cli.py handlers over scripts/tools/stack.py); routed
  # by the dispatch prologue before this switch. Cases deleted. (ONE-C Slice 2)
  'eval' {
    $eArgs = @{}
    $pos = @()
    for ($i = 0; $i -lt $rest.Count; $i++) {
      if ($rest[$i] -eq '--shots' -and $i+1 -lt $rest.Count) { $eArgs['Shots'] = [int]$rest[++$i] }
      elseif ($rest[$i] -eq '--limit' -and $i+1 -lt $rest.Count) { $eArgs['Limit'] = [int]$rest[++$i] }
      else { $pos += $rest[$i] }
    }
    if ($pos.Count -ge 1) { $eArgs['Role'] = $pos[0] }
    if ($pos.Count -ge 2) { $eArgs['Task'] = $pos[1] }
    & "$repo\scripts\eval.ps1" @eArgs
  }
  # ONE-C Slice 1: remember/recall/memory/budget are now runtime=python (cli.py handlers over
  # bob_core memory + bob_memory.py + scripts/tools/budget.py); the dispatch prologue routes them to
  # `python -m bob` before this switch. Cases deleted.

  # 'setup' (check) + 'doctor' -> runtime=python (cli.py _handle_setup/_handle_doctor over
  # scripts/tools/health.py:health_check, doctor=False/True); routed by the dispatch prologue before
  # this switch. Full first-run setup stays in setup.bat/setup.sh (ONE-D). (ONE-C Slice 3)

  # ── Phase 2: Voice + Vision ─────────────────────────────────────────────────
  'setup-voice' { & "$repo\scripts\setup-voice.ps1" $(if ($rest -contains '-Force') { '-Force' }) }

  # 'whisper' and 'piper' -> runtime=python (cli.py handlers over scripts/tools/stack.py control fns);
  # routed by the dispatch prologue before this switch. Cases deleted. (ONE-C Slice 2)

  # 'agent' (+ run/schedule/log/install/uninstall/status/serve/mcp/tools) -> runtime=python; the whole
  # case is dead now that every agent subcommand routes through the dispatch prologue. Scheduling lives
  # in scripts/tools/schedule.py + scripts/bob_agent_runner.py; the pwsh runner (bob-agent.ps1) + the
  # _platform.ps1/_models.ps1 scheduler seams (Get-AgentTaskSpec/Register/Unregister/Status/
  # Test-CrontabAvailable/Test-CronDue) were retired with it. (ONE-C Slice 5)

  'clip' {
    if (-not $rest.Count) { Write-Host "Usage: bob clip <url> [--note <text>]"; break }
    $venvPy = Get-VenvExe -Venv 'venv-litellm' -Exe 'python'
    $env:PYTHONIOENCODING = 'utf-8'
    & $venvPy (Join-Path $repo 'scripts' 'bob_clip.py') @rest   # Join-Path: native python consumer
    $env:PYTHONIOENCODING = $null
  }

  # 'tools' and 'plugins' -> runtime=python (cli.py _handle_tools/_handle_plugins over the engine's
  # tool_loader.py + a plugins/ scan); routed by the dispatch prologue. Cases deleted. (ONE-C Slice 1)

  default {
    # Plugin fallback — run plugins/<cmd>/invoke.ps1 or invoke.py if it exists
    $pluginDir = Join-Path $repo "plugins\$cmd"
    if (Test-Path "$pluginDir\invoke.ps1") {
      & "$pluginDir\invoke.ps1" @rest
      break
    } elseif (Test-Path (Join-Path $pluginDir 'invoke.py')) {
      $venvPy = Get-VenvExe -Venv 'venv-litellm' -Exe 'python'
      $env:PYTHONIOENCODING = 'utf-8'
      & $venvPy (Join-Path $pluginDir 'invoke.py') @rest   # Join-Path: native python consumer
      $env:PYTHONIOENCODING = $null
      break
    }

    # WI-7 — one generated catalog: `help` and any unknown verb render from the command registry via
    # `python -m bob help` (the old hand-maintained here-string is retired). Falls back to a one-liner
    # only if the venv is missing (pre-bootstrap).
    $venvPy = Get-VenvExe -Venv 'venv-litellm' -Exe 'python'
    if (Test-Path $venvPy) {
      if ($cmd -ne 'help') { Write-Host "Unknown command: $cmd`n" -ForegroundColor Yellow }
      try { Get-BobConfig | Out-Null } catch {}
      $env:PYTHONPATH = Join-Path $repo 'scripts'
      $env:PYTHONIOENCODING = 'utf-8'
      & $venvPy -m bob help
    } else {
      Write-Host "bob — run scripts\bootstrap-litellm.ps1 to set up, then 'bob help'."
    }
    break
  }
}
