@echo off
:: Task Scheduler - Add to Global Environment
:: Writing to the current user's environment does NOT require admin.

echo ============================================================
echo Task Scheduler - Global Environment Setup
echo ============================================================
echo.

set "TASK_SCHEDULER_DIR=C:\Users\%USERNAME%\.dsh\skills\task-scheduler"

:: Check directory exists
if not exist "%TASK_SCHEDULER_DIR%" (
    echo [ERROR] Directory not found: %TASK_SCHEDULER_DIR%
    echo Please verify task-scheduler is installed correctly
    pause
    exit /b 1
)

echo [INFO] Task Scheduler directory: %TASK_SCHEDULER_DIR%
echo.

:: Add to User PATH via PowerShell registry API (avoids setx's 1024-char truncation)
echo [>] Adding to User PATH environment variable...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$dir = $env:USERPROFILE + '\.dsh\skills\task-scheduler';" ^
  "$p = [Environment]::GetEnvironmentVariable('PATH', 'User');" ^
  "if ($p -like ('*' + $dir + '*')) { Write-Host '[OK] Already in PATH, skipping' }" ^
  "else { [Environment]::SetEnvironmentVariable('PATH', ($p + ';' + $dir), 'User'); Write-Host '[OK] Added to User PATH' }"

echo.
echo ============================================================
echo Installation Complete!
echo ============================================================
echo.
echo Usage:
echo   schedule list              - List all tasks
echo   schedule add "taskname"    - Add new task
echo   schedule logs              - View logs
echo.
echo Note: Please reopen Command Prompt / PowerShell for changes to take effect
echo.
pause
