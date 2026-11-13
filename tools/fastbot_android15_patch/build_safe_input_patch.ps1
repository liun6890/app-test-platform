param(
    [string]$JavaHome = 'C:\Program Files\Java\jdk1.8.0_261',
    [string]$AndroidSdk = 'D:\Android\android-sdk'
)

$ErrorActionPreference = 'Stop'
$patchRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$classesInput = Join-Path $patchRoot 'monkey-android15-compat-classes.jar'
$apeSource = Join-Path $patchRoot 'MonkeySourceApeNative.java'
$permissionGuardSource = Join-Path $patchRoot 'PermissionInputGuard.java'
$gson = Join-Path $patchRoot 'gson-2.8.9.jar'
$framework = 'D:\Users\Administrator\Downloads\Fastbot_Android-main\Fastbot_Android-main\monkey\libs\framework.jar'
$androidJar = Join-Path $AndroidSdk 'platforms\android-R\android.jar'
$javac = Join-Path $JavaHome 'bin\javac.exe'
$jar = Join-Path $JavaHome 'bin\jar.exe'
$d8 = Join-Path $AndroidSdk 'build-tools\29.0.3\d8.bat'
$classesDir = Join-Path $patchRoot 'classes_safe_input'
$dexDir = Join-Path $patchRoot 'dex_safe_input'
$classesOutput = Join-Path $patchRoot 'monkey-android15-safe-input-classes.jar'
$dexOutput = Join-Path $patchRoot 'monkeyq-android15-safe-input.jar'

$required = @($classesInput, $apeSource, $permissionGuardSource, $gson, $framework, $androidJar, $javac, $jar, $d8)
foreach ($path in $required) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required build input is missing: $path"
    }
}

$rootPrefix = [IO.Path]::GetFullPath($patchRoot).TrimEnd('\') + '\'
foreach ($workingDir in @($classesDir, $dexDir)) {
    $resolvedWorkingDir = [IO.Path]::GetFullPath($workingDir)
    if (-not $resolvedWorkingDir.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean a build directory outside the patch root: $resolvedWorkingDir"
    }
    New-Item -ItemType Directory -Force -Path $resolvedWorkingDir | Out-Null
    Get-ChildItem -LiteralPath $resolvedWorkingDir -Force | Remove-Item -Recurse -Force
}

$classpath = "$framework;$androidJar;$classesInput;$gson"
$sources = @(
    (Join-Path $patchRoot 'MonkeyMotionEvent.java'),
    (Join-Path $patchRoot 'MonkeyKeyEvent.java'),
    $permissionGuardSource,
    (Join-Path $patchRoot 'AppInputGuard.java'),
    (Join-Path $patchRoot 'AppTextEvent.java'),
    $apeSource
)
& $javac -encoding UTF-8 -source 1.8 -target 1.8 -classpath $classpath `
    -d $classesDir `
    $sources
if ($LASTEXITCODE -ne 0) {
    throw "javac failed with exit code $LASTEXITCODE"
}

Copy-Item -LiteralPath $classesInput -Destination $classesOutput -Force
Push-Location $classesDir
try {
    $compiledClasses = @(
        'com/android/commands/monkey/events/base/MonkeyMotionEvent.class',
        'com/android/commands/monkey/events/base/MonkeyKeyEvent.class',
        'com/android/commands/monkey/events/base/PermissionInputGuard.class',
        'com/android/commands/monkey/events/base/AppInputGuard.class',
        'com/android/commands/monkey/events/base/AppTextEvent.class',
        'com/android/commands/monkey/source/MonkeySourceApeNative.class',
        'com/android/commands/monkey/source/MonkeySourceApeNative$1.class'
    )
    & $jar uf $classesOutput $compiledClasses
    if ($LASTEXITCODE -ne 0) {
        throw "jar update failed with exit code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}

# Keep the extracted class snapshots in sync with the reproducible patch
# output. They are useful for bytecode inspection and offline verification.
foreach ($snapshotName in @('classes_v2', 'classes_full')) {
    $snapshotRoot = Join-Path $patchRoot $snapshotName
    foreach ($compiledClass in $compiledClasses) {
        $sourceClass = Join-Path $classesDir ($compiledClass -replace '/', '\')
        $snapshotClass = Join-Path $snapshotRoot ($compiledClass -replace '/', '\')
        $snapshotParent = Split-Path -Parent $snapshotClass
        New-Item -ItemType Directory -Force -Path $snapshotParent | Out-Null
        Copy-Item -LiteralPath $sourceClass -Destination $snapshotClass -Force
    }
}

& $d8 --min-api 22 --output $dexDir $classesOutput
if ($LASTEXITCODE -ne 0) {
    throw "d8 failed with exit code $LASTEXITCODE"
}

if (Test-Path -LiteralPath $dexOutput) {
    Remove-Item -LiteralPath $dexOutput -Force
}
Push-Location $dexDir
try {
    & $jar cf $dexOutput 'classes.dex'
    if ($LASTEXITCODE -ne 0) {
        throw "dex jar creation failed with exit code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}

Get-FileHash -LiteralPath $dexOutput -Algorithm MD5
