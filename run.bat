:: 云嘉每日资讯海报 - Windows 快捷运行脚本
@echo off
setlocal

set PYTHON=C:\Users\rdfes\.workbuddy\binaries\python\envs\default\Scripts\python.exe
set SCRIPT_DIR=%~dp0

cd /d "%SCRIPT_DIR%"

if "%1"=="" (
    "%PYTHON%" generate_poster.py
) else (
    "%PYTHON%" generate_poster.py %*
)

echo.
echo 完成! 输出文件在 output 目录下。
pause
