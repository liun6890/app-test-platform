[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$BackendPort = 8765,

    [ValidateRange(1, 65535)]
    [int]$FrontendPort = 5173,

    [ValidateRange(5, 120)]
    [int]$TimeoutSeconds = 30,

    [switch]$NoBrowser,

    [switch]$CheckOnly
)

# Start the local API and Vite development server as two independent
# processes.  The script deliberately does not install dependencies: a
# launcher should be predictable and must not change the environment behind
# the user's back.

$ErrorActionPreference = "Stop"

# Some IDE/agent shells expose both `Path` and `PATH`.  Windows treats those
# names as the same environment variable, while PowerShell's Start-Process
# enumerator treats them as duplicate dictionary keys.  Collapse them before
# creating child processes; both values are normally identical.
$processEnvironment = [Environment]::GetEnvironmentVariables([EnvironmentVariableTarget]::Process)
$pathKeys = @($processEnvironment.Keys | Where-Object { [string]$_ -ieq "PATH" })
if ($pathKeys.Count -gt 1) {
    $pathValue = [string]$processEnvironment[$pathKeys[0]]
    foreach ($pathKey in $pathKeys) {
        [Environment]::SetEnvironmentVariable([string]$pathKey, $null, [EnvironmentVariableTarget]::Process)
    }
    [Environment]::SetEnvironmentVariable("PATH", $pathValue, [EnvironmentVariableTarget]::Process)
}

$ProjectRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$FrontendRoot = Join-Path $ProjectRoot "frontend"
$LauncherRoot = "E:\app_press_test\frontend\runtime-local\launcher"
$StatePath = Join-Path $LauncherRoot "services.json"

function Fail([string]$Message) {
    throw $Message
}

function Test-TcpPort {
    param(
        [string]$HostName,
        [int]$Port,
        [int]$TimeoutMilliseconds = 500
    )

    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $asyncResult = $client.BeginConnect($HostName, $Port, $null, $null)
        if (-not $asyncResult.AsyncWaitHandle.WaitOne($TimeoutMilliseconds)) {
            return $false
        }
        $client.EndConnect($asyncResult)
        return $true
    }
    catch {
        return $false
    }
    finally {
        $client.Close()
    }
}

function Get-HttpStatus {
    param(
        [string]$Uri,
        [int]$TimeoutMilliseconds = 1000
    )

    $request = $null
    $response = $null
    try {
        $request = [System.Net.HttpWebRequest]::Create($Uri)
        $request.Method = "GET"
        $request.Timeout = $TimeoutMilliseconds
        $request.ReadWriteTimeout = $TimeoutMilliseconds
        $response = $request.GetResponse()
        return [int]$response.StatusCode
    }
    catch [System.Net.WebException] {
        if ($null -ne $_.Exception.Response) {
            return [int]$_.Exception.Response.StatusCode
        }
        return 0
    }
    catch {
        return 0
    }
    finally {
        if ($null -ne $response) {
            $response.Close()
        }
    }
}

function Wait-HttpOk {
    param(
        [string]$Uri,
        [int]$Timeout = 30
    )

    $deadline = (Get-Date).AddSeconds($Timeout)
    do {
        $status = Get-HttpStatus -Uri $Uri
        if ($status -ge 200 -and $status -lt 300) {
            return $true
        }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)
    return $false
}

function Resolve-CommandPath {
    param([string[]]$Names)

    foreach ($name in $Names) {
        $command = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -ne $command) {
            return $command.Source
        }
    }
    return $null
}

function Stop-ProcessTree {
    param([int]$ProcessId)

    if ($ProcessId -le 0) {
        return
    }

    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return
    }

    # npm.cmd can leave a node child behind. taskkill's /T option limits the
    # cleanup to this launcher's process tree instead of scanning by port.
    try {
        & taskkill.exe /PID $ProcessId /T /F 2>$null | Out-Null
    }
    catch {
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Get-ProcessStartMarker {
    param([System.Diagnostics.Process]$Process)

    if ($null -eq $Process) {
        return $null
    }
    try {
        return $Process.StartTime.ToUniversalTime().ToString("o")
    }
    catch {
        return $null
    }
}

function Get-PreservedServiceState {
    param(
        [object]$LauncherState,
        [string]$Name
    )

    if ($null -eq $LauncherState) {
        return $null
    }
    $service = $LauncherState.$Name
    if (
        $null -eq $service -or
        -not [bool]$service.owned -or
        $null -eq $service.pid -or
        [string]::IsNullOrWhiteSpace([string]$service.processStartedAt)
    ) {
        return $null
    }
    $process = Get-Process -Id ([int]$service.pid) -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return $null
    }
    if ((Get-ProcessStartMarker -Process $process) -ne [string]$service.processStartedAt) {
        return $null
    }
    return $service
}

