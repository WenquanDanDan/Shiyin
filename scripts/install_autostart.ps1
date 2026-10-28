#Requires -Version 5.1
<#
.SYNOPSIS
    为 vtw 注册开机自启。

.DESCRIPTION
    实际逻辑在 src\vtw\autostart.py（用 winreg 操作
    HKCU\Software\Microsoft\Windows\CurrentVersion\Run）。
    本脚本只是薄封装，保证命令行与图形设置界面用的是同一套实现，避免两处漂移。

    只影响当前用户，**不需要管理员权限**；启动用 pythonw.exe，登录后无控制台窗口。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1
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

# 不依赖 vtw.pth 是否已生成，显式指定源码目录
$env:PYTHONPATH = Join-Path $projectRoot 'src'
& $venvPython -m vtw.autostart enable
exit $LASTEXITCODE
