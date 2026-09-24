@echo off
REM ============================================================
REM  astock_ai 环境依赖安装脚本
REM  用法：双击运行，或 cmd /c "scripts\setup_env.bat"
REM ============================================================
setlocal
set PY=D:\miniconda3\envs\astock\python.exe
set MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple
set ROOT=%~dp0..
set LOGDIR=%ROOT%\data\logs
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

echo [1/3] 升级 pip ...
"%PY%" -m pip install --upgrade pip -i %MIRROR% > "%LOGDIR%\pip_pip.log" 2>&1

echo [2/3] 安装基础依赖 ...
"%PY%" -m pip install duckdb pandas numpy pyarrow requests tenacity tqdm pyyaml python-dotenv pydantic-settings -i %MIRROR% > "%LOGDIR%\pip_base.log" 2>&1

echo [3/3] 安装数据源与 Web 依赖 ...
"%PY%" -m pip install baostock fastapi "uvicorn[standard]" openai -i %MIRROR% > "%LOGDIR%\pip_data.log" 2>&1
"%PY%" -m pip install akshare adata -i %MIRROR% > "%LOGDIR%\pip_extra.log" 2>&1

echo.
echo ============ 依赖校验 ============
"%PY%" "%ROOT%\scripts\check_env.py"
echo.
echo 安装完成。完整日志见 %LOGDIR%
endlocal
