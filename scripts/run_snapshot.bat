@echo off
REM ============================================================
REM  盘中快照采集（每交易日 14:00）
REM  由 Windows 任务计划程序调用（scripts\register_task.ps1）
REM
REM  为什么不并进 run_daily.bat：
REM    daily 依赖 `data.data_ready_time = 16:00` 守卫，14:00 运行时
REM    `calendar.resolve_data_date` 会**主动回退到上一交易日** —— 结果是
REM    产出一份「昨天的数据日 + 今天的计划买入日」的错位报告，
REM    而今天的开盘早已过去，报告里"次日开盘买入"的计划既无数据也不可执行。
REM    盘中决策要的是「当天此刻」的截面，只能走独立链路。
REM
REM  这是**唯一有时效性**的一步：免费源没有历史盘中数据，
REM    晚一天开始记录，就永远少一天可回测的样本。
REM ============================================================
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PY=D:\miniconda3\envs\astock\python.exe
set ROOT=%~dp0..
cd /d "%ROOT%"

REM 日期向 PowerShell 要，不用 %date%（中文 Windows 的 %date% 星期几在前，切片会乱）
set STAMP=
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"`) do set STAMP=%%i
if "%STAMP%"=="" set STAMP=unknown

if not exist "%ROOT%\data\logs" mkdir "%ROOT%\data\logs"
set LOG=%ROOT%\data\logs\snapshot_%STAMP%.log

echo 开始采集盘中快照，日志：%LOG%
"%PY%" -m astock.cli snapshot --slot 14:00 > "%LOG%" 2>&1
set RC=%errorlevel%

if not "%RC%"=="0" (
  echo [失败] snapshot 退出码 %RC%，日志：%LOG%
  powershell -NoProfile -Command "Get-Content '%LOG%' -Tail 20"
  endlocal & exit /b %RC%
)

REM ---- 快照采集成功后重建报告（幂等）----
REM 为什么：报告的「影子信号候选」板块会自动关联**计划交易日的快照实况**
REM （现价/涨幅/量比/可执行性分类）。18:30 生成报告时计划日快照尚不存在，
REM 只有信号表；14:00 采集完快照立即重建，报告才带上「此刻能不能买」的实况——
REM 这正是 14:00 决策的输出形态。--no-refresh：数据日仍是上一交易日（守卫回退），
REM 全程只读库重建，不会拉任何新数据，幂等可重复。
set LOG2=%ROOT%\data\logs\report_rebuild_%STAMP%.log
"%PY%" -m astock.cli daily --no-refresh --no-llm > "%LOG2%" 2>&1
set RC2=%errorlevel%
if not "%RC2%"=="0" (
  echo [提示] 报告重建失败（不影响快照数据），日志：%LOG2%
  powershell -NoProfile -Command "Get-Content '%LOG2%' -Tail 10"
)

REM 以快照采集的结果为总退出码（报告重建失败不改变快照已成功的事实）
endlocal & exit /b %RC%

REM 必须透出 Python 退出码，否则 bat 最后一句话的 0 会盖掉真实结果
endlocal & exit /b %RC%
