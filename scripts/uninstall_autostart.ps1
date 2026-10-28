#Requires -Version 5.1
<#
.SYNOPSIS
    移除 vtw 的开机自启项。

.DESCRIPTION
    实际逻辑在 src\vtw\autostart.py，本脚本只是薄封装。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\uninstall_autostart.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $venvPython)) {
    Write-Error "找不到 $venvPython`n请先运行 scripts\setup.ps1 创建虚拟环境。"
    exit 1
}

$env:PYTHONPATH = Join-Path $projectRoot 'src'
& $venvPython -m vtw.autostart disable
$code = $LASTEXITCODE

if ($code -eq 0) {
    Write-Host "注意：如果 vtw 正在运行，托盘图标不会自动消失，请右键托盘图标选择「退出」。"
}
exit $code
