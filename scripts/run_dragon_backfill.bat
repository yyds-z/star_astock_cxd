@echo off
REM ============================================================
REM  龙虎榜回填（同花顺源，每天 1 次请求，限流 15 次/分钟）
REM
REM  为什么要独立脚本：全年约 242 个交易日 × 4.2 秒 ≈ 17 分钟，
REM  必须后台跑，且日志要单独落盘便于监控（与涨停池回填同一模式）。
REM  已入库的日期会被 pending_dragon_dates 跳过，可随时中断后重跑。
REM
REM  用法：
REM    scripts\run_dragon_backfill.bat          :: 默认回填最近 1 年
REM    scripts\run_dragon_backfill.bat 2        :: 回填最近 2 年
REM ============================================================
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PY=D:\miniconda3\envs\astock\python.exe
set ROOT=%~dp0..
cd /d "%ROOT%"

set YEARS=%1
if "%YEARS%"=="" set YEARS=1

if not exist "%ROOT%\data\logs" mkdir "%ROOT%\data\logs"
set LOG=%ROOT%\data\logs\dragon_backfill.log

echo 开始回填龙虎榜（最近 %YEARS% 年），日志：%LOG%
"%PY%" -m astock.cli dragon-tiger --years %YEARS% > "%LOG%" 2>&1
set RC=%errorlevel%

echo.
echo ============ 执行结果（尾部） ============
if exist "%LOG%" powershell -NoProfile -Command "Get-Content '%LOG%' -Tail 20"
exit /b %RC%
