#Requires -Version 5.1
<#
.SYNOPSIS
    用 Windows 自带语音合成（SAPI）生成一段测试音频。

.DESCRIPTION
    用于在没有真人录音的情况下验证识别链路与"口述标点"功能。
    默认使用中文语音 Microsoft Huihui（随中文语言包安装）。

    注意：本文件必须保存为 **UTF-8 with BOM**。
    Windows PowerShell 5.1 会把无 BOM 的 .ps1 按 ANSI(GBK) 解码，
    中文文本会变成乱码，导致合成的音频内容不对。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\make_tts_sample.ps1
#>
[CmdletBinding()]
param(
    [string]$Text = "大家好逗号今天天气不错句号请问你叫什么名字问号感叹号换行新的一行",
    [string]$OutFile = "",
    [int]$Rate = -1
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech

if (-not $OutFile) {
    $projectRoot = Split-Path -Parent $PSScriptRoot
    $OutFile = Join-Path $projectRoot 'output\tts_punct_sample.wav'
}

$outDir = Split-Path -Parent $OutFile
if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Force -Path $outDir | Out-Null }

$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    # 优先选中文语音
    $zhVoice = $synth.GetInstalledVoices() |
        Where-Object { $_.VoiceInfo.Culture.Name -like 'zh*' -and $_.Enabled } |
        Select-Object -First 1
    if ($zhVoice) {
        $synth.SelectVoice($zhVoice.VoiceInfo.Name)
        Write-Host "使用语音：$($zhVoice.VoiceInfo.Name)"
    } else {
        Write-Host "[警告] 未找到中文语音，使用默认语音；识别结果可能不符预期。"
    }

    $synth.Rate = $Rate
    $synth.SetOutputToWaveFile($OutFile)
    $synth.Speak($Text) | Out-Null
    $synth.SetOutputToNull()
} finally {
    $synth.Dispose()
}

Write-Host "已生成：$OutFile"
Write-Host "文本内容：$Text"
Write-Host ("文件大小：{0} KB" -f [math]::Round((Get-Item $OutFile).Length / 1KB, 1))
