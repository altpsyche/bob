#requires -Version 7
# GENERATE config/llama-swap.yaml from config/models.psd1 (the single source of truth).
# Deterministic, idempotent. Re-run any time; also runs automatically on `bob serve`.
#   .\scripts\gen-llama-swap.ps1            # active profile (or $env:BOB_PROFILE)
#   .\scripts\gen-llama-swap.ps1 12gb       # a specific profile
param([string]$Profile)
$ErrorActionPreference = "Stop"
. "$PSScriptRoot\_models.ps1"

$repo = Split-Path $PSScriptRoot -Parent
$out  = Join-Path $repo 'config\llama-swap.yaml'

$resolved = Get-Models -Profile $Profile
$name     = $resolved.profile
$cfg      = $resolved.config
$models   = $resolved.models

# NC8 — the server binary name is OS-aware (llama-server.exe on Windows, llama-server on Linux) so a
# Linux/CPU build's generated cmd points at the right file. The '${env.LLAMA_LOCAL_ROOT}' token stays
# a llama-swap template var (single-quoted), only the exe leaf is appended.
$srvBin = '${env.LLAMA_LOCAL_ROOT}/bin/' + (Get-BobExeName 'llama-server')
# CPU tier: no GPU offload, flash-attn/KV-quant off (unsupported / regressive on the CPU backend).
$isCpu  = ($name -eq 'cpu')

# --- build macros from defaults (overrides the empty placeholder values in models.psd1) ---
$d = $cfg.defaults
if (-not $d) { throw 'models.psd1 is missing the defaults block. Add it per MODULE-A docs.' }

$ngl   = if ($isCpu) { 0 } elseif ($null -ne $d.ngl) { $d.ngl } else { 99 }
$fa    = if ($d.flashAttn -ne $false -and -not $isCpu) { '--flash-attn on' } else { '' }
$batch = if ($d.batch -and $d.batch -ne 512)  { "-b $($d.batch)" }   else { '' }
$ub    = if ($d.ubatch -and $d.ubatch -ne 512) { "-ub $($d.ubatch)" } else { '' }
$par   = if ($d.parallel -and $d.parallel -gt 1) { "-np $($d.parallel)" } else { '' }
$thr   = if ($d.threads -and $d.threads -gt 0)   { "-t $($d.threads)" }   else { '' }
$numa  = if ($d.numa -and $d.numa -ne '')  { "--numa $($d.numa)" }   else { '' }
$srvParts = @(
    $srvBin,
    '--port ${PORT}',
    "-ngl $ngl",
    $fa, $batch, $ub, $numa, $par, $thr
) | Where-Object { $_ -ne '' }
$cfg.macros['srv'] = $srvParts -join ' '

# kvQuant (legacy) overrides both axes when non-empty; kvQuantK/kvQuantV allow asymmetric control.
$legacyKv = if ($null -ne $d.kvQuant -and $d.kvQuant -ne '') { $d.kvQuant } else { $null }
$kvQuantK = if ($legacyKv)              { $legacyKv } `
             elseif ($null -ne $d.kvQuantK) { $d.kvQuantK } `
             else                           { 'q8_0' }
$kvQuantV = if ($legacyKv)              { $legacyKv } `
             elseif ($null -ne $d.kvQuantV) { $d.kvQuantV } `
             else                           { 'q8_0' }
$cfg.macros['kv'] = if ($isCpu) { '' } elseif ($kvQuantK -or $kvQuantV) {
    "--cache-type-k $kvQuantK --cache-type-v $kvQuantV"
} else { '' }

# --- format helpers (InvariantCulture so 0.7 never becomes "0,7" on EU locales) ---
$inv = [System.Globalization.CultureInfo]::InvariantCulture
function Fmt($v) {
  if     ($v -is [bool])   { return $v.ToString().ToLower() }
  elseif ($v -is [double] -or $v -is [single]) { return $v.ToString($inv) }
  elseif ($v -is [int] -or $v -is [long])      { return $v.ToString($inv) }
  else   { return [string]$v }
}
function Assert-NoQuote($s, $what) { if ($s -match '"') { throw "value for $what contains a double-quote, which would break the generated YAML: $s" } }