function Start-ManagedProcess {
    param(
        [string]$Name,
        [string]$FilePath,
        [string[]]$ArgumentList,
        [string]$WorkingDirectory,
        [string]$StandardOutput,
        [string]$StandardError
    )

    Write-Host ("Starting {0} service..." -f $Name)
    try {
        return Start-Process `
            -FilePath $FilePath `
            -ArgumentList $ArgumentList `
            -WorkingDirectory $WorkingDirectory `
            -WindowStyle Hidden `
            -RedirectStandardOutput $StandardOutput `
            -RedirectStandardError $StandardError `
            -PassThru
    }
    catch {
        throw ("Unable to start the {0} service: {1}" -f $Name, $_.Exception.Message)
    }
}

if (-not (Test-Path -LiteralPath $FrontendRoot -PathType Container)) {
    Fail ("Frontend directory not found: {0}" -f $FrontendRoot)
}

$backendUrl = "http://127.0.0.1:{0}/api/health" -f $BackendPort
$frontendUrl = "http://127.0.0.1:{0}/" -f $FrontendPort
$backendStatus = Get-HttpStatus -Uri $backendUrl
$frontendStatus = Get-HttpStatus -Uri $frontendUrl
$backendReady = $backendStatus -ge 200 -and $backendStatus -lt 300
$frontendReady = $frontendStatus -ge 200 -and $frontendStatus -lt 300

# This is the critical no-interference path. When both services are healthy,
# do not touch ADB, launcher state, processes, or the active test workers.
if (-not $CheckOnly -and $backendReady -and $frontendReady) {
    Write-Host "Services are already ready; no process was changed." -ForegroundColor Green
    Write-Host ("  Frontend: {0}" -f $frontendUrl)
    Write-Host ("  Backend:  {0}" -f $backendUrl)
    if (-not $NoBrowser) {
        Start-Process $frontendUrl | Out-Null
    }
    exit 0
}

$pythonPath = $null
$adbPath = $null
if ($CheckOnly -or -not $backendReady) {
    $pythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
        $pythonPath = Resolve-CommandPath @("python.exe", "python")
        if ([string]::IsNullOrWhiteSpace($pythonPath)) {
            Fail "Python was not found. Create .venv or add Python to PATH."
        }
        Write-Warning ("Project virtual environment not found; using Python from PATH: {0}" -f $pythonPath)
    }

    # Endpoint security software can block file writes for one interpreter path
    # while allowing another (this host denies the project venv python outside
    # the workspace, so the service started and then died on its first report
    # write). Probe the output root and fall back to a writable interpreter.
    $probeRoot = $env:APP_PRESS_TEST_OUTPUT_ROOT
    if ([string]::IsNullOrWhiteSpace($probeRoot)) {
        $probeRoot = "E:\app_press_test"
    }
    if (Test-Path -LiteralPath $probeRoot -PathType Container) {
        $probeFile = Join-Path $probeRoot (".write-probe-{0}.tmp" -f [guid]::NewGuid().ToString("N"))
        $canWrite = $false
        try {
            & $pythonPath -c "open(r'$probeFile','w').write('1')" 2>$null
            $canWrite = ($LASTEXITCODE -eq 0) -and (Test-Path -LiteralPath $probeFile)
        } catch {
            $canWrite = $false
        }
        if (Test-Path -LiteralPath $probeFile) {
            Remove-Item -LiteralPath $probeFile -Force -ErrorAction SilentlyContinue
        }
        if (-not $canWrite) {
            $fallbackPython = Resolve-CommandPath @("python.exe", "python")
            if ([string]::IsNullOrWhiteSpace($fallbackPython) -or $fallbackPython -eq $pythonPath) {
                Fail ("{0} cannot write to {1} and no alternative Python is available." -f $pythonPath, $probeRoot)
            }
            & $fallbackPython -c "import uiautomator2" 2>$null
            if ($LASTEXITCODE -ne 0) {
                Fail ("{0} cannot write to {1}, and {2} is missing uiautomator2.`nFix the interpreter permissions or run: {2} -m pip install -r requirements.txt" -f $pythonPath, $probeRoot, $fallbackPython)
            }
            Write-Warning ("{0} cannot write to {1}; using {2} for the API and its workers." -f $pythonPath, $probeRoot, $fallbackPython)
            $pythonPath = $fallbackPython
        }
    }

    $adbPath = Resolve-CommandPath @("adb.exe", "adb")
    if ([string]::IsNullOrWhiteSpace($adbPath)) {
        Fail "ADB was not found. Install Android Platform Tools and add adb to PATH."
    }

    # Keep the API and its child commands on one known ADB daemon. Redirect
    # the daemon log away from stale, machine-specific environment paths.
    $env:ADB_PATH = $adbPath
    $env:ANDROID_ADB_SERVER_PORT = "5037"
    $env:ANDROID_ADB_LOG_PATH = Join-Path $LauncherRoot "adb-trace.log"
    # Keep the API and every CLI worker it spawns on the same interpreter that
    # passed the write probe above.
    $env:APP_PRESS_TEST_RUNTIME_PYTHON = $pythonPath

    & $pythonPath -c "import uiautomator2" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Fail ("uiautomator2 is not installed in: {0}`nRun: {0} -m pip install -r requirements.txt" -f $pythonPath)
    }
}

