@echo off
REM YouTube Daily Downloader - UTF-8 启动器
REM 修复 Windows 编码问题

chcp 65001 >nul

REM 设置 UTF-8 环境变量
set PYTHONIOENCODING=utf-8
set PYTHONUTF8MODE=1
set PYTHONLEGACYWINDOWSSTDIO=utf-8
set LC_ALL=C.UTF-8
set LANG=C.UTF-8

REM 显示启动信息
echo [INFO] YouTube Daily Downloader - UTF-8 模式
echo [INFO] 开始执行...
echo.

REM 使用 Python UTF-8 模式运行
python -X utf8 "%~dp0\youtube_daily.py" %*

exit /b %errorlevel%
