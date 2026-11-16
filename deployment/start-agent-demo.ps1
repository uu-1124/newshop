$ErrorActionPreference = 'Stop'
$workspace = Split-Path $PSScriptRoot -Parent
$agentRoot = Join-Path $workspace 'mall-agent'
$python = Join-Path $agentRoot '.venv\Scripts\python.exe'
$run = Join-Path $PSScriptRoot '.run'
$configPath = Join-Path $run 'agent-env.json'
if (-not (Test-Path -LiteralPath $python)) { throw 'First run uv sync --python 3.13 in mall-agent.' }
$node = 'D:\develop\nodejs\node.exe'
$config = if (Test-Path -LiteralPath $configPath) { Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json }
if ($config.postgresMode -ne 'cloud') { throw 'First run node deployment/prepare-agent.cjs.' }
$pidFile = Join-Path $run 'pids.json'
if (Test-Path -LiteralPath $pidFile) {
    $saved = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
    $existing = Get-CimInstance Win32_Process -Filter "ProcessId = $($saved.manager)"
    if ($existing.CommandLine -and $existing.CommandLine.Contains((Join-Path $PSScriptRoot 'dev.cjs'))) {
        throw 'Local environment is already running. Stop it before switching the Java packages or migrating.'
    }
}
$jars = Join-Path $run 'agent-jars'
New-Item -ItemType Directory -Path $jars -Force | Out-Null
foreach ($service in @('portal', 'gateway', 'admin')) {
    $source = Join-Path $workspace "mall-swarm\mall-$service\target\mall-$service-1.0-SNAPSHOT.jar"
    if (-not (Test-Path -LiteralPath $source)) { throw "Build mall-$service with Maven first." }
    Copy-Item -LiteralPath $source -Destination (Join-Path $jars "mall-$service.jar") -Force
}
& $node (Join-Path $PSScriptRoot 'migrate-agent.cjs')
if ($LASTEXITCODE -ne 0) { throw 'Agent database migration failed.' }
$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$config.enabled = $true
$config | ConvertTo-Json | Set-Content -LiteralPath $configPath -Encoding utf8NoBOM
& (Join-Path $PSScriptRoot 'start-local.ps1')