$nodePath = $null
$viteCli = $null
if ($CheckOnly -or -not $frontendReady) {
    $nodePath = Resolve-CommandPath @("node.exe", "node")
    if ([string]::IsNullOrWhiteSpace($nodePath)) {
        Fail "Node.js was not found. Install Node.js and add node to PATH."
    }

    $nodeModules = Join-Path $FrontendRoot "node_modules"
    if (-not (Test-Path -LiteralPath $nodeModules -PathType Container)) {
        Fail ("Frontend dependencies are not installed: {0}`nRun: cd frontend; npm install" -f $nodeModules)
    }
    $viteCli = Join-Path $nodeModules "vite\bin\vite.js"
    if (-not (Test-Path -LiteralPath $viteCli -PathType Leaf)) {
        Fail ("Vite CLI was not found: {0}`nRun: cd frontend; npm install" -f $viteCli)
    }
}

if ($CheckOnly) {
    [pscustomobject]@{
        ProjectRoot = $ProjectRoot
        Python = $pythonPath
        Node = $nodePath
        Adb = $adbPath
        BackendUrl = $backendUrl
        BackendReady = $backendReady
        FrontendUrl = $frontendUrl
        FrontendReady = $frontendReady
    } | Format-List
    exit 0
}

New-Item -ItemType Directory -Path $LauncherRoot -Force | Out-Null

if (-not $backendReady) {
    # adb writes normal daemon startup information to stderr. PowerShell treats
    # native stderr as an error record under the launcher's Stop policy, so
    # capture it while preserving the actual process exit code.
    $launchErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $adbStartupOutput = @(& $adbPath start-server 2>&1)
        $adbStartupExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $launchErrorAction
    }
    if ($adbStartupExitCode -ne 0) {
        Fail ("ADB server failed to start: {0}" -f (($adbStartupOutput | ForEach-Object { [string]$_ }) -join " "))
    }
}

if (-not $backendReady -and (Test-TcpPort -HostName "127.0.0.1" -Port $BackendPort)) {
    Fail ("Port {0} is in use, but it is not the Fastbot API: {1}" -f $BackendPort, $backendUrl)
}
if (-not $frontendReady -and (Test-TcpPort -HostName "127.0.0.1" -Port $FrontendPort)) {
    Fail ("Port {0} is in use, but it is not the Vite frontend: {1}" -f $FrontendPort, $frontendUrl)
}

$existingState = $null
if (Test-Path -LiteralPath $StatePath -PathType Leaf) {
    try {
        $existingState = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
    }
    catch {
        Write-Warning ("Ignoring unreadable launcher state: {0}" -f $StatePath)
    }
}
$preservedBackend = Get-PreservedServiceState -LauncherState $existingState -Name "backend"
$preservedFrontend = Get-PreservedServiceState -LauncherState $existingState -Name "frontend"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backendStdout = if ($null -ne $preservedBackend) { [string]$preservedBackend.stdout } else { Join-Path $LauncherRoot ("backend-{0}.out.log" -f $stamp) }
$backendStderr = if ($null -ne $preservedBackend) { [string]$preservedBackend.stderr } else { Join-Path $LauncherRoot ("backend-{0}.err.log" -f $stamp) }
$frontendStdout = if ($null -ne $preservedFrontend) { [string]$preservedFrontend.stdout } else { Join-Path $LauncherRoot ("frontend-{0}.out.log" -f $stamp) }
$frontendStderr = if ($null -ne $preservedFrontend) { [string]$preservedFrontend.stderr } else { Join-Path $LauncherRoot ("frontend-{0}.err.log" -f $stamp) }
$startedProcesses = New-Object System.Collections.Generic.List[object]
$backendProcess = $null
$frontendProcess = $null

