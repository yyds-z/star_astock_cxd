@echo off
REM ============================================================
REM  每日盘后流程：自动补数 → 因子 → 市场状态 → 选股 → AI报告 → 复盘
REM  由 Windows 任务计划程序在每交易日 18:30 调用（scripts\register_task.bat）
REM ============================================================
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PY=D:\miniconda3\envs\astock\python.exe
set ROOT=%~dp0..
cd /d "%ROOT%"

REM 日期必须向 PowerShell 要，不能用 %date%：
REM 中文 Windows 的 %date% 形如「周三 2026/09/23」，星期几在最前面，
REM 按位置切片（%date:~0,4%）会得到「周三 22609」这种乱码文件名，
REM 而且该写法随区域设置变化，换台机器就崩。
set STAMP=
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"`) do set STAMP=%%i
if "%STAMP%"=="" set STAMP=unknown

if not exist "%ROOT%\data\logs" mkdir "%ROOT%\data\logs"
set LOG=%ROOT%\data\logs\daily_%STAMP%.log
set FAILMARK=%ROOT%\data\logs\LAST_DAILY_FAILED.txt

echo 开始执行每日流程，日志：%LOG%
"%PY%" -m astock.cli daily --workers 4 > "%LOG%" 2>&1
set RC=%errorlevel%

echo.
echo ============ 执行结果（尾部） ============
if exist "%LOG%" powershell -NoProfile -Command "Get-Content '%LOG%' -Tail 40"

if "%RC%"=="0" (
  if exist "%FAILMARK%" del "%FAILMARK%" >nul 2>&1
  echo.
  echo [成功] 每日流程已完成。
) else (
  echo.
  echo [失败] daily 退出码 %RC%，日志：%LOG%
  REM 留下失败标记：任务计划程序里也可以看「上次结果」，但那个藏在属性里，
  REM 容易被忽略。这里落一个文件，monitor.py 会把它顶到最上面。
  > "%FAILMARK%" echo 最近一次每日流程失败
  >>"%FAILMARK%" echo 时间: %DATE% %TIME%
  >>"%FAILMARK%" echo 退出码: %RC%
  >>"%FAILMARK%" echo 日志: %LOG%
  >>"%FAILMARK%" echo.
  >>"%FAILMARK%" echo 排查: python -m astock.cli status
)

REM 必须把 Python 的退出码透出去。
REM 否则 bat 最后一句话的退出码（通常是 0）会盖掉真实结果，
REM 任务计划程序永远显示「上次结果 0」，失败被静默吞掉。
endlocal & exit /b %RC%
