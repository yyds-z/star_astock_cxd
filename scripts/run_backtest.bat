@echo off
REM ============================================================
REM  样本外验证：历史逐日回放并统计各策略真实胜率
REM  用法：scripts\run_backtest.bat [额外参数]
REM     scripts\run_backtest.bat --months 6
REM     scripts\run_backtest.bat --tiers swing,short
REM     scripts\run_backtest.bat --list          （查看历史回测运行）
REM ============================================================
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PY=D:\miniconda3\envs\astock\python.exe
set ROOT=%~dp0..
cd /d "%ROOT%"
if not exist "%ROOT%\data\logs" mkdir "%ROOT%\data\logs"
set LOG=%ROOT%\data\logs\backtest.log

echo 开始回测，日志：%LOG%
"%PY%" -m astock.cli backtest %* > "%LOG%" 2>&1

echo.
echo ============ 结果（尾部） ============
powershell -NoProfile -Command "Get-Content '%LOG%' -Tail 45"
endlocal
