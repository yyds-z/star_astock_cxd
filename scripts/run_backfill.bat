@echo off
REM ============================================================
REM  全市场历史数据回填（首次使用执行一次）
REM  预计耗时约 1 小时；中途关机/断网后重新双击即可续传
REM ============================================================
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PY=D:\miniconda3\envs\astock\python.exe
set ROOT=%~dp0..
cd /d "%ROOT%"

echo [1/2] 初始化数据库 ...
"%PY%" -m astock.cli initdb

echo.
echo [2/2] 开始回填（每批 600 只 / 3 进程）...
REM 并发保持 2~3：baostock 登录过频会被拉黑，akshare/新浪 高频请求也会限流。
REM 主源不可用时会自动降级到 akshare，无需手动指定。
"%PY%" scripts\run_full_backfill.py --batch 600 --workers 3

echo.
echo 回填流程结束。若显示覆盖率不足 100%%，重新运行本脚本即可继续。
pause
endlocal
