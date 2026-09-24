# -*- coding: utf-8 -*-
"""写任务启动前的状态检查：主库是否空闲 + 长任务心跳是否正常。

退出码：
    0 = 主库空闲 → 可以启动写任务
    1 = 主库被占用（正常情况：别的写任务在跑）→ 静默跳过
    2 = 主库被**财务回填**占用，且回填日志超过阈值未更新 → 疑似卡死，需告警

------------------------------------------------------------------
为什么判断逻辑放在 Python 而不是 .bat
------------------------------------------------------------------
在批处理里用 PowerShell 判断踩过两个"静默失效"的坑：
  ① 查进程命令行经 cmd → powershell 两层转义后引号被破坏，恒返回 0
     → 守护重复启动、抢 DuckDB 独占锁；
  ② `for /f ('powershell ... ''%LOG%'' ...')` 取文件时间时单引号二次转义
     没生效，PowerShell 收到空路径异常退出 → STALE 恒为 0，
     卡死告警永远不触发，而表面上一切正常。
放进 Python 后：`subprocess.run([...])` 用**参数列表**调用，不经过任何
shell 转义，引号原样传递。
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb  # noqa: E402

from astock.config import get_config  # noqa: E402

STALE_MINUTES = 20
BACKFILL_LOG = "finance_backfill.log"


def _python_cmdlines() -> list[str]:
    """当前所有 python 进程的命令行。用参数列表调用，无 shell 转义问题。"""
    try:
        out = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                "Select-Object -ExpandProperty CommandLine",
            ],
            capture_output=True, text=True, timeout=25,
        ).stdout
        return [ln.strip() for ln in out.splitlines() if ln.strip()]
    except Exception:  # noqa: BLE001
        return []


def main() -> int:
    cfg = get_config()
    try:
        con = duckdb.connect(str(cfg.duckdb_path), read_only=True)
        con.close()
        print("主库空闲")
        return 0
    except Exception as exc:  # noqa: BLE001
        occupied = str(exc)[:100].replace("\n", " ")

    cmds = _python_cmdlines()
    holders = []
    for c in cmds:
        for task in ("cli backtest", "cli daily", "cli backfill", "cli finance",
                     "cli review", "cli attribution", "cli factor"):
            if task in c:
                holders.append(task.replace("cli ", ""))
    who = "、".join(sorted(set(holders))) or "未知任务"

    # 只有「占用者确实是财务回填」时才谈卡死。
    # 否则会误报：回填早已正常结束，而主库被回测占用 —— 日志自然长时间不更新，
    # 却被读成"回填卡死"。这类误报会让人不再相信告警，比没有告警更糟。
    if "finance" not in holders:
        print(f"主库被占用（占用者：{who}），非财务回填，跳过心跳判定")
        return 1

    log = Path(cfg.data_dir) / "logs" / BACKFILL_LOG
    if not log.exists():
        print("主库被财务回填占用，但日志不存在，无法判断进度")
        return 1
    age = (datetime.now() - datetime.fromtimestamp(log.stat().st_mtime)).total_seconds() / 60
    if age >= STALE_MINUTES:
        print(
            f"⚠ 财务回填疑似卡死：日志已 {age:.0f} 分钟未更新"
            f"（阈值 {STALE_MINUTES} 分钟）。守护不会自动杀进程（盲杀有风险），"
            "请手动检查：python scripts\\monitor.py"
        )
        return 2
    print(f"主库被财务回填占用，进行中（日志 {age:.0f} 分钟前更新）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
