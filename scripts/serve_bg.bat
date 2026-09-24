@echo off
REM 后台启动本地服务（供计划任务或快速验证使用），日志写入 logs\serve.log
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PY=D:\miniconda3\envs\astock\python.exe
set ROOT=%~dp0..
cd /d "%ROOT%"
if not exist "%ROOT%\data\logs" mkdir "%ROOT%\data\logs"
"%PY%" -m astock.cli serve --host 127.0.0.1 --port 8000 > "%ROOT%\data\logs\serve.log" 2>&1
endlocal
