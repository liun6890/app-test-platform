<#
.SYNOPSIS
    开关安卓设备的触摸可视化（指针位置 + 点按圆点）。

.DESCRIPTION
    通过 adb 修改两个系统设置：
        pointer_location  顶部显示坐标条（P / dX / dY / Xv / Yv / Prs / Size）
        show_touches      点击时显示触摸圆点

    演示、录屏、复现时打开可以直观看到落点；长稳压测时建议关闭，
    因为这两个浮层是额外的系统窗口，会被 Fastbot 的输入守卫计入窗口数量，
    偶尔导致注入被判定为「外部窗口」而拦截。

.PARAMETER Mode
    on     打开
    off    关闭
    status 只查询当前状态（默认）

.PARAMETER Devices
    指定设备序列号；不传则对 `adb devices` 里所有 device 状态的设备执行。

.EXAMPLE
    .\touch-overlay.ps1 on
    .\touch-overlay.ps1 status
    .\touch-overlay.ps1 off -Devices a93adef2,AMFU6R1B10000108
#>
[CmdletBinding()]
param(
    [ValidateSet('on', 'off', 'status')]
    [string]$Mode = 'status',

    [string[]]$Devices
)

$ErrorActionPreference = 'Stop'
$adb = if ($env:ADB_PATH) { $env:ADB_PATH } else { 'adb' }

function Get-TargetSerials {
    if ($Devices -and $Devices.Count -gt 0) {
        return @($Devices | Where-Object { $_ -and $_.Trim() } | ForEach-Object { $_.Trim() })
    }
    $lines = & $adb devices 2>$null
    return @(
        $lines |
            Where-Object { $_ -match '^\S+\s+device$' } |
            ForEach-Object { ($_ -split '\s+')[0] }
    )
}

function Get-Setting($serial, $key) {
    $value = (& $adb -s $serial shell settings get system $key 2>$null) -join ''
    return $value.Trim()
}

$serials = Get-TargetSerials
if ($serials.Count -eq 0) {
    Write-Error '没有找到处于 device 状态的设备。'
    exit 1
}

$value = switch ($Mode) {
    'on'  { '1' }
    'off' { '0' }
    default { $null }
}

foreach ($serial in $serials) {
    if ($value -ne $null) {
        foreach ($key in @('pointer_location', 'show_touches')) {
            & $adb -s $serial shell settings put system $key $value 2>$null | Out-Null
        }
    }
    $pointer = Get-Setting $serial 'pointer_location'
    $touches = Get-Setting $serial 'show_touches'
    $state = if ($pointer -eq '1' -or $touches -eq '1') { '开启' } else { '关闭' }
    '{0,-22} pointer_location={1,-3} show_touches={2,-3} => {3}' -f $serial, $pointer, $touches, $state
}

Write-Host ''
switch ($Mode) {
    'on'  { Write-Host '已开启触摸可视化。压测前建议执行 off，避免浮层干扰注入判定。' }
    'off' { Write-Host '已关闭触摸可视化。' }
    default { Write-Host '查询完成。使用 -Mode on / -Mode off 切换。' }
}
