@echo off
REM ============================================================
REM  运行全部自检（测试脚本在 tests\ 目录）
REM  三项测试都不需要启动服务，也不占用主数据库
REM ============================================================
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PY=D:\miniconda3\envs\astock\python.exe
set ROOT=%~dp0..
cd /d "%ROOT%"

echo ============ 1) 数据解析回归测试（离线，最快）============
"%PY%" tests\test_hithink_parsers.py
set R0=%errorlevel%

echo.
echo ============ 2) 报告生成回归测试 ============
"%PY%" tests\test_report.py
set R1=%errorlevel%

echo.
echo ============ 3) 接口冒烟测试（串行 + 并发）============
"%PY%" tests\smoke_test.py
set R2=%errorlevel%

echo.
echo ============ 4) 展示层读取器并发回归 ============
"%PY%" tests\test_serving_concurrency.py
set R3=%errorlevel%

echo.
set FAIL=0
if not %R0%==0 set FAIL=1
if not %R1%==0 set FAIL=1
if not %R2%==0 set FAIL=1
if not %R3%==0 set FAIL=1
if %FAIL%==0 (
  echo 全部自检通过
) else (
  echo 存在失败项：解析=%R0%，报告=%R1%，接口=%R2%，并发=%R3%
)
endlocal & exit /b %FAIL%
