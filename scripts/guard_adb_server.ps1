[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$PrimaryPort = 5037,

    [ValidateRange(1, 65535)]
    [int]$SecondaryPort = 5038,

    [string]$AdbPath = 'D:\Android\android-sdk\platform-tools\adb.exe',

    [ValidateRange(1, 10)]
    [int]$RequiredIdleChecks = 3,

    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$logRoot = 'E:\app_press_test\logs\adb_guard'
$logPath = Join-Path $logRoot 'adb_guard.log'
$statePath = Join-Path $logRoot 'adb_guard_state.json'
$lockPath = Join-Path $logRoot 'adb_guard.lock'
$lockStream = $null

try {
    [System.IO.Directory]::CreateDirectory($logRoot) | Out-Null
}
catch {
    # A missing log volume must never turn into an unobserved kill decision.
    exit 2
}

function Write-GuardLog {
    param([string]$Message)
    Add-Content -LiteralPath $logPath -Value ("{0} {1}" -f (Get-Date -Format o), $Message) -Encoding UTF8
}

try {
    $lockStream = [System.IO.File]::Open(
        $lockPath,
        [System.IO.FileMode]::OpenOrCreate,
        [System.IO.FileAccess]::ReadWrite,
        [System.IO.FileShare]::None
    )
}
catch {
    Write-GuardLog 'SKIP another guard invocation already holds the lock'
    exit 0
}

function Get-Listener {
    param([int]$Port)

    foreach ($line in @(netstat.exe -ano -p tcp 2>$null)) {
        $text = [string]$line
        if ($text -match '^\s*TCP\s+\S+:(\d+)\s+\S+\s+LISTENING\s+(\d+)\s*$' -and [int]$Matches[1] -eq $Port) {
            $pidValue = [int]$Matches[2]
            $process = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
            return [pscustomobject]@{
                port = $Port
                pid = $pidValue
                verified = ($null -ne $process -and $process.ProcessName -match '^adb$')
            }
        }
    }
    return $null
}

function Get-DeviceRows {
    param([int]$Port)

    $output = @(& $AdbPath -H 127.0.0.1 -P $Port devices 2>&1)
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw ("adb devices failed on port {0}: {1}" -f $Port, (($output | ForEach-Object { [string]$_ }) -join ' | '))
    }

    $rows = @()
    foreach ($line in $output) {
        $text = [string]$line
        if ($text -match '^\s*(\S+)\s+(device|offline|unauthorized)\b') {
            $rows += [pscustomobject]@{
                serial = $Matches[1]
                status = $Matches[2]
            }
        }
    }
    return $rows
}

function Test-RemotePressure {
    param([string]$Serial)

    $output = @(& $AdbPath -H 127.0.0.1 -P $SecondaryPort -s $Serial shell ps -A 2>&1)
    $exitCode = $LASTEXITCODE
    $text = ($output | ForEach-Object { [string]$_ }) -join "`n"
    if ($exitCode -ne 0) {
        return [pscustomobject]@{ state = 'unknown'; lines = @(); error = $text }
    }

    $lines = @($text -split "`r?`n" | Where-Object {
        $_ -match '(?i)com\.android\.commands\.monkey|\bmonkeyq\b|\bfastbot\b'
    })
    if ($lines.Count -gt 0) {
        return [pscustomobject]@{ state = 'busy'; lines = $lines; error = $null }
    }
    return [pscustomobject]@{ state = 'idle'; lines = @(); error = $null }
}

function Get-HostPressureProcesses {
    try {
        $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
            $_.Name -match '^python(?:w)?\.exe$'
        })
    }
    catch {
        return [pscustomobject]@{ state = 'unknown'; matches = @(); all_runs = @(); error = $_.Exception.Message }
    }

    $allRuns = @()
    foreach ($process in $processes) {
        $commandLine = [string]$process.CommandLine
        if ($commandLine -match '(?i)(?:cli\.py|app_press_test\.cli)\s+run(?:\s|$)') {
            $allRuns += [pscustomobject]@{
                pid = $process.ProcessId
                command = $commandLine
            }
        }
    }
    return [pscustomobject]@{ state = 'known'; matches = @(); all_runs = $allRuns; error = $null }
}

