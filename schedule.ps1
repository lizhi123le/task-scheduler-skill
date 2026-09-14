#!/usr/bin/env pwsh
# Task Scheduler - PowerShell 全局命令包装器
# 允许在任意目录直接运行 schedule 命令
#
# 路径由 $PSScriptRoot 推导（原为写死的绝对路径，换机即失效）。

$scriptPath = Join-Path $PSScriptRoot 'scripts/schedule.py'

if (-not (Test-Path -LiteralPath $scriptPath)) {
    Write-Error "找不到 schedule.py：$scriptPath"
    exit 1
}

& python $scriptPath @args
exit $LASTEXITCODE
