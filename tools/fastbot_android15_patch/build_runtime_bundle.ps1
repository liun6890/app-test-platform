param(
    [string]$JavaHome = 'C:\Program Files\Java\jdk1.8.0_261',
    [string]$AndroidSdk = 'D:\Android\android-sdk',
    [int]$MinApi = 22
)

$ErrorActionPreference = 'Stop'
$patchRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$d8 = Join-Path $AndroidSdk 'build-tools\29.0.3\d8.bat'
$jar = Join-Path $JavaHome 'bin\jar.exe'
$zipalign = Join-Path $AndroidSdk 'build-tools\29.0.3\zipalign.exe'
$monkey = Join-Path $patchRoot 'monkeyq-android15-safe-input.jar'
$framework = Join-Path $patchRoot 'framework.jar'
$thirdpart = Join-Path $patchRoot 'fastbot-thirdpart.jar'
$output = Join-Path $patchRoot 'fastbot-runtime.apk'
$buildRoot = Join-Path $patchRoot '.runtime-build'
$frameworkDex = Join-Path $buildRoot 'framework.dex'
$dexRoot = Join-Path $buildRoot 'dex'
$stagingRoot = Join-Path $buildRoot 'staging'

foreach ($path in @($d8, $jar, $zipalign, $monkey, $framework, $thirdpart)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required Fastbot bundle input is missing: $path"
    }
}

$rootPrefix = [IO.Path]::GetFullPath($patchRoot).TrimEnd('\') + '\'
$resolvedBuildRoot = [IO.Path]::GetFullPath($buildRoot)
if (-not $resolvedBuildRoot.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to use a build directory outside the Fastbot patch root: $resolvedBuildRoot"
}
New-Item -ItemType Directory -Force -Path $buildRoot | Out-Null
Copy-Item -LiteralPath $framework -Destination $frameworkDex -Force

New-Item -ItemType Directory -Force -Path $dexRoot, $stagingRoot | Out-Null
& $d8 --release --min-api $MinApi --output $dexRoot $monkey $frameworkDex $thirdpart
if ($LASTEXITCODE -ne 0) {
    throw "d8 failed with exit code $LASTEXITCODE"
}

Copy-Item -LiteralPath (Join-Path $dexRoot 'classes.dex') -Destination $stagingRoot -Force
$nativeRoot = Join-Path $patchRoot 'native'
if (Test-Path -LiteralPath $nativeRoot -PathType Container) {
    Get-ChildItem -LiteralPath $nativeRoot -Directory | ForEach-Object {
        $nativeSource = Join-Path $_.FullName 'libfastbot_native.so'
        if (Test-Path -LiteralPath $nativeSource -PathType Leaf) {
            $nativeDestination = Join-Path (Join-Path $stagingRoot 'lib') $_.Name
            New-Item -ItemType Directory -Force -Path $nativeDestination | Out-Null
            Copy-Item -LiteralPath $nativeSource -Destination $nativeDestination -Force
        }
    }
}

if (-not (Test-Path -LiteralPath (Join-Path $stagingRoot 'lib') -PathType Container)) {
    throw "No Fastbot native library was found under $nativeRoot"
}

$rawOutput = Join-Path $buildRoot 'fastbot-runtime-raw.apk'
if (Test-Path -LiteralPath $rawOutput -PathType Leaf) {
    Remove-Item -LiteralPath $rawOutput -Force
}
Push-Location $stagingRoot
try {
    & $jar cf0 $rawOutput 'classes.dex' 'lib'
    if ($LASTEXITCODE -ne 0) {
        throw "jar failed with exit code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}

if (Test-Path -LiteralPath $output -PathType Leaf) {
    Remove-Item -LiteralPath $output -Force
}
& $zipalign -p -f 4 $rawOutput $output
if ($LASTEXITCODE -ne 0) {
    throw "zipalign failed with exit code $LASTEXITCODE"
}

Remove-Item -LiteralPath $buildRoot -Recurse -Force
Get-FileHash -LiteralPath $output -Algorithm MD5
