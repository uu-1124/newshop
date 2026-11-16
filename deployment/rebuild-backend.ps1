param(
    [string]$JavaHome = 'D:\develop\jdk-17',
    [string]$MavenPath = 'D:\develop\maven\bin\mvn.cmd',
    [string]$MavenRepository = 'D:\develop\cache\maven'
)
$ErrorActionPreference = 'Stop'
$workspace = Split-Path $PSScriptRoot -Parent
$previousJavaHome = $env:JAVA_HOME
$previousMallJavaHome = $env:MALL_JAVA_HOME
$staging = Join-Path $PSScriptRoot '.run\new-jars'
$backup = Join-Path $PSScriptRoot ('.run\jar-backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
$services = @('admin','portal','search','auth','gateway','monitor')
$agentConfigPath = Join-Path $PSScriptRoot '.run\agent-env.json'
$agentEnabled = (Test-Path -LiteralPath $agentConfigPath) -and (Get-Content -LiteralPath $agentConfigPath -Raw | ConvertFrom-Json).enabled
$agentJars = Join-Path $PSScriptRoot '.run\agent-jars'
try {
    $env:JAVA_HOME = $JavaHome
    $env:MALL_JAVA_HOME = $JavaHome
    Push-Location (Join-Path $workspace 'mall-swarm')
    try {
        & $MavenPath "-Dmaven.repo.local=$MavenRepository" '-Dmaven.test.skip=true' '-Ddocker.skip=true' package
        if ($LASTEXITCODE -ne 0) { throw 'Compilation failed; running services were left in place.' }
    } finally { Pop-Location }
    New-Item -ItemType Directory -Path $staging -Force | Out-Null
    New-Item -ItemType Directory -Path $backup -Force | Out-Null
    foreach ($service in $services) {
        Copy-Item -LiteralPath (Join-Path $workspace "mall-swarm\mall-$service\target\mall-$service-1.0-SNAPSHOT.jar") -Destination (Join-Path $staging "mall-$service.jar") -Force
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot "jars\mall-$service.jar") -Destination $backup
    }
    if ($agentEnabled) {
        foreach ($service in @('portal','gateway','admin')) {
            $active = Join-Path $agentJars "mall-$service.jar"
            if (-not (Test-Path -LiteralPath $active)) { throw "Missing active agent package: $service" }
            Copy-Item -LiteralPath $active -Destination (Join-Path $backup "agent-$service.jar")
        }
    }
    & (Join-Path $PSScriptRoot 'stop-local.ps1')
    try {
        foreach ($service in $services) {
            Copy-Item -LiteralPath (Join-Path $staging "mall-$service.jar") -Destination (Join-Path $PSScriptRoot "jars\mall-$service.jar") -Force
        }
        if ($agentEnabled) {
            foreach ($service in @('portal','gateway','admin')) {
                Copy-Item -LiteralPath (Join-Path $staging "mall-$service.jar") -Destination (Join-Path $agentJars "mall-$service.jar") -Force
            }
        }
    } catch {
        foreach ($service in $services) {
            Copy-Item -LiteralPath (Join-Path $backup "mall-$service.jar") -Destination (Join-Path $PSScriptRoot "jars\mall-$service.jar") -Force
        }
        if ($agentEnabled) {
            foreach ($service in @('portal','gateway','admin')) {
                Copy-Item -LiteralPath (Join-Path $backup "agent-$service.jar") -Destination (Join-Path $agentJars "mall-$service.jar") -Force
            }
        }
        & (Join-Path $PSScriptRoot 'start-local.ps1')
        throw
    }
    & (Join-Path $PSScriptRoot 'start-local.ps1')
    Write-Output "Compiled packages installed. Startup logs: $PSScriptRoot\.run ; previous packages: $backup"
} finally { $env:JAVA_HOME = $previousJavaHome; $env:MALL_JAVA_HOME = $previousMallJavaHome }
