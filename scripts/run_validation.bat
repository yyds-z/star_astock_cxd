@echo off
REM ============================================================
REM  数据补齐后的完整验证链：
REM    1. 全量重建因子表
REM    2. 重算市场状态
REM    3. 刷新展示层快照
REM    4. 样本外回测（逐日回放）
REM    5. 回填 Skill 库历史成功率
REM
REM  全市场 2 年数据约需 20~30 分钟，建议后台运行。
REM ============================================================
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PY=D:\miniconda3\envs\astock\python.exe
set ROOT=%~dp0..
cd /d "%ROOT%"
if not exist "%ROOT%\data\logs" mkdir "%ROOT%\data\logs"
set LOG=%ROOT%\data\logs\validation.log

echo [1/5] 全量重建因子表 ...
"%PY%" -m astock.cli factor --all >> "%LOG%" 2>&1

echo [2/5] 重算市场状态 ...
"%PY%" -m astock.cli regime >> "%LOG%" 2>&1

echo [3/5] 刷新展示层快照 ...
"%PY%" -m astock.cli export >> "%LOG%" 2>&1

echo [4/5] 样本外回测（耗时最长）...
"%PY%" -m astock.cli backtest --top-n 5 >> "%LOG%" 2>&1

echo [5/5] 回填 Skill 库统计 ...
"%PY%" -m astock.cli skills sync >> "%LOG%" 2>&1

echo.
echo 验证链执行完成。日志：%LOG%
endlocal
