#!/usr/bin/env pwsh
# Task Scheduler - 添加到全局环境变量脚本 (PowerShell)
# 写入当前用户的用户环境变量，无需管理员权限

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "Task Scheduler - Global Environment Setup" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# 设置变量
$taskSchedulerDir = "$env:USERPROFILE\.config\opencode\skills\task-scheduler"
$pathEntry = $taskSchedulerDir

# 检查目录是否存在
if (-not (Test-Path $taskSchedulerDir)) {
    Write-Host "[ERROR] 目录不存在: $taskSchedulerDir" -ForegroundColor Red
    Write-Host "请确认 task-scheduler 技能已正确安装" -ForegroundColor Yellow
    pause
    exit 1
}

Write-Host "[INFO] Task Scheduler 目录: $taskSchedulerDir" -ForegroundColor Green
Write-Host ""

# 获取当前用户 PATH
$currentPath = [Environment]::GetEnvironmentVariable("PATH", "User")

# 检查是否已在 PATH 中
if ($currentPath -like "*$pathEntry*") {
    Write-Host "[OK] 已在 PATH 中，跳过添加" -ForegroundColor Green
} else {
    # 添加到 PATH (注册表 API，不受 setx 1024 字符限制)
    if ([string]::IsNullOrEmpty($currentPath)) {
        $newPath = $pathEntry
    } else {
        $newPath = "$currentPath;$pathEntry"
    }

    [Environment]::SetEnvironmentVariable("PATH", $newPath, "User")
    Write-Host "[OK] 已添加到用户 PATH" -ForegroundColor Green
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "安装完成！" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "使用方法:" -ForegroundColor Yellow
Write-Host "  schedule list              - 查看所有任务"
Write-Host "  schedule add '任务名' ...  - 添加新任务"
Write-Host "  schedule logs              - 查看日志"
Write-Host ""
Write-Host "注意: 请重新打开 PowerShell 窗口以使更改生效" -ForegroundColor Magenta
Write-Host ""
pause
