$ErrorActionPreference = 'Stop'
$pidFile = Join-Path $PSScriptRoot '.run\pids.json'
if (-not (Test-Path -LiteralPath $pidFile)) { Write-Output 'No saved local processes.'; exit 0 }
$saved = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
$workspace = Split-Path $PSScriptRoot -Parent
foreach ($entry in @([pscustomobject]@{ name='manager'; pid=$saved.manager }) + @($saved.children)) {
    if (-not $entry.pid) { continue }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($entry.pid)"
    if ($process -and $process.CommandLine -and $process.CommandLine.Contains($workspace)) {
        # Python virtualenv launchers and Vite may have child processes.
        # Kill the verified workspace process tree so interpreters cannot keep ports open.
        & taskkill.exe /PID $entry.pid /T /F
        if ($LASTEXITCODE -ne 0 -and (Get-Process -Id $entry.pid -ErrorAction SilentlyContinue)) {
            throw "Could not stop $($entry.name) process tree."
        }
        Wait-Process -Id $entry.pid -Timeout 10 -ErrorAction SilentlyContinue
        Write-Output "Stopped $($entry.name) PID $($entry.pid)"
    }
}
