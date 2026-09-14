@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8MODE=1
set PYTHONLEGACYWINDOWSSTDIO=utf-8
set LC_ALL=C.UTF-8
set LANG=C.UTF-8

echo [UTF-8环境已设置]
echo 运行 YouTube Daily Downloader...
echo.

python -X utf8 "%~dp0\youtube_daily.py" %*

exit /b %errorlevel%
