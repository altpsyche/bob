#requires -Version 7
# Syncs model system prompts from config/models.psd1 -> Open WebUI database.
# Run automatically on `bob gen`. Safe to run manually anytime.
# Skips gracefully if webui.db does not exist yet (first run before `bob webui`).

param([string]$Profile)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\_models.ps1"

$dbPath = Join-Path $script:ModelsRepo 'tools\webui-data\webui.db'
if (-not (Test-Path $dbPath)) {
    Write-Host "gen-webui: webui.db not found — skipping (run 'bob webui' once to create it)" -ForegroundColor DarkGray
    return
}

$data   = Get-Models -Profile $Profile
$cfg    = $data.config
$models = $data.models
$peers  = Get-EnabledPeers -Config $cfg

$entries = [System.Collections.Generic.List[hashtable]]::new()

# Local models — prompt from top-level cfg.prompts keyed by role name
foreach ($m in $models) {
    if ($m.embedding -or $m.role -in @('fim', 'embed')) { continue }
    $prompt = if ($cfg.prompts -and $cfg.prompts.ContainsKey($m.role)) { $cfg.prompts[$m.role] } else { '' }
    $entries.Add(@{ id = $m.role; prompt = "$prompt" })
}

# Pro models — prompt from peer.pro[role].systemPrompt (hashtable form only)
foreach ($peer in $peers) {
    if (-not $peer.pro) { continue }
    foreach ($role in ($peer.pro.Keys | Sort-Object)) {
        $rv     = $peer.pro[$role]
        $prompt = if ($rv -is [hashtable] -and $rv.systemPrompt) { $rv.systemPrompt } else { '' }
        $entries.Add(@{ id = "$role-pro"; prompt = "$prompt" })
    }
}

# Write entries via Python (PowerShell has no native SQLite support). Build the .py name directly
# (GetTempFileName() + '.py' would create AND leak a zero-byte base temp file every run).
$tmpJson = [System.IO.Path]::GetTempFileName()
$tmpPy   = Join-Path ([System.IO.Path]::GetTempPath()) "bob-genwebui-$PID.py"

try {
    $entries | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $tmpJson -Encoding utf8

    @'
import sqlite3, json, time, sys

db_path    = sys.argv[1]
input_path = sys.argv[2]

with open(input_path, encoding='utf-8') as f:
    entries = json.load(f)
if not isinstance(entries, list):
    entries = [entries]

# Short busy timeout so a running Open WebUI (which holds the write lock) makes us SKIP with a
# clear message instead of blocking `bob gen` indefinitely. Prompts are regenerated next run.
try:
    db  = sqlite3.connect(db_path, timeout=3)
    cur = db.cursor()
    cur.execute("SELECT id FROM user WHERE role='admin' LIMIT 1")
    row = cur.fetchone()
    if not row:
        print('gen-webui: no admin user found — skipping', flush=True)
        db.close(); sys.exit(0)
    admin_id = row[0]
    now_ms   = int(time.time() * 1000)

    for e in entries:
        eid    = e['id']
        prompt = (e.get('prompt') or '').strip()
        params = json.dumps({'system': prompt}) if prompt else '{}'
        # Preserve created_at on update so the row does not appear newly created each run
        cur.execute(
            """INSERT OR REPLACE INTO model
               (id, user_id, base_model_id, name, params, meta, updated_at, created_at, is_active)
               VALUES (?,?,?,?,?,?,?,COALESCE((SELECT created_at FROM model WHERE id=?),?),1)""",
            (eid, admin_id, eid, eid, params, '{}', now_ms, eid, now_ms)
        )
        label = 'set' if prompt else 'cleared'
        print(f'  {eid}: system prompt {label}', flush=True)

    db.commit()
    db.close()
except sqlite3.OperationalError as ex:
    if 'locked' in str(ex).lower():
        print('gen-webui: webui.db is locked (Open WebUI running?) — skipping; '
              're-run `bob gen` after stopping WebUI.', flush=True)
        sys.exit(0)
    raise
'@ | Set-Content -LiteralPath $tmpPy -Encoding utf8

    # NC4: use the venv python seam, not a bare `python` off PATH.
    $py = Get-VenvExe -Venv 'venv-litellm' -Exe 'python'
    & $py $tmpPy $dbPath $tmpJson

} finally {
    if (Test-Path $tmpJson) { Remove-Item $tmpJson }
    if (Test-Path $tmpPy)   { Remove-Item $tmpPy }
}

Write-Host "Generated Open WebUI model system prompts" -ForegroundColor Green
