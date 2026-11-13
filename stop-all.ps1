[CmdletBinding()]
param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$StatePath = "E:\app_press_test\frontend\runtime-local\launcher\services.json"

function Stop-ProcessTree {
    param(
        [int]$ProcessId,
        [string]$ExpectedStartTime
    )

    if ($ProcessId -le 0) {
        return $false
    }
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return $false
    }
    try {
        $actualStartTime = $process.StartTime.ToUniversalTime().ToString("o")
    }
    catch {
        return $false
    }
    if (
        [string]::IsNullOrWhiteSpace($ExpectedStartTime) -or
        $actualStartTime -ne $ExpectedStartTime
    ) {
        Write-Warning ("Skipping PID {0}: process identity no longer matches launcher state." -f $ProcessId)
        return $false
    }
    try {
        & taskkill.exe /PID $ProcessId /T /F 2>$null | Out-Null
    }
    catch {
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    }
    return $true
}

if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) {
    Write-Host "No launcher state was found; no external service was stopped."
    exit 0
}

try {
    $state = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
}
catch {
    throw ("Unable to read launcher state {0}: {1}" -f $StatePath, $_.Exception.Message)
}

if (-not $Force) {
    $backendUrl = [string]$state.backend.url
    if (-not [string]::IsNullOrWhiteSpace($backendUrl)) {
        $runsUrl = $backendUrl -replace "/api/health$", "/api/runs"
        try {
            $response = Invoke-WebRequest -Uri $runsUrl -UseBasicParsing -TimeoutSec 30
            $runs = @((($response.Content | ConvertFrom-Json).runs))
            $terminalStates = @("completed", "failed", "stopped", "cancelled", "manually_ended")
            $activeRuns = @($runs | Where-Object { [string]$_.status -notin $terminalStates })
            if ($activeRuns.Count -gt 0) {
                Write-Host ("Refusing to stop services: {0} test job(s) are active." -f $activeRuns.Count) -ForegroundColor Yellow
                Write-Host "Use .\stop-all.ps1 -Force only when interruption is intentional."
                exit 2
            }
        }
        catch {
            Write-Warning "Could not verify active test jobs; services were left running."
            exit 2
        }
    }
}

$stopped = New-Object System.Collections.Generic.List[string]
foreach ($name in @("backend", "frontend")) {
    $service = $state.$name
    if ($null -eq $service -or -not [bool]$service.owned -or $null -eq $service.pid) {
        continue
    }
    if (Stop-ProcessTree -ProcessId ([int]$service.pid) -ExpectedStartTime ([string]$service.processStartedAt)) {
        $stopped.Add($name)
    }
}

Remove-Item -LiteralPath $StatePath -Force -ErrorAction SilentlyContinue
if ($stopped.Count -eq 0) {
    Write-Host "Launcher-owned services have already exited; no other process was stopped."
}
else {
    Write-Host ("Stopped: {0}" -f ($stopped -join ", ")) -ForegroundColor Green
}
