@echo off
REM ============================================================
REM  财务三表回填 + 自愈守护（由计划任务 AStockAI_Finance 每 30 分钟调用）
REM
REM  为什么这么久：同花顺接口**限流 15 次/分钟**（4.2 秒/次），
REM  可交易池约 4900 只 × 每只 2 次请求（不取现金流量表）≈ 9800 次请求
REM  → 实测约 7.6 秒/只，合计约 10.4 小时。
REM  这是服务端硬约束，加线程无用（限流按账号计，并发只会撞 429）。
REM
REM  【防中断设计】
REM   ① 独立守护日志：**不能**和回填共用日志文件 ——
REM      回填进程占着它，守护写不进去会报「文件被占用」，
REM      而偏偏"回填在跑"正是守护最常走的路径。
REM   ② 主库被占用则退出：用 check_db_free.py 判断能否只读打开，
REM      **不查进程命令行**（经 cmd → powershell 两层转义后引号会被破坏，
REM      实测静默返回 0，守护会重复启动抢锁）。
REM   ③ 完成后写 .done 标记，之后计划任务立刻退出（避免永久空跑）。
REM   ④ 卡死告警：进程活着但不干活时，守护只能告警不能自愈（见下），
REM      因此至少把"多久没心跳"写进守护日志，便于第二天排查。
REM   ⑤ 进度逐股提交 + pending 续传：崩溃最多丢 1 只，重跑即续。
REM
REM  重跑一轮：先删 data\logs\finance_backfill.done
REM ============================================================
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PY=D:\miniconda3\envs\astock\python.exe
set ROOT=%~dp0..
cd /d "%ROOT%"
if not exist "%ROOT%\data\logs" mkdir "%ROOT%\data\logs"
set LOG=%ROOT%\data\logs\finance_backfill.log
set GLOG=%ROOT%\data\logs\finance_guard.log
set MARKER=%ROOT%\data\logs\finance_backfill.done

REM ---- ① 已完成则退出 ----
if exist "%MARKER%" (
    echo [%DATE% %TIME%] 已存在完成标记，无需再跑。 >> "%GLOG%"
    exit /b 0
)

REM ---- ② 状态检查（主库空闲？心跳正常？）----
REM 全部判断收在 Python 里：批处理里的 PowerShell 嵌套引号会静默失效，
REM 实测踩过两次（进程检测恒返回 0、文件时间恒为 0），都是"看起来正常但检测失效"。
REM 也**不用 `for /f` 捕获输出** —— 它会吞掉子进程的 errorlevel，
REM 于是"检测结果"和"是否跳过"会脱钩。直接重定向输出、用 errorlevel 判断。
"%PY%" "%ROOT%\scripts\check_db_free.py" >> "%GLOG%" 2>&1
if errorlevel 1 (
    echo [%DATE% %TIME%] ^-- 本次跳过启动（主库被占用或状态异常，详见上一行） >> "%GLOG%"
    exit /b 0
)

:run
echo [%DATE% %TIME%] 启动财务回填 >> "%GLOG%"
"%PY%" -m astock.cli finance --all >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%
echo [%DATE% %TIME%] 回填进程退出，返回码 %RC% >> "%GLOG%"

REM ---- ③ 只有正常跑完才写完成标记；非 0 说明异常退出，留给下次续跑 ----
if "%RC%"=="0" (
    echo [%DATE% %TIME%] 财务回填已完成（详见 %LOG% 尾部的采集汇总） > "%MARKER%"
    echo [%DATE% %TIME%] 已写入完成标记。自检：python -m astock.cli finance --show 20 >> "%GLOG%"
)
endlocal
