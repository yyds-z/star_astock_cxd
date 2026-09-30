# -*- coding: utf-8 -*-
"""后台任务监控（任务感知）。

为什么需要它：DuckDB 主库是独占文件锁，任务运行期间无法执行
`astock.cli status`（现在可加 --snapshot 绕过）。因此本脚本改为
「看进程 → 看日志 → 尝试轻量查库」的组合方式。

与早期版本的关键差异：**先判断当前在跑什么任务，再展示对应的进度**。
早期版本只认「回填」，导致跑回测时会把上一轮回填的残留日志当作当前状态展示，
给出「采集已完成」这类误导性结论。

用法：
    python scripts/monitor.py            # 自动识别当前任务
    python scripts/monitor.py backfill   # 强制按回填视角查看
    python scripts/monitor.py backtest   # 强制按回测视角查看
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 日志统一放在 data/logs（由 config 的 log_dir 决定）。
# 这里做一次兼容：早期脚本把 stdout 重定向到了项目根的 logs/，两处都要能找到。
try:
    sys.path.insert(0, str(ROOT))
    from astock.config import get_config

    LOGS = get_config().log_dir
except Exception:  # noqa: BLE001
    LOGS = ROOT / "data" / "logs"
FALLBACK_LOGS = ROOT / "logs"

# Windows 控制台默认 GBK，日志含非 GBK 字符时会抛 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

RE_TS = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"

# ---------------- 回填 ----------------
RE_BATCH_START = re.compile(RE_TS + r".*第 (\d+) 批：剩余 (\d+) 只待采集")
RE_BATCH_TASK = re.compile(RE_TS + r".*待采集 (\d+) 只股票")
RE_PROGRESS = re.compile(RE_TS + r".*进度 (\d+)/(\d+) \| 入库 (\d+) 行 \| 失败 (\d+)")
RE_BATCH_DONE = re.compile(RE_TS + r".*第 (\d+) 批完成：入库 (\d+) 行，失败 (\d+) 只")
RE_ALL_DONE = re.compile(RE_TS + r".*全部完成")
RE_ABORTED = re.compile(RE_TS + r".*全部失败")

# ---------------- 回测 ----------------
RE_BT_PROGRESS = re.compile(
    RE_TS + r".*回放进度 (\d+)/(\d+)（(\d+)%）\| 累计信号 (\d+) 条"
              r" \| 已用 ([\d.]+) 分钟 \| 预计剩余 ([\d.]+) 分钟"
)
RE_BT_START = re.compile(RE_TS + r".*开始回放：(\S+) ~ (\S+)，共 (\d+) 个交易日")
RE_BT_DONE = re.compile(RE_TS + r".*回放完成：信号 (\d+) 条，覆盖 (\d+) 个交易日，耗时 ([\d.]+) 分钟")
RE_BT_ERROR = re.compile(RE_TS + r".*\[(\w+)\] 策略计算异常，本次.*信号全部丢失")

# ---------------- 涨停池采集 ----------------
# 日级明细行：[2025-12-23] 涨停 62 只（已入库 62），炸板 21 只（已入库 21）
RE_LP_DAY = re.compile(
    RE_TS + r".*\[(\d{4}-\d{2}-\d{2})\] 涨停 (\d+) 只（已入库 (\d+)），炸板 (\d+) 只（已入库 (\d+)）"
)
# 周期进度行：涨停池采集进度 60/243（2025-12-23）| 预计剩余 25.4 分钟
RE_LP_PROGRESS = re.compile(
    RE_TS + r".*涨停池采集进度 (\d+)/(\d+)（(\S+)）\| 预计剩余 ([\d.]+) 分钟"
)
RE_LP_DONE = re.compile(RE_TS + r".*采集完成：(\d+) 个交易日（(\S+) ~ (\S+)）")

# ---------------- 龙虎榜采集 ----------------
RE_DT_DAY = re.compile(RE_TS + r".*\[(\d{4}-\d{2}-\d{2})\] 龙虎榜 (\d+) 条")
RE_DT_PROGRESS = re.compile(
    RE_TS + r".*龙虎榜采集进度 (\d+)/(\d+)（(\S+)）\| 预计剩余 ([\d.]+) 分钟"
)

# ---------------- 财务采集 ----------------
RE_FIN_PROGRESS = re.compile(
    RE_TS + r".*财务采集进度 (\d+)/(\d+)（(\S+)）\| 预计剩余 ([\d.]+) 分钟"
)
RE_FIN_DONE = re.compile(RE_TS + r".*财务采集完成：(\d+) 只股票，共 (\d+) 期报表")

# 任务名 → 展示名
TASK_LABEL = {
    "backfill": "历史数据回填",
    "backtest": "策略回测",
    "daily": "每日选股",
    "factor": "因子计算",
    "regime": "市场状态计算",
    "review": "复盘",
    "export": "快照导出",
    "skills": "Skill 库同步",
    "index": "指数同步",
    "limit-pool": "涨停池采集（同花顺源）",
    "dragon-tiger": "龙虎榜采集（同花顺源）",
    "auction": "集合竞价采集（同花顺源）",
    "finance": "财务三表回填（同花顺源，约 11.5 小时）",
    "attribution": "复盘归因（LLM）",
    "finance": "财务数据采集（同花顺源，每只 3 次请求）",
    "attribution": "复盘归因（LLM）",
    "sector": "行业映射采集",
    "sector-strength": "板块强度计算",
    "serve": "本地 Web 服务（只读快照，不占主库）",
}

# 会抢占主库写锁的任务
WRITE_TASKS = {
    "backfill", "backtest", "daily", "factor", "regime", "review", "index",
    "limit-pool", "dragon-tiger", "auction", "finance", "attribution",
    "sector", "sector-strength",
}


def detect_tasks() -> tuple[list[str], list[str], list[int]]:
    """返回 (进程描述行, 正在运行的任务名, 写库任务的 PID)。"""
    ps_cmd = (
        'Get-CimInstance Win32_Process -Filter "Name=%s" | '
        "ForEach-Object { $_.ProcessId.ToString() + '|' + $_.CommandLine }"
    ) % "'python.exe'"
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=40,
        ).stdout
    except Exception as exc:  # noqa: BLE001
        return [f"无法获取进程列表: {exc}"], [], []

    rows: list[str] = []
    running: list[str] = []
    write_pids: list[int] = []
    for line in out.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        pid, cmd = line.split("|", 1)
        pid, cmd = pid.strip(), cmd.strip()
        if not pid.isdigit():
            continue
        # 只保留本项目相关的进程，避免把其它 python 程序算进来
        if "astock" not in cmd and "run_full_backfill" not in cmd:
            continue
        rows.append(f"  PID={pid}  {cmd[:110]}")

        task = _task_of(cmd)
        if task is None:
            continue
        if task not in running:
            running.append(task)
        if task in WRITE_TASKS:
            write_pids.append(int(pid))
    return rows, running, write_pids


def _task_of(cmd: str) -> str | None:
    """从命令行解析出任务名。

    必须用 `[\w-]+` 而不是 `\w+`：`limit-pool`、`sector-strength` 这类
    带连字符的子命令名会被 `\w+` 截断成 `limit` / `sector`，
    随后在任务清单里查不到，监控器就会给出「没有运行中的写库任务」的错误结论
    （真的发生过：回填明明在跑，监控却说没有任何任务）。
    """
    if "run_full_backfill" in cmd:
        return "backfill"
    m = re.search(r"astock\.cli\s+([\w-]+)", cmd)
    return m.group(1) if m else None


def load_lines(marker: str | None = None) -> tuple[list[str], str]:
    """加载日志行，可选地从最后一个 marker 处截断。

    优先读 `logs/astock.log`：它由日志组件以 UTF-8 恒定写入，
    而 `backfill.log` 是 stdout 重定向，编码受控制台代码页影响（可能是 GBK），
    中文进度行会变成乱码导致无法解析。
    """
    candidates = [
        LOGS / "astock.log",
        FALLBACK_LOGS / "astock.log",
        LOGS / "validation.log",
        LOGS / "backfill.log",
    ]
    for path in candidates:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = [ln for ln in text.splitlines() if "astock." in ln]
        if not lines:
            continue
        if marker:
            start = None
            for i, ln in enumerate(lines):
                if marker in ln:
                    start = i
            if start is not None:
                lines = lines[start:]
        return lines, str(path.relative_to(ROOT)).replace("\\", "/")
    return [], "-"


def _ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def parse_backfill(lines: list[str]) -> dict:
    """解析回填进度。"""
    info: dict = {
        "batch": 0,
        "remaining": None,
        "batch_size": None,
        "done_in_batch": 0,
        "rows_so_far": 0,
        "failed_so_far": 0,
        "eta_sec": None,
        "total_eta_sec": None,
        "rate": None,
        "batch_failing": False,
        "finished": False,
        "aborted": False,
        "last_progress": None,
        "last_progress_at": None,
        "prev_progress": None,
        "prev_progress_at": None,
        "last_line_at": None,
    }

    for ln in lines:
        ts_match = re.match(RE_TS, ln)
        if ts_match:
            info["last_line_at"] = ts_match.group(1)

        m = RE_BATCH_START.search(ln)
        if m:
            info["batch"] = int(m.group(2))
            info["remaining"] = int(m.group(3))
            info["done_in_batch"] = 0
            info["prev_progress"] = None
            continue

        m = RE_BATCH_TASK.search(ln)
        if m:
            info["batch_size"] = int(m.group(2))
            info["done_in_batch"] = 0
            continue

        # 注意：RE_TS 已占用 group(1)，因此业务字段从 group(2) 开始
        m = RE_PROGRESS.search(ln)
        if m:
            info["prev_progress"] = info["last_progress"]
            info["prev_progress_at"] = info["last_progress_at"]
            info["last_progress"] = (int(m.group(2)), int(m.group(3)))
            info["last_progress_at"] = _ts(m.group(1))
            info["done_in_batch"] = int(m.group(2))
            info["rows_so_far"] = int(m.group(4))
            info["failed_so_far"] = int(m.group(5))
            continue

        m = RE_BATCH_DONE.search(ln)
        if m:
            info["done_in_batch"] = info["batch_size"] or info["done_in_batch"]
            info["rows_so_far"] = int(m.group(3))
            info["failed_so_far"] = int(m.group(4))
            continue

        if RE_ALL_DONE.search(ln):
            info["finished"] = True
        elif RE_ABORTED.search(ln):
            info["aborted"] = True

    # 用最近两次进度点估算速率
    if (
        info["prev_progress"]
        and info["last_progress"]
        and info["prev_progress_at"]
        and info["last_progress_at"]
        and info["batch_size"]
    ):
        delta_n = info["last_progress"][0] - info["prev_progress"][0]
        delta_t = (info["last_progress_at"] - info["prev_progress_at"]).total_seconds()
        if delta_n > 0 and delta_t > 0:
            rate = delta_n / delta_t
            rest = info["batch_size"] - info["last_progress"][0]
            info["eta_sec"] = rest / rate if rest > 0 else 0
            info["rate"] = rate

    # 整批全部失败时不估算时间：此时速率没有参考意义
    info["batch_failing"] = bool(
        info["done_in_batch"] > 0
        and info["rows_so_far"] == 0
        and info["failed_so_far"] >= info["done_in_batch"]
    )

    if info.get("rate") and info["batch_size"] and info["remaining"] and not info["batch_failing"]:
        per_batch = info["batch_size"] / info["rate"]
        batches_left = max(0, (info["remaining"] - 1) // info["batch_size"])
        info["total_eta_sec"] = (info["eta_sec"] or 0) + batches_left * per_batch

    return info


def parse_backtest(lines: list[str]) -> dict:
    """解析回测进度。"""
    info: dict = {
        "started": False,
        "start_at": None,
        "range": None,
        "total_days": None,
        "done_days": 0,
        "pct": 0,
        "signals": 0,
        "elapsed_min": None,
        "eta_min": None,
        "finished": False,
        "cover_days": None,
        "strategy_errors": {},
    }
    for ln in lines:
        m = RE_BT_START.search(ln)
        if m:
            info["started"] = True
            info["start_at"] = m.group(1)
            info["range"] = (m.group(2), m.group(3))
            info["total_days"] = int(m.group(4))
            info["done_days"] = 0
            info["signals"] = 0
            continue

        m = RE_BT_PROGRESS.search(ln)
        if m:
            info["done_days"] = int(m.group(2))
            info["total_days"] = int(m.group(3))
            info["pct"] = int(m.group(4))
            info["signals"] = int(m.group(5))
            info["elapsed_min"] = float(m.group(6))
            info["eta_min"] = float(m.group(7))
            continue

        m = RE_BT_DONE.search(ln)
        if m:
            info["finished"] = True
            info["signals"] = int(m.group(2))
            info["cover_days"] = int(m.group(3))
            info["elapsed_min"] = float(m.group(4))
            info["pct"] = 100
            continue

        m = RE_BT_ERROR.search(ln)
        if m:
            name = m.group(2)
            info["strategy_errors"][name] = info["strategy_errors"].get(name, 0) + 1

    return info


def parse_limit_pool(lines: list[str]) -> dict:
    """解析涨停池采集进度。

    行数用「逐日累加」而不是从进度行取：进度行只出现在每 20 天的节点，
    日级明细行则每天都有一条，能给出更实时的入库量。
    """
    info: dict = {
        "done": 0,
        "total": None,
        "current_date": None,
        "eta_min": None,
        "up_rows": 0,
        "break_rows": 0,
        "finished": False,
        "range": None,
    }
    for ln in lines:
        m = RE_LP_DAY.search(ln)
        if m:
            info["current_date"] = m.group(2)
            info["up_rows"] += int(m.group(4))
            info["break_rows"] += int(m.group(6))
            continue

        m = RE_LP_PROGRESS.search(ln)
        if m:
            info["done"] = int(m.group(2))
            info["total"] = int(m.group(3))
            info["eta_min"] = float(m.group(5))
            continue

        m = RE_LP_DONE.search(ln)
        if m:
            info["finished"] = True
            info["done"] = int(m.group(2))
            info["range"] = (m.group(3), m.group(4))
    return info


def parse_dragon_tiger(lines: list[str]) -> dict:
    """解析龙虎榜采集进度（结构同涨停池，便于复用展示逻辑）。"""
    info: dict = {
        "done": 0,
        "total": None,
        "current_date": None,
        "eta_min": None,
        "rows": 0,
        "finished": False,
    }
    for ln in lines:
        m = RE_DT_DAY.search(ln)
        if m:
            info["current_date"] = m.group(2)
            info["rows"] += int(m.group(3))
            continue
        m = RE_DT_PROGRESS.search(ln)
        if m:
            info["done"] = int(m.group(2))
            info["total"] = int(m.group(3))
            info["eta_min"] = float(m.group(5))
    return info


def parse_finance(lines: list[str]) -> dict:
    """解析财务采集进度（全市场回填约 17 小时，必须能看到进度）。"""
    info: dict = {"done": 0, "total": None, "current": None, "eta_min": None,
                  "finished": False, "stocks": None, "periods": None}
    for ln in lines:
        m = RE_FIN_PROGRESS.search(ln)
        if m:
            info["done"] = int(m.group(2))
            info["total"] = int(m.group(3))
            info["current"] = m.group(4)
            info["eta_min"] = float(m.group(5))
            continue
        m = RE_FIN_DONE.search(ln)
        if m:
            info["finished"] = True
            info["stocks"] = int(m.group(2))
            info["periods"] = int(m.group(3))
    return info


def _show_finance(info: dict, live: bool) -> None:
    if info["finished"]:
        print(f"  状态：已完成　采集 {info['stocks']} 只股票，共 {info['periods']} 期报表")
        return
    if info["total"] is None:
        print("  尚未产生首个进度点（每 50 只记录一次，启动后稍等即可）。")
        return
    done, total = info["done"], info["total"]
    print(f"  最新处理：{info['current']}　已完成 {done}/{total} 只")
    print(f"  进度：[{_bar(done, total)}] {done}/{total} ({done / total * 100:.0f}%)")
    if live and info["eta_min"] is not None:
        print(f"  预计剩余：约 {info['eta_min'] / 60:.1f} 小时"
              "（每只 3 次请求，速度固定，无法加快）")


def _show_dragon_tiger(info: dict, live: bool) -> None:
    if info["total"] is None and not info["current_date"]:
        print("  未找到龙虎榜采集记录。")
        return
    done, total = info["done"], info["total"] or 0
    print(f"  最新采集日：{info['current_date'] or '-'}　"
          f"已完成 {done}/{total or '?'} 个交易日")
    if total:
        print(f"  进度：[{_bar(done, total)}] {done}/{total} ({done / total * 100:.0f}%)")
    print(f"  已入库：{info['rows']} 条")
    if live and info["eta_min"] is not None:
        print(f"  预计剩余：约 {info['eta_min']:.1f} 分钟（每天 1 次请求，速度固定）")


def _show_limit_pool(info: dict, live: bool) -> None:
    if info["finished"]:
        rng = f"（{info['range'][0]} ~ {info['range'][1]}）" if info["range"] else ""
        print(f"  状态：已完成　采集 {info['done']} 个交易日{rng}")
        print(f"  入库：涨停 {info['up_rows']} 条　炸板 {info['break_rows']} 条")
        return
    if info["total"] is None and not info["current_date"]:
        print("  未找到涨停池采集记录。")
        return
    done, total = info["done"], info["total"] or 0
    print(f"  最新采集日：{info['current_date'] or '-'}　"
          f"已完成 {done}/{total or '?'} 个交易日")
    if total:
        print(f"  进度：[{_bar(done, total)}] {done}/{total} ({done / total * 100:.0f}%)")
    print(f"  已入库：涨停 {info['up_rows']} 条　炸板 {info['break_rows']} 条")
    if info["eta_min"] is not None:
        print(f"  预计剩余：约 {info['eta_min']:.1f} 分钟（限流 15 次/分钟，速度固定）")


def try_db_counts() -> dict | None:
    """尝试轻量只读查库；若主库被写任务占用则返回 None。"""
    try:
        import duckdb

        sys.path.insert(0, str(ROOT))
        from astock.config import get_config

        db = str(get_config().duckdb_path)
        con = duckdb.connect(db, read_only=True)
        try:
            total = con.execute("SELECT COUNT(*) FROM dim_stock").fetchone()[0]
            covered = con.execute("SELECT COUNT(DISTINCT code) FROM dwd_daily_bar").fetchone()[0]
            bars = con.execute("SELECT COUNT(*) FROM dwd_daily_bar").fetchone()[0]
        finally:
            con.close()
        return {
            "total": total,
            "covered": covered,
            "bars": bars,
            "coverage": f"{covered / total * 100:.1f}%" if total else "0%",
        }
    except Exception:  # noqa: BLE001
        return None


def fmt_eta(seconds: float | None) -> str:
    if seconds is None:
        return "估算中…"
    if seconds <= 0:
        return "本批即将完成"
    if seconds < 90:
        return f"约 {seconds:.0f} 秒"
    return f"约 {seconds / 60:.1f} 分钟"


def _bar(done: int, total: int, width: int = 30) -> str:
    filled = int(width * done / total) if total else 0
    return "█" * filled + "░" * (width - filled)


def _show_backtest(info: dict, live: bool) -> None:
    if not info["started"]:
        print("  未找到回测日志记录。")
        return
    rng = f"{info['range'][0]} ~ {info['range'][1]}" if info["range"] else "?"
    print(f"  回测区间：{rng}　共 {info['total_days']} 个交易日")
    if info["finished"]:
        # 非实时时不画进度条，避免静态进度条被误读成「卡住了」
        print(f"  状态：已完成　信号 {info['signals']} 条"
              f"　覆盖 {info['cover_days']} 个交易日　耗时 {info['elapsed_min']} 分钟")
        errs = info["strategy_errors"]
        if errs:
            print("  ⚠ 该次回测有策略异常（对应信号已丢失）："
                  + "、".join(f"{k}×{v}" for k, v in errs.items()))
        return
    print(f"  状态：{'进行中' if live else '已中断'}"
          f"　已用 {info['elapsed_min']} 分钟　预计剩余 {info['eta_min']} 分钟")
    print(f"  进度：[{_bar(info['done_days'], info['total_days'])}] "
          f"{info['done_days']}/{info['total_days']} ({info['pct']}%)")
    print(f"  累计信号：{info['signals']} 条")
    errs = info["strategy_errors"]
    if errs:
        print("  ⚠ 有策略异常（对应信号已丢失）："
              + "、".join(f"{k}×{v}" for k, v in errs.items()))


def _show_backfill(info: dict, live: bool) -> None:
    if not info["batch"] and not info["last_progress"]:
        print("  未找到回填日志记录。")
        return

    # 非实时（历史记录）时**不画进度条**：静态的进度条会被误读成「任务卡住了」。
    if not live:
        stage = "已完成" if info["finished"] else (
            "已中止（数据源限流）" if info["aborted"] else "已停止"
        )
        # 用回填自己的进度点时间，不能用 last_line_at ——
        # 后者是「日志文件最后一行」的时间，daily 等其它命令也会写同一个日志，
        # 拿它当回填的结束时间会出现「第 9 批 + 今天的日期」这种自相矛盾的组合。
        when = info.get("last_progress_at") or info.get("last_line_at")
        when_txt = when.strftime("%Y-%m-%d %H:%M") if hasattr(when, "strftime") else (
            when or "未知时间"
        )
        print(f"  最后一批：第 {info['batch']} 批　状态：{stage}　"
              f"该批最后进度：{when_txt}")
        print(f"  最近累计入库：{info['rows_so_far']} 行　失败：{info['failed_so_far']} 只")
        return

    stage = "进行中"
    print(f"  状态：{stage}")
    if info["batch"]:
        print(f"  批次：第 {info['batch']} 批"
              f"（本批 {info['batch_size'] or '?'} 只，已完成 {info['done_in_batch']} 只）")
    if info["last_progress"]:
        done, total = info["last_progress"]
        pct = done / total * 100 if total else 0
        print(f"  本批进度：[{_bar(done, total)}] {done}/{total} ({pct:.0f}%)")
    print(f"  已入库：{info['rows_so_far']} 行　失败：{info['failed_so_far']} 只")
    if info["batch_failing"]:
        print("  ⚠ 本批全部失败（0 行入库），疑似数据源限流。")
        print("    处理：等待 5~10 分钟后重跑 scripts\\run_backfill.bat（自动续传）")
    else:
        print(f"  本批剩余时间：{fmt_eta(info['eta_sec'])}")
        if info["total_eta_sec"] is not None:
            print(f"  全部剩余：约 {info['total_eta_sec'] / 60:.0f} 分钟"
                  f"（待采集 {info['remaining']} 只）")
        elif info["remaining"]:
            print(f"  全部剩余：待采集 {info['remaining']} 只（暂无法估算时间）")


def main() -> int:
    forced = sys.argv[1] if len(sys.argv) > 1 else None

    # 回填日志从「最近一次运行起点」截断，避免多次运行的数据混在一起
    lines, source = load_lines("回填区间")
    bt_lines, bt_source = load_lines("开始回放：")
    if not bt_lines:
        bt_lines = lines
    # 涨停池采集同样按最近一次运行起点截断
    lp_lines, _ = load_lines("执行命令：limit-pool")
    dt_lines, _ = load_lines("执行命令：dragon-tiger")
    fin_lines, _ = load_lines("执行命令：finance")

    backfill = parse_backfill(lines)
    backtest = parse_backtest(bt_lines)
    limit_pool = parse_limit_pool(lp_lines)
    dragon = parse_dragon_tiger(dt_lines)
    finance = parse_finance(fin_lines)

    procs, running, write_pids = detect_tasks()
    if forced:
        active = forced
    elif running:
        # 写库任务优先展示（它才是用户最关心的那个）
        active = next((t for t in running if t in WRITE_TASKS), running[0])
    else:
        active = None

    print("=" * 64)
    print(f"  astock 任务监控　当前时间 {datetime.now().strftime('%H:%M:%S')}")
    print(f"  日志：{source}")
    print("=" * 64)

    # ---- 失败告警（最高优先级，先于一切）----
    # 定时任务失败时 run_daily.bat 会留下这个标记。放在最顶部是因为
    # 「昨天没跑成功」比任何进度信息都重要，埋在输出中间就会被忽略。
    fail_mark = LOGS / "LAST_DAILY_FAILED.txt"
    if fail_mark.exists():
        print("  ✖ 最近一次每日流程【执行失败】")
        try:
            for line in fail_mark.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.strip():
                    print(f"    {line}")
        except OSError:
            pass
        print("    处理：确认数据源可用后重跑 scripts\\run_daily.bat")
        print("-" * 64)

    # ---- 当前任务 ----
    if active:
        label = TASK_LABEL.get(active, active)
        others = [t for t in running if t != active]
        print(f"  当前任务：{label}")
        if others:
            print(f"  同时运行：{'、'.join(TASK_LABEL.get(t, t) for t in others)}")
    else:
        print("  当前任务：无（本项目没有正在运行的写库任务）")
    print("-" * 64)

    # ---- 进度 ----
    if active == "backtest" or forced == "backtest":
        _show_backtest(backtest, live=("backtest" in running))
    elif active == "backfill" or forced == "backfill":
        _show_backfill(backfill, live=("backfill" in running))
    elif active == "limit-pool" or forced == "limit-pool":
        _show_limit_pool(limit_pool, live=("limit-pool" in running))
    elif active == "dragon-tiger" or forced == "dragon-tiger":
        _show_dragon_tiger(dragon, live=("dragon-tiger" in running))
    elif active == "finance" or forced == "finance":
        _show_finance(finance, live=("finance" in running))
    else:
        # 没有写库任务时，给出各项的最近一次记录，明确标注为历史
        print("  【最近一次记录】（非实时，仅供参考）")
        if limit_pool["total"] or limit_pool["finished"]:
            _show_limit_pool(limit_pool, live=False)
        if dragon["total"]:
            print()
            _show_dragon_tiger(dragon, live=False)
        if backtest["started"]:
            print()
            _show_backtest(backtest, live=False)
        if backfill["batch"]:
            print()
            _show_backfill(backfill, live=False)

    # ---- 数据库 ----
    print("-" * 64)
    counts = try_db_counts()
    if counts:
        print(f"  数据库覆盖率：{counts['coverage']}"
              f"（{counts['covered']}/{counts['total']} 只，{counts['bars']} 根日线）")
        print("  主库当前空闲，可执行完整状态查询：python -m astock.cli status")
    elif write_pids:
        print(f"  主库被写任务占用（PID {', '.join(map(str, write_pids))}），无法查库")
        print("  想立刻看状态：python -m astock.cli status --snapshot")
    else:
        print("  主库无法读取（可能被其它进程占用）")

    # ---- 进程 ----
    print("-" * 64)
    print(f"  本项目相关进程：{len(procs)} 个")
    for line in procs[:8]:
        print(line)

    # ---- 结论 ----
    print("-" * 64)
    if active and active in WRITE_TASKS:
        if active == "backfill":
            eta = ""
            if backfill.get("total_eta_sec"):
                eta = f"，预计还需约 {backfill['total_eta_sec'] / 60:.0f} 分钟"
            print(f"  ✔ 历史数据回填正在运行{eta}")
        elif active == "backtest":
            if backtest["eta_min"] is not None:
                print(f"  ✔ 策略回测正在运行，预计还需 {backtest['eta_min']} 分钟")
            else:
                print("  ✔ 策略回测正在运行（刚启动，尚未产出第一个进度点）")
        elif active == "limit-pool":
            if limit_pool["finished"]:
                print("  ✔ 涨停池采集已完成（进程即将退出）")
            elif limit_pool["eta_min"] is not None:
                print(f"  ✔ 涨停池采集正在运行，预计还需约 {limit_pool['eta_min']:.0f} 分钟"
                      "（限流 15 次/分钟，速度固定，无法加快）")
            else:
                print("  ✔ 涨停池采集正在运行（刚启动，尚未产出第一个进度点）")
        elif active == "dragon-tiger":
            if dragon["eta_min"] is not None:
                print(f"  ✔ 龙虎榜采集正在运行，预计还需约 {dragon['eta_min']:.0f} 分钟"
                      "（限流 15 次/分钟，速度固定，无法加快）")
            else:
                print("  ✔ 龙虎榜采集正在运行（刚启动，尚未产出第一个进度点）")
        elif active == "finance":
            if finance["finished"]:
                print("  ✔ 财务采集已完成（进程即将退出）")
            elif finance["eta_min"] is not None:
                print(f"  ✔ 财务采集正在运行，预计还需约 {finance['eta_min'] / 60:.1f} 小时"
                      "（全市场回填，可随时中断后重跑续传）")
            else:
                print("  ✔ 财务采集正在运行（刚启动，每 50 只记录一次进度）")
        else:
            print(f"  ✔ {TASK_LABEL.get(active, active)} 正在运行")
        print("    结束前会一直是这个状态，属正常；稍后再执行本命令可刷新进度。")
    else:
        # 没有写库任务时，明确告诉用户「已经没有东西在跑了」，
        # 否则反复执行本命令会看到一模一样的输出，被误以为卡死。
        print("  ✔ 没有运行中的写库任务 —— 上面显示的都是历史记录，不是实时进度。")
        if "serve" in running:
            print("    本地 Web 服务在运行（只读快照，不会阻塞 daily 等写库命令）。")
        print("    下一步可选：")
        print("      python -m astock.cli shadow status  # 看影子信号成绩（v1.0 决策依据）")
        print("      python -m astock.cli daily          # 跑一次当日选股")

    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
