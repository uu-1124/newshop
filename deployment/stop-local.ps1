$ErrorActionPreference = 'Stop'
$pidFile = Join-Path $PSScriptRoot '.run\pids.json'
if (-not (Test-Path -LiteralPath $pidFile)) { Write-Output 'No saved local processes.'; exit 0 }
$saved = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
$workspace = Split-Path $PSScriptRoot -Parent
foreach ($entry in @($saved.children) + @([pscustomobject]@{ name='manager'; pid=$saved.manager })) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($entry.pid)"
    if ($process -and $process.CommandLine.Contains($workspace)) {
        Stop-Process -Id $entry.pid -Force
        Wait-Process -Id $entry.pid -Timeout 10 -ErrorAction SilentlyContinue
        Write-Output "Stopped $($entry.name) PID $($entry.pid)"
    }
}
