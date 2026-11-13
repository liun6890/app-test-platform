[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$taskName = 'AppPressTest-Adb5038Guard'
$powershell = 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
$guardScript = Join-Path $PSScriptRoot 'guard_adb_server.ps1'
$taskCommand = ('"{0}" -NoProfile -ExecutionPolicy Bypass -File "{1}"' -f $powershell, $guardScript)
$logRoot = 'E:\app_press_test\logs\adb_guard'
$installLog = Join-Path $logRoot 'task_install.log'

[System.IO.Directory]::CreateDirectory($logRoot) | Out-Null

function Write-InstallLog {
    param([string]$Message)
    Add-Content -LiteralPath $installLog -Value ("{0} {1}" -f (Get-Date -Format o), $Message) -Encoding UTF8
}

try {
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [System.Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Administrator elevation is required to register the scheduled task'
    }

    $createOutput = @(& schtasks.exe /Create /SC MINUTE /MO 1 /TN $taskName /TR $taskCommand /IT /RL LIMITED /F 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw ("schtasks create failed: {0}" -f (($createOutput | ForEach-Object { [string]$_ }) -join ' | '))
    }

    $queryOutput = @(& schtasks.exe /Query /TN $taskName /FO LIST /V 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw ("schtasks query failed: {0}" -f (($queryOutput | ForEach-Object { [string]$_ }) -join ' | '))
    }

    $runOutput = @(& schtasks.exe /Run /TN $taskName 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw ("schtasks run failed: {0}" -f (($runOutput | ForEach-Object { [string]$_ }) -join ' | '))
    }

    Write-InstallLog ("INSTALLED task={0} user={1} command={2}" -f $taskName, $identity.Name, $taskCommand)
    foreach ($line in $queryOutput) {
        if ([string]$line) {
            Write-InstallLog ("QUERY {0}" -f [string]$line)
        }
    }
    exit 0
}
catch {
    Write-InstallLog ("ERROR {0}" -f $_.Exception.Message)
    exit 1
}