function Save-GuardState {
    param(
        [int]$IdleChecks,
        [string[]]$Serials,
        [string]$Decision,
        [int]$ListenerPid
    )

    $state = [ordered]@{
        updated_at = (Get-Date).ToUniversalTime().ToString('o')
        idle_checks = $IdleChecks
        last_serials = @($Serials)
        last_decision = $Decision
        last_listener_pid = $ListenerPid
    }
    $state | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $statePath -Encoding UTF8
}

function Load-IdleState {
    param([int]$ListenerPid)

    if (-not (Test-Path -LiteralPath $statePath)) {
        return 0
    }
    try {
        $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
        if ([int]$state.last_listener_pid -ne $ListenerPid) {
            return 0
        }
        return [Math]::Max(0, [int]$state.idle_checks)
    }
    catch {
        Write-GuardLog ("STATE reset unreadable state: {0}" -f $_.Exception.Message)
        return 0
    }
}

try {
    $secondaryListener = Get-Listener -Port $SecondaryPort
    if ($null -eq $secondaryListener) {
        Write-GuardLog ("ABSENT port={0}; no secondary ADB server to manage" -f $SecondaryPort)
        Save-GuardState -IdleChecks 0 -Serials @() -Decision 'absent' -ListenerPid 0
        exit 0
    }
    if (-not $secondaryListener.verified) {
        Write-GuardLog ("KEEP port={0} pid={1}; listener process could not be verified as adb.exe" -f $SecondaryPort, $secondaryListener.pid)
        Save-GuardState -IdleChecks 0 -Serials @() -Decision 'unverified_listener' -ListenerPid $secondaryListener.pid
        exit 0
    }

    $primaryListener = Get-Listener -Port $PrimaryPort
    if ($null -eq $primaryListener -or -not $primaryListener.verified) {
        $primaryPid = if ($null -eq $primaryListener) { '<none>' } else { [string]$primaryListener.pid }
        Write-GuardLog ("KEEP port={0} pid={1}; primary port={2} is not a verified adb listener (pid={3})" -f $SecondaryPort, $secondaryListener.pid, $PrimaryPort, $primaryPid)
        Save-GuardState -IdleChecks 0 -Serials @() -Decision 'primary_unavailable' -ListenerPid $secondaryListener.pid
        exit 0
    }

    # Only query a port after its listener has been found.  `adb devices` would
    # otherwise start a fresh second server and defeat the purpose of this guard.
    $deviceRows = @(Get-DeviceRows -Port $SecondaryPort)
    $onlineSerials = @($deviceRows | Where-Object { $_.status -eq 'device' } | ForEach-Object { [string]$_.serial })
    $knownSerials = @($deviceRows | ForEach-Object { [string]$_.serial })
    if (Test-Path -LiteralPath $statePath) {
        try {
            $oldState = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
            if ([int]$oldState.last_listener_pid -eq $secondaryListener.pid) {
                $knownSerials += @($oldState.last_serials | ForEach-Object { [string]$_ })
            }
        }
        catch {
            Write-GuardLog ("STATE previous state ignored: {0}" -f $_.Exception.Message)
        }
    }
    $knownSerials = @($knownSerials | Where-Object { $_ } | Select-Object -Unique)

    $remoteBusy = @()
    $remoteUnknown = @($deviceRows | Where-Object { $_.status -ne 'device' } | ForEach-Object {
        "{0}: adb status {1}" -f $_.serial, $_.status
    })
    foreach ($serial in $onlineSerials) {
        $probe = Test-RemotePressure -Serial $serial
        if ($probe.state -eq 'busy') {
            $remoteBusy += $serial
        } elseif ($probe.state -eq 'unknown') {
            $remoteUnknown += ("{0}: {1}" -f $serial, $probe.error)
        }
    }

    $hostProbe = Get-HostPressureProcesses
    if ($hostProbe.state -eq 'unknown') {
        Write-GuardLog ("KEEP port={0} pid={1}; host process query unknown: {2}" -f $SecondaryPort, $secondaryListener.pid, $hostProbe.error)
        Save-GuardState -IdleChecks 0 -Serials $knownSerials -Decision 'host_probe_unknown' -ListenerPid $secondaryListener.pid
        exit 0
    }

    $hostBusy = @()
    foreach ($run in $hostProbe.all_runs) {
        foreach ($serial in $knownSerials) {
            $serialPattern = '(?i)(?<![A-Za-z0-9_.:-])' + [regex]::Escape($serial) + '(?![A-Za-z0-9_.:-])'
            if ($run.command -match $serialPattern) {
                $hostBusy += ("{0}:pid={1}" -f $serial, $run.pid)
                break
            }
        }
    }

    if ($remoteUnknown.Count -gt 0) {
        Write-GuardLog ("KEEP port={0} pid={1}; remote probe unknown: {2}" -f $SecondaryPort, $secondaryListener.pid, ($remoteUnknown -join ' | '))
        Save-GuardState -IdleChecks 0 -Serials $knownSerials -Decision 'remote_probe_unknown' -ListenerPid $secondaryListener.pid
        exit 0
    }
    if ($remoteBusy.Count -gt 0 -or $hostBusy.Count -gt 0) {
        $details = @($remoteBusy + $hostBusy) -join ', '
        Write-GuardLog ("KEEP port={0} pid={1}; pressure detected: {2}" -f $SecondaryPort, $secondaryListener.pid, $details)
        Save-GuardState -IdleChecks 0 -Serials $knownSerials -Decision 'pressure_detected' -ListenerPid $secondaryListener.pid
        exit 0
    }

    $idleChecks = (Load-IdleState -ListenerPid $secondaryListener.pid) + 1
    if ($idleChecks -lt $RequiredIdleChecks) {
        Write-GuardLog ("DEFER port={0} pid={1}; no pressure devices={2}; idle_check={3}/{4}" -f $SecondaryPort, $secondaryListener.pid, (($deviceRows | ForEach-Object { "{0}:{1}" -f $_.serial, $_.status }) -join ','), $idleChecks, $RequiredIdleChecks)
        Save-GuardState -IdleChecks $idleChecks -Serials $knownSerials -Decision 'deferred_idle' -ListenerPid $secondaryListener.pid
        exit 0
    }

    if ($DryRun) {
        Write-GuardLog ("DRYRUN would kill-server port={0} pid={1}; idle_checks={2}" -f $SecondaryPort, $secondaryListener.pid, $idleChecks)
        Save-GuardState -IdleChecks $idleChecks -Serials $knownSerials -Decision 'dry_run_would_kill' -ListenerPid $secondaryListener.pid
        exit 0
    }

    Write-GuardLog ("KILL start port={0} pid={1}; no pressure for {2} checks" -f $SecondaryPort, $secondaryListener.pid, $idleChecks)
    & $AdbPath -H 127.0.0.1 -P $SecondaryPort kill-server 2>&1 | ForEach-Object {
        if ([string]$_) { Write-GuardLog ("KILL adb: {0}" -f [string]$_) }
    }
    $killExit = $LASTEXITCODE
    Start-Sleep -Milliseconds 500
    $remaining = Get-Listener -Port $SecondaryPort
    if ($null -eq $remaining) {
        Write-GuardLog ("KILL complete port={0} exit={1}; listener stopped" -f $SecondaryPort, $killExit)
    } else {
        Write-GuardLog ("KILL incomplete port={0} exit={1}; listener_pid={2}" -f $SecondaryPort, $killExit, $remaining.pid)
    }
    Save-GuardState -IdleChecks 0 -Serials @() -Decision 'kill_attempted' -ListenerPid $secondaryListener.pid
    exit 0
}
catch {
    Write-GuardLog ("ERROR fail-safe keep: {0}" -f $_.Exception.Message)
    exit 1
}
finally {
    if ($null -ne $lockStream) {
        $lockStream.Dispose()
    }
}
