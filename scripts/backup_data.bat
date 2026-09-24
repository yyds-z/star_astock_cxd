@echo off
REM ============================================================
REM  数据备份：主库 + Skill 库 + 报告 + 回测明细
REM
REM  默认备份到 data\backups\（与主库同盘，只能防误删，防不了磁盘损坏）
REM  想真正防灾，请改成另一个盘：
REM      scripts\backup_data.bat --dest E:\astock_backup
REM  建议每周执行一次，或注册到任务计划程序。
REM ============================================================
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PY=D:\miniconda3\envs\astock\python.exe
set ROOT=%~dp0..
cd /d "%ROOT%"

"%PY%" scripts\backup_data.py %*

endlocal
