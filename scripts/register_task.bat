@echo off
REM ============================================================
REM  注册 Windows 计划任务（包装 register_task.ps1）
REM
REM  必须「以管理员身份运行」本脚本。
REM  实际逻辑在 register_task.ps1 —— 因为 schtasks 命令行无法设置
REM  StartWhenAvailable（错过计划时间后补跑），而本机不保证 24 小时开机。
REM ============================================================
setlocal
chcp 65001 >nul
set ROOT=%~dp0..

net session >nul 2>&1
if not %errorlevel%==0 (
  echo.
  echo [错误] 需要管理员权限。
  echo        请右键本文件 -^> 「以管理员身份运行」。
  echo.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\register_task.ps1"
pause
endlocal
