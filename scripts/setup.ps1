#Requires -Version 5.1
<#
.SYNOPSIS
    vtw 一键部署：创建虚拟环境 → 安装依赖 → 下载模型 →（可选）注册开机自启。

.DESCRIPTION
    幂等，可重复运行。已经完成的步骤会自动跳过。

    依赖安装使用当前环境的 pip 配置（默认 PyPI）。如果你的网络访问 PyPI 较慢，
    可以先设置镜像，例如：
      $env:PIP_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple"
    本脚本不会修改全局 pip 配置。

.PARAMETER SkipAutostart
    不注册开机自启。

.PARAMETER ForceModels
    强制重新下载模型。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\setup.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -SkipAutostart
#>
[CmdletBinding()]
param(
    [switch]$SkipAutostart,
    [switch]$ForceModels
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host ("=" * 64)
    Write-Host "  $Text"
    Write-Host ("=" * 64)
}

Write-Host "vtw 部署脚本"
Write-Host "项目目录：$projectRoot"

# ---------------------------------------------------------------- 1. 虚拟环境
Write-Step "步骤 1/5：Python 虚拟环境"
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (Test-Path $venvPython) {
    Write-Host "[跳过] 已存在：$venvPython"
} else {
    $base = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $base) {
        Write-Error "找不到 python。请先安装 Python 3.14+ 并加入 PATH。"
        exit 1
    }
    $baseVersion = & $base -c "import sys; print('.'.join(map(str, sys.version_info[:2])))"
    if ([version]$baseVersion -lt [version]'3.14') {
        Write-Error "需要 Python 3.14+，当前为 $baseVersion。"
        exit 1
    }
    Write-Host "使用基础解释器：$base（Python $baseVersion）"
    & $base -m venv (Join-Path $projectRoot '.venv')
    if ($LASTEXITCODE -ne 0) { Write-Error "创建虚拟环境失败"; exit 1 }
    Write-Host "[完成] 虚拟环境已创建"
}

$version = & $venvPython -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
Write-Host "虚拟环境 Python 版本：$version"
$venvMajorMinor = [version](($version -split '\.')[0..1] -join '.')
if ($venvMajorMinor -lt [version]'3.14') {
    Write-Error "虚拟环境 Python 版本过低（$version），请删除 .venv 后用 Python 3.14+ 重建。"
    exit 1
}

# 让 `python -m vtw` 在任何工作目录下都能用。
# 等价于 editable 安装的效果，但不需要额外的构建后端与联网。
$pthFile = Join-Path $projectRoot '.venv\Lib\site-packages\vtw.pth'
$srcPath = Join-Path $projectRoot 'src'
if (-not (Test-Path $pthFile)) {
    # .pth 必须是纯文本且不要 BOM，site.py 才能正确解析
    [System.IO.File]::WriteAllText($pthFile, $srcPath + [Environment]::NewLine)
    Write-Host "[完成] 已写入 vtw.pth（使 python -m vtw 可用）"
} else {
    Write-Host "[跳过] vtw.pth 已存在"
}

# ---------------------------------------------------------------- 2. 依赖
Write-Step "步骤 2/5：安装依赖"
Write-Host "安装源：当前 pip 配置（可用 PIP_INDEX_URL 指定镜像）"
& $venvPython -m pip install --upgrade pip --quiet
& $venvPython -m pip install -r (Join-Path $projectRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) {
    Write-Error "依赖安装失败。网络较慢时可设置 PIP_INDEX_URL 镜像后重试。"
    exit 1
}
Write-Host "[完成] 依赖已安装"

# ---------------------------------------------------------------- 3. 模型
Write-Step "步骤 3/5：下载模型（约 160MB，从 GitHub 直连）"
$modelArgs = @((Join-Path $projectRoot 'scripts\download_models.py'))
if ($ForceModels) { $modelArgs += '--force' }
& $venvPython @modelArgs
if ($LASTEXITCODE -ne 0) {
    Write-Error "模型下载失败，请检查网络后重试。"
    exit 1
}

# ---------------------------------------------------------------- 4. 自检
Write-Step "步骤 4/5：冒烟自检"
& $venvPython -c @"
import sys
sys.path.insert(0, r'$projectRoot\src')
from vtw.asr import verify_model_files
ok, msg = verify_model_files()
print(('通过: ' if ok else '失败: ') + msg)
sys.exit(0 if ok else 1)
"@
if ($LASTEXITCODE -ne 0) {
    Write-Error "模型校验失败。"
    exit 1
}

# ---------------------------------------------------------------- 5. 自启
Write-Step "步骤 5/5：开机自启"
if ($SkipAutostart) {
    Write-Host "[跳过] 按参数要求未注册开机自启"
} else {
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'install_autostart.ps1')
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "自启注册失败，可稍后手动运行 scripts\install_autostart.ps1"
    }
}

Write-Host ""
Write-Host ("=" * 64)
Write-Host "  部署完成"
Write-Host ("=" * 64)
Write-Host "启动方式："
Write-Host "  · 双击 run.pyw（无控制台）"
Write-Host "  · 或运行：& `"$venvPython`" -m vtw -v  （带日志，便于排错）"
Write-Host ""
Write-Host "使用方式："
Write-Host "  · 按住 Ctrl+Alt+Space 说话，松开即可把文字输入到光标处"
Write-Host "  · 按 Ctrl+Alt+A 开始/停止会议（系统声音）转写"
Write-Host "  · 右键托盘图标可以切换模式、查看配置与日志"
Write-Host ""
Write-Host "自检脚本："
Write-Host "  scripts\test_hotkey.py    验证热键抑制"
Write-Host "  scripts\dictate_once.py   麦克风端到端测试"
Write-Host ""
