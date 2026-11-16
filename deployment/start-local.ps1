param([string]$NodePath = 'D:\develop\nodejs\node.exe')
$ErrorActionPreference = 'Stop'
$runDirectory = Join-Path $PSScriptRoot '.run'
New-Item -ItemType Directory -Path $runDirectory -Force | Out-Null
$pidFile = Join-Path $runDirectory 'pids.json'
if (Test-Path -LiteralPath $pidFile) {
    $saved = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
    $existing = if ($saved.manager) { Get-CimInstance Win32_Process -Filter "ProcessId = $($saved.manager)" }
    if ($existing -and $existing.CommandLine -and $existing.CommandLine.Contains((Join-Path $PSScriptRoot 'dev.cjs'))) {
        Write-Output 'Local development environment is already running.'
        exit 0
    }
}
$scriptPath = Join-Path $PSScriptRoot 'dev.cjs'
$process = Start-Process -FilePath $NodePath -ArgumentList ('"' + $scriptPath + '"') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runDirectory 'manager.log') -RedirectStandardError (Join-Path $runDirectory 'manager-error.log')
Write-Output "Started local environment manager PID $($process.Id). Logs: $runDirectory"
