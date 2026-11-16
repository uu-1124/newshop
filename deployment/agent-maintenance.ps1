param(
    [Parameter(Mandatory)][ValidateSet('pause','resume','status')][string]$Mode,
    [Parameter(Mandatory)][Guid]$PlanId
)
$ErrorActionPreference='Stop'
$workspace=[System.IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
$marker=Join-Path $PSScriptRoot '.run\maintenance.json'
$owner=$PlanId.ToString()
function Read-Marker {
    if(Test-Path -LiteralPath $marker){return (Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json)}
    return $null
}
function Check-Owner {
    $state=Read-Marker
    if($state -and ($state.schema -ne 'agent-maintenance-v1' -or $state.plan_id -ne $owner)){
        throw 'Another maintenance plan owns this environment; no processes changed.'
    }
    return $state
}
if($Mode -eq 'status'){
    $state=Read-Marker
    if($state){Write-Output ($state | ConvertTo-Json -Compress)}else{Write-Output '{"active":false}'}
    exit 0
}
$existing=Check-Owner
if($Mode -eq 'resume'){
    if(-not $existing){Write-Output 'Maintenance already released.';exit 0}
    # Delete only the exact marker owned by this operation. Pending manager timers
    # restart writers; no database, object or unrelated file is removed.
    Remove-Item -LiteralPath $marker
    Write-Output "Maintenance released for $owner; manager will restart writers."
    exit 0
}
if(-not $existing){
    $state=@{schema='agent-maintenance-v1';plan_id=$owner;state='PAUSING';created_at=[DateTime]::UtcNow.ToString('o')}
    $bytes=[Text.Encoding]::UTF8.GetBytes(($state | ConvertTo-Json))
    $stream=[System.IO.File]::Open($marker,[System.IO.FileMode]::CreateNew,[System.IO.FileAccess]::Write,[System.IO.FileShare]::Read)
    try{$stream.Write($bytes,0,$bytes.Length)}finally{$stream.Dispose()}
}
$names=@('gateway','agent-api','agent-generation','agent-jobs','agent-scheduler','agent-outbox','agent-observability','portal','admin','auth','search','monitor')
foreach($name in $names){
    $saved=Get-Content -LiteralPath (Join-Path $PSScriptRoot '.run\pids.json') -Raw | ConvertFrom-Json
    $entry=$saved.children | Where-Object name -eq $name
    if(-not $entry.pid){continue}
    $process=Get-CimInstance Win32_Process -Filter "ProcessId = $($entry.pid)" -ErrorAction SilentlyContinue
    if(-not $process){continue}
    if(-not $process.CommandLine -or -not $process.CommandLine.Contains($workspace)){
        throw "Managed process identity mismatch for $name; maintenance remains active."
    }
    & taskkill.exe /PID $entry.pid /T /F | Out-Null
    if($LASTEXITCODE -ne 0 -and (Get-Process -Id $entry.pid -ErrorAction SilentlyContinue)){
        throw "Could not stop verified writer $name; maintenance remains active."
    }
}
for($attempt=0;$attempt -lt 30;$attempt++){
    $saved=Get-Content -LiteralPath (Join-Path $PSScriptRoot '.run\pids.json') -Raw | ConvertFrom-Json
    $remaining=@($saved.children | Where-Object {$_.name -in $names -and $_.pid} | Where-Object {
        Get-Process -Id $_.pid -ErrorAction SilentlyContinue
    })
    if(-not $remaining.Count){break}
    Start-Sleep -Milliseconds 500
}
if($remaining.Count){throw 'Writer processes remain; no snapshot may be labeled consistent.'}
$state=Check-Owner
$state.state='PAUSED'
[System.IO.File]::WriteAllText($marker,($state | ConvertTo-Json),(New-Object System.Text.UTF8Encoding($false)))
Write-Output "All managed writers paused for $owner."
