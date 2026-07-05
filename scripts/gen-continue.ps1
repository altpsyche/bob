#requires -Version 7
# Generates config/continue/config.yaml from config/models.psd1 (+ user.psd1 overrides).
# Mirrors gen-litellm.ps1 / gen-webui.ps1: the models: section is derived from Get-Models
# (role -> model, profile ctx -> contextLength, models.psd1 prompts -> systemMessage), apiBase
# from the litellm port, apiKey from the litellmKey secret seam (C3). The mcpServers block is
# TEMPLATED with portable defaults (repo root + $HOME/dev, ${GITHUB_TOKEN}) — never a personal path.
#
# Run automatically on `bob gen` and by setup-clients.ps1. Safe to run manually anytime.

param([string]$Profile)
$ErrorActionPreference = "Stop"   # a mid-generation cmdlet failure must abort, not emit a partial YAML

. "$PSScriptRoot\_models.ps1"

$data    = Get-Models -Profile $Profile
$cfg     = $data.config
$models  = $data.models
$peers   = Get-EnabledPeers -Config $cfg
$bobCfg  = Get-BobConfig
$litellmPort = $bobCfg.litellmPort ?? (Get-BobPortDefault 'litellmPort')
$searxngPort = $bobCfg.searxngPort ?? (Get-BobPortDefault 'searxngPort')
$litellmKey  = Get-Secret -Name 'litellmKey' -Default ($bobCfg.litellmKey ?? 'sk-local')
$apiBase     = "http://localhost:$litellmPort/v1"
$repo        = $script:ModelsRepo
$homeDev     = Join-Path $HOME 'dev'

# Double-quoted YAML scalar escaper: backslash + quote escaped, newlines flattened to spaces.
function ConvertTo-YamlString([string]$s) {
    $e = $s -replace '\\', '\\' -replace '"', '\"' -replace "`r?`n", ' '
    return '"' + $e + '"'
}

# Continue `roles` per model role (base + pro). Names that differ from the role are mapped too.
$roleAssign = @{
    coder = @('chat', 'edit', 'apply'); chat = @('chat'); planner = @('chat', 'edit')
    vision = @('chat'); fim = @('autocomplete'); embed = @('embed')
}
$proAssign = @{
    chat = @('chat', 'edit'); coder = @('chat', 'edit', 'apply'); planner = @('chat'); vision = @('chat')
}
$nameFor = @{ fim = 'autocomplete'; embed = 'embeddings' }

$out = [System.Collections.Generic.List[string]]::new()
$out.Add('# GENERATED - DO NOT EDIT.  Source: config/models.json  (+ config/user.json)')
$out.Add('# Regenerate: scripts/gen-continue.ps1  (also runs on `bob gen`)')
$out.Add('# Continue.dev config (2026 YAML format). Symlinked to ~/.continue/config.yaml by setup-clients.ps1.')
$out.Add('name: bob')
$out.Add('version: 0.0.1')
$out.Add('schema: v1')
$out.Add('')
$out.Add('models:')

function Add-Model {
    param([string]$Name, [string]$Model, [int]$Ctx, [string]$Prompt, [string[]]$Roles)
    $out.Add("  - name: $Name")
    $out.Add("    provider: openai")
    $out.Add("    model: $Model")
    $out.Add("    apiBase: $apiBase")
    $out.Add("    apiKey: $litellmKey")
    if ($Ctx -gt 0)  { $out.Add("    contextLength: $Ctx") }
    if ($Roles)      { $out.Add("    roles: [$($Roles -join ', ')]") }
    if ($Prompt)     { $out.Add("    systemMessage: $(ConvertTo-YamlString $Prompt)") }
}

# Local models (skip 'agent' — that's Bob's own function-calling model, not a Continue client model)
foreach ($m in $models) {
    if ($m.role -eq 'agent') { continue }
    $name   = if ($nameFor.ContainsKey($m.role)) { $nameFor[$m.role] } else { $m.role }
    $ctx    = if ($m.embedding) { 0 } else { [int]($m.ctx ?? 0) }
    $prompt = if ($cfg.prompts -and $cfg.prompts.ContainsKey($m.role)) { "$($cfg.prompts[$m.role])" } else { '' }
    $roles  = if ($roleAssign.ContainsKey($m.role)) { $roleAssign[$m.role] } else { @('chat') }
    Add-Model -Name $name -Model $m.role -Ctx $ctx -Prompt $prompt -Roles $roles
    $out.Add('')
}

# Pro models — one {role}-pro entry per enabled peer role
foreach ($peer in $peers) {
    if (-not $peer.pro) { continue }
    foreach ($role in ($peer.pro.Keys | Sort-Object)) {
        $rv     = $peer.pro[$role]
        $prompt = if ($rv -is [hashtable] -and $rv.systemPrompt) { "$($rv.systemPrompt)" } else { '' }
        $roles  = if ($proAssign.ContainsKey($role)) { $proAssign[$role] } else { @('chat') }
        Add-Model -Name "$role-pro" -Model "$role-pro" -Ctx 0 -Prompt $prompt -Roles $roles
        $out.Add('')
    }
}

# mcpServers — templated portable defaults (repo root + $HOME/dev), never a committed personal path.
$out.Add('mcpServers:')
$out.Add('  - name: filesystem')
$out.Add('    command: npx')
$out.Add('    args:')
$out.Add('      - "-y"')
$out.Add('      - "@modelcontextprotocol/server-filesystem"')
$out.Add("      - $(ConvertTo-YamlString $homeDev)")
$out.Add("      - $(ConvertTo-YamlString $repo)")
$out.Add('  - name: fetch')
$out.Add('    command: uvx')
$out.Add('    args:')
$out.Add('      - "mcp-server-fetch"')
$out.Add('  - name: github')
$out.Add('    command: npx')
$out.Add('    args:')
$out.Add('      - "-y"')
$out.Add('      - "@modelcontextprotocol/server-github"')
$out.Add('    env:')
$out.Add('      GITHUB_PERSONAL_ACCESS_TOKEN: "${GITHUB_TOKEN}"')
$out.Add('  - name: searxng-search')
$out.Add('    command: npx')
$out.Add('    args:')
$out.Add('      - "-y"')
$out.Add('      - "mcp-searxng"')
$out.Add('    env:')
$out.Add("      SEARXNG_URL: `"http://localhost:$searxngPort`"")

$outFile = Join-Path $repo 'config\continue\config.yaml'
$outDir  = Split-Path $outFile
if (-not (Test-Path $outDir)) { New-Item $outDir -ItemType Directory -Force | Out-Null }
($out -join "`n") + "`n" | Set-Content -LiteralPath $outFile -Encoding utf8 -NoNewline
Write-Host "Generated $outFile" -ForegroundColor Green