# --- build each model's cmd string (order matters; mirrors the hand-written config) ---
$members   = @($cfg.group.members)
$roleNames = $models.role
foreach ($m in $models) {
  Assert-NoQuote $m.gguf "model '$($m.role)' gguf"
  if ($m.gguf -match 'gemma' -and $m.kv -eq $true) {
    Write-Warning "[$($m.role)] Gemma model with kv=`$true — KV quant causes quality regression. Set kvQuant='' in config/user.psd1 or set kv=`$false on the model."
  }
  # Flash-attn is incompatible with multimodal projection (mmproj); auto-expand srv without it.
  $srvRef = if ($m.mmproj -and $fa -ne '') {
    (@($srvBin, '--port ${PORT}', "-ngl $ngl", $batch, $ub, $numa, $par, $thr) | Where-Object { $_ -ne '' }) -join ' '
  } else { '${srv}' }
  $parts = @($srvRef, "-m `${env.LLAMA_LOCAL_ROOT}/models/$($m.gguf)")
  if ($null -ne $m.ctx)              { $parts += "-c $(Fmt $m.ctx)" }
  if ($m.kv)                         { $parts += '${kv}' }
  if ($m.embedding)                  { $parts += '--embedding' }
  if ($m.flags) { foreach ($f in $m.flags) { Assert-NoQuote $f "model '$($m.role)' flag"; $parts += [string]$f } }
  # mlock: per-model flag OR global mlockBig applied to swap-group members.
  $globalMlockBig = ($d.mlockBig -eq $true)
  $isSwapMember   = $members -contains $m.role
  $applyMlock     = ($m.mlock -eq $true) -or ($globalMlockBig -and $isSwapMember)
  if ($applyMlock) { $parts += '--mlock' }
  # no-mmap: per-model flag overrides global default.
  $globalNoMmap = ($d.noMmap -eq $true)
  $modelNoMmap  = if ($null -ne $m.noMmap) { $m.noMmap -eq $true } else { $globalNoMmap }
  if ($modelNoMmap) { $parts += '--no-mmap' }
  if ($m.draftRole) {
    $draftModel = $models | Where-Object { $_.role -eq $m.draftRole } | Select-Object -First 1
    if (-not $draftModel) {
      Write-Warning "[$($m.role)] draftRole '$($m.draftRole)' not found in profile — speculative decoding disabled."
    } elseif ($draftModel.pinned -ne $true) {
      Write-Warning "[$($m.role)] draftRole '$($m.draftRole)' is not pinned — draft must be in VRAM. Skipping."
    } else {
      $parts += "-md `${env.LLAMA_LOCAL_ROOT}/models/$($draftModel.gguf)"
      $parts += '-ngld 99'
    }
  }
  if ($m.mmproj) {
    Assert-NoQuote $m.mmproj "model '$($m.role)' mmproj"
    $parts += "--mmproj `${env.LLAMA_LOCAL_ROOT}/models/$($m.mmproj)"
  }
  $m | Add-Member -NotePropertyName _cmd -NotePropertyValue ($parts -join ' ')
}

# --- group assertions (catch hand-edit mistakes in the PSD1) ---
$activeMembers = [System.Collections.Generic.List[string]]::new()
foreach ($mem in $members) {
  if ($roleNames -notcontains $mem) {
    Write-Warning "group member '$mem' not in profile '$name' — skipping in swap group (add it to this profile to enable)"
    continue
  }
  $mObj = $models | Where-Object role -eq $mem
  if ($mObj.pinned) { throw "model '$mem' is pinned but also listed in group.members — pinned models must stay out of the swap group" }
  $activeMembers.Add($mem)
}

# --- emit YAML (deterministic; matches the schema llama-swap reads) ---
$sb = [System.Text.StringBuilder]::new()
$nl = "`n"
[void]$sb.Append("# =============================================================$nl")
[void]$sb.Append("#  GENERATED - DO NOT EDIT.  Source: config/models.psd1$nl")
[void]$sb.Append("#  Regenerate: scripts/gen-llama-swap.ps1  (also runs on ``bob serve``)$nl")
[void]$sb.Append("#  Active profile: $name$nl")
[void]$sb.Append("# =============================================================$nl$nl")

# macros (srv, kv first, then any extras alphabetically)
[void]$sb.Append("macros:$nl")
$macroOrder = @('srv', 'kv') + @($cfg.macros.Keys | Where-Object { $_ -ne 'srv' -and $_ -ne 'kv' } | Sort-Object)
foreach ($k in $macroOrder) {
  if (-not $cfg.macros.Contains($k)) { continue }
  $val = [string]$cfg.macros[$k]
  Assert-NoQuote $val "macro '$k'"
  [void]$sb.Append("  ${k}: `"$val`"$nl")
}
[void]$sb.Append($nl)

# models
[void]$sb.Append("models:$nl")
foreach ($m in $models) {
  [void]$sb.Append("  $($m.role):$nl")
  [void]$sb.Append("    cmd: `"$($m._cmd)`"$nl")
  if ($m.setParams) {
    $pairs = @($m.setParams.Keys | Sort-Object | ForEach-Object { "${_}: $(Fmt $m.setParams[$_])" }) -join ', '
    [void]$sb.Append("    filters:$nl")
    [void]$sb.Append("      setParams: { $pairs }$nl")
  }
  if ($null -ne $m.ttl) { [void]$sb.Append("    ttl: $(Fmt $m.ttl)$nl") }
  [void]$sb.Append($nl)
}

# groups
[void]$sb.Append("groups:$nl")
[void]$sb.Append("  $($cfg.group.name):$nl")
[void]$sb.Append("    swap: $(Fmt $cfg.group.swap)$nl")
[void]$sb.Append("    members: [$(@($activeMembers) -join ', ')]$nl")

Set-Content -LiteralPath $out -Value $sb.ToString() -NoNewline -Encoding utf8
Write-Host "generated $out  (profile: $name)" -ForegroundColor Green