try {
    if (-not $backendReady) {
        $backendProcess = Start-ManagedProcess `
            -Name "backend" `
            -FilePath $pythonPath `
            -ArgumentList @("server.py", "--host", "127.0.0.1", "--port", [string]$BackendPort) `
            -WorkingDirectory $FrontendRoot `
            -StandardOutput $backendStdout `
            -StandardError $backendStderr
        $startedProcesses.Add($backendProcess)

        if (-not (Wait-HttpOk -Uri $backendUrl -Timeout $TimeoutSeconds)) {
            Fail ("Backend did not become ready within {0} seconds. See: {1}" -f $TimeoutSeconds, $backendStderr)
        }
        $backendReady = $true
    }
    else {
        Write-Host ("Backend is already running: {0}" -f $backendUrl)
    }

    if (-not $frontendReady) {
        $frontendProcess = Start-ManagedProcess `
            -Name "frontend" `
            -FilePath $nodePath `
            -ArgumentList @($viteCli, "--configLoader", "runner", "--host", "127.0.0.1", "--port", [string]$FrontendPort) `
            -WorkingDirectory $FrontendRoot `
            -StandardOutput $frontendStdout `
            -StandardError $frontendStderr
        $startedProcesses.Add($frontendProcess)

        if (-not (Wait-HttpOk -Uri $frontendUrl -Timeout $TimeoutSeconds)) {
            Fail ("Frontend did not become ready within {0} seconds. See: {1}" -f $TimeoutSeconds, $frontendStderr)
        }
        $frontendReady = $true
    }
    else {
        Write-Host ("Frontend is already running: {0}" -f $frontendUrl)
    }

    $backendOwned = $null -ne $backendProcess -or ($backendReady -and $null -ne $preservedBackend)
    $frontendOwned = $null -ne $frontendProcess -or ($frontendReady -and $null -ne $preservedFrontend)
    $backendPid = if ($null -ne $backendProcess) { $backendProcess.Id } elseif ($backendOwned) { [int]$preservedBackend.pid } else { $null }
    $frontendPid = if ($null -ne $frontendProcess) { $frontendProcess.Id } elseif ($frontendOwned) { [int]$preservedFrontend.pid } else { $null }
    $backendStartedAt = if ($null -ne $backendProcess) { Get-ProcessStartMarker -Process $backendProcess } elseif ($backendOwned) { [string]$preservedBackend.processStartedAt } else { $null }
    $frontendStartedAt = if ($null -ne $frontendProcess) { Get-ProcessStartMarker -Process $frontendProcess } elseif ($frontendOwned) { [string]$preservedFrontend.processStartedAt } else { $null }

    $state = [ordered]@{
        startedAt = (Get-Date).ToString("o")
        projectRoot = $ProjectRoot
        backend = [ordered]@{
            url = $backendUrl
            port = $BackendPort
            pid = $backendPid
            processStartedAt = $backendStartedAt
            owned = $backendOwned
            stdout = $backendStdout
            stderr = $backendStderr
        }
        frontend = [ordered]@{
            url = $frontendUrl
            port = $FrontendPort
            pid = $frontendPid
            processStartedAt = $frontendStartedAt
            owned = $frontendOwned
            stdout = $frontendStdout
            stderr = $frontendStderr
        }
    }
    $state | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $StatePath -Encoding UTF8

    Write-Host ""
    Write-Host "Services are ready:" -ForegroundColor Green
    Write-Host ("  Frontend: {0}" -f $frontendUrl)
    Write-Host ("  Backend:  {0}" -f $backendUrl)
    Write-Host ("Logs: {0}" -f $LauncherRoot)
    Write-Host "Stop services started here: double-click stop-all.cmd or run .\stop-all.ps1"

    if (-not $NoBrowser) {
        Start-Process $frontendUrl | Out-Null
    }
}
catch {
    foreach ($process in $startedProcesses) {
        if ($null -ne $process) {
            Stop-ProcessTree -ProcessId $process.Id
        }
    }
    throw
}
