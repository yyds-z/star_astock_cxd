@echo off
REM ============================================================
REM  启动本地 Web 服务，浏览器访问 http://127.0.0.1:8000
REM ============================================================
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PY=D:\miniconda3\envs\astock\python.exe
set ROOT=%~dp0..
cd /d "%ROOT%"

echo 启动本地服务：http://127.0.0.1:8000  （按 Ctrl+C 停止）
"%PY%" -m astock.cli serve --host 127.0.0.1 --port 8000
endlocal
