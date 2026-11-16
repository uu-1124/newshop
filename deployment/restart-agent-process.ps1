param([Parameter(Mandatory)][ValidateSet('agent-api','agent-generation','agent-scheduler','agent-jobs','agent-outbox','agent-backups','agent-observability')][string]$Name)
$ErrorActionPreference = 'Stop'
$workspace = Split-Path $PSScriptRoot -Parent
$saved = Get-Content -LiteralPath (Join-Path $PSScriptRoot '.run\pids.json') -Raw | ConvertFrom-Json
$entry = $saved.children | Where-Object name -eq $Name
if (-not $entry.pid) { throw 'The selected process is not currently running.' }
$process = Get-CimInstance Win32_Process -Filter "ProcessId = $($entry.pid)"
if (-not $process.CommandLine -or -not $process.CommandLine.Contains((Join-Path $workspace 'mall-agent'))) {
    throw 'Process identity does not match the managed agent application.'
}
# Stop the verified Python launcher and its interpreter descendants; manager restarts the process.
& taskkill.exe /PID $entry.pid /T /F
if ($LASTEXITCODE -ne 0) { throw 'Agent process tree could not be stopped.' }
