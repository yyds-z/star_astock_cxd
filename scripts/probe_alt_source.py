# -*- coding: utf-8 -*-
"""数据源连通性诊断（测的是系统真正使用的适配器）。

为什么这样写：
早期版本直接用 akshare / adata 的原始接口做探测，结果是「诊断结论」与
「系统实际行为」脱节 —— 例如它测的是 `adata.StockMarketBaidu`（类名还拼错了，
实际叫 `StockMarketBaiDu`），而系统走的是 `AdataSource`，两者根本不是一个通道。

因此本脚本改为**通过数据源工厂取真实适配器**，逐个执行
`list_stocks / fetch_daily`，所见即系统所得。

用法：
    python scripts/probe_alt_source.py
    python scripts/probe_alt_source.py baostock      # 只测一个源
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from astock.config import get_config  # noqa: E402
from astock.data.sources import available_sources, get_source  # noqa: E402

get_config()  # 触发 NO_PROXY 设置等副作用

CODE = "600519"
START, END = "2026-08-01", "2026-09-22"


def probe(name: str) -> dict:
    """对单个数据源做端到端探测，返回结果字典。"""
    result = {"name": name, "open": False, "stocks": 0, "bars": 0, "error": ""}
    source = None
    t0 = time.perf_counter()
    try:
        source = get_source(name)
        source.open()
        result["open"] = True

        stocks = source.list_stocks()
        result["stocks"] = 0 if stocks is None else len(stocks)

        bars = source.fetch_daily(CODE, START, END)
        result["bars"] = 0 if bars is None else len(bars)
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {str(exc)[:110]}"
    finally:
        if source is not None:
            try:
                source.close()
            except Exception:  # noqa: BLE001
                pass
    result["cost"] = time.perf_counter() - t0
    return result


def main() -> int:
    only = sys.argv[1] if len(sys.argv) > 1 else None
    names = available_sources()
    if only:
        names = [n for n in names if n == only] or [only]

    print("=" * 70)
    print(f"  数据源探测（探测标的 {CODE}，区间 {START} ~ {END}）")
    print("  说明：直接调用系统实际使用的适配器，而非数据源原始接口")
    print("=" * 70)

    results = [probe(n) for n in names]

    print()
    print(f"  {'数据源':<12}{'登录':<6}{'股票列表':>9}{'日线':>7}{'耗时':>8}   结论")
    print("-" * 70)
    usable: list[str] = []
    for r in results:
        login = "OK" if r["open"] else "FAIL"
        verdict = "可用"
        if not r["open"]:
            verdict = "不可用（登录失败）"
        elif r["bars"] == 0:
            verdict = "部分可用（能取列表，日线为空）"
        elif r["stocks"] == 0:
            verdict = "部分可用（列表为空）"
        else:
            usable.append(r["name"])
        print(
            f"  {r['name']:<12}{login:<6}{r['stocks']:>9}{r['bars']:>7}"
            f"{r['cost']:>7.1f}s   {verdict}"
        )
        if r["error"]:
            print(f"       {r['error']}")

    print("-" * 70)
    if usable:
        print(f"  ✔ 可用于回填的通道：{'、'.join(usable)}")
        print(f"    例：python -m astock.cli backfill --source {usable[0]} --limit 300 --workers 2")
    else:
        print("  ✖ 当前没有能拉取日线的通道。")
        print("    若主源被限流，通常等待 10~30 分钟即可恢复；")
        print("    恢复后直接重跑 scripts\\run_backfill.bat（自动续传，不会漏数据）。")
    print("=" * 70)
    print()
    print("  提示：日线为空也可能是「该源在当前网络下被拦截」。")
    print("        受限通道（如东财系）在国内网络下常表现为能连上但返回空。")
    return 0 if usable else 1


if __name__ == "__main__":
    sys.exit(main())
