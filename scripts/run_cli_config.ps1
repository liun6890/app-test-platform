param(
    [Parameter(Mandatory = $true)]
    [string]$Config,

    [Parameter(Mandatory = $true)]
    [string]$Devices,

    [ValidateRange(1, 65535)]
    [int]$AdbServerPort = 5037
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:ADB_PATH = 'D:\Android\android-sdk\platform-tools\adb.exe'
$env:ANDROID_ADB_SERVER_PORT = [string]$AdbServerPort
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$cli = Join-Path $projectRoot 'cli.py'

& $python $cli run --config $Config --devices $Devices
exit $LASTEXITCODE
