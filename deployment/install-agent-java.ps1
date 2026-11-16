param([Parameter(Mandatory)][ValidateSet('admin','portal','gateway')][string]$Service)
$ErrorActionPreference='Stop'
$workspace=Split-Path $PSScriptRoot -Parent
$runtime=Join-Path $PSScriptRoot ".run\agent-jars\mall-$Service.jar"
$source=Join-Path $workspace "mall-swarm\mall-$Service\target\mall-$Service-1.0-SNAPSHOT.jar"
$stage=Join-Path $PSScriptRoot ".run\agent-jars\mall-$Service.next.jar"
$backupFolder=Join-Path $PSScriptRoot '.run\order-return-direct-rollback-20261112'
if(-not (Test-Path -LiteralPath $source) -or -not (Test-Path -LiteralPath $runtime)){throw 'Verified source and current runtime package are required.'}
New-Item -ItemType Directory -Path $backupFolder -Force | Out-Null
$backup=Join-Path $backupFolder ("mall-$Service-"+(Get-Date -Format 'yyyyMMdd-HHmmss')+'.jar')
Copy-Item -LiteralPath $source -Destination $stage -Force
if((Get-FileHash -LiteralPath $source).Hash -ne (Get-FileHash -LiteralPath $stage).Hash){throw 'Staged package hash mismatch.'}
$saved=Get-Content -LiteralPath (Join-Path $PSScriptRoot '.run\pids.json') -Raw | ConvertFrom-Json
$entry=$saved.children | Where-Object name -eq $Service
$process=Get-CimInstance Win32_Process -Filter "ProcessId = $($entry.pid)"
if(-not $process.CommandLine -or -not $process.CommandLine.Contains($runtime)){throw 'Process identity does not match the managed package.'}
& taskkill.exe /PID $entry.pid /T /F
if($LASTEXITCODE -ne 0){throw 'Managed Java process could not be stopped.'}
# The manager restarts this service after five seconds. Replace a completely staged jar atomically.
[System.IO.File]::Replace($stage,$runtime,$backup)
Write-Output "Installed $Service package; manager will restart it. Previous package: $backup"
Get-FileHash -LiteralPath $runtime -Algorithm SHA256 | Select-Object Path,Hash
