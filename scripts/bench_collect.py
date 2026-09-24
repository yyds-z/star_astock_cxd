# -*- coding: utf-8 -*-
"""采集性能基准测试：估算全市场回填所需时间，用于选择并发数。

用法：
    python scripts/bench_collect.py            # 默认测 20 只
    python scripts/bench_collect.py 50 4       # 测 50 只，4 进程
"""

from __future__ import annotations

import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from astock.config import get_config  # noqa: E402
from astock.data.sources.baostock_source import BaostockSource  # noqa: E402

SAMPLE = [
    "601398", "601288", "601988", "601939", "601857", "600028", "601088", "601628",
    "601336", "601601", "000568", "000596", "002714", "002027", "300124", "300760",
    "688111", "688036", "688981", "688012", "600585", "600031", "000725", "002304",
    "600809", "603259", "601899", "600438", "002460", "300274",
]


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    years = 2
    cfg = get_config()
    codes = SAMPLE[:n]

    end = date.today()
    start = end - timedelta(days=int(365.25 * years))

    src = BaostockSource(adjust_flag=str(cfg.get("data.adjust_flag", "2")))
    src.open()
    ok, rows = 0, 0
    try:
        t0 = time.perf_counter()
        per_stock: list[float] = []
        for code in codes:
            t = time.perf_counter()
            df = src.fetch_daily(code, start, end)
            cost = time.perf_counter() - t
            per_stock.append(cost)
            if not df.empty:
                ok += 1
                rows += len(df)
        total = time.perf_counter() - t0
    finally:
        src.close()

    per_stock.sort()
    avg = total / len(codes)
    p95 = per_stock[int(len(per_stock) * 0.95) - 1]

    print(f"\n样本 {len(codes)} 只 | 成功 {ok} 只 | 入库 {rows} 行 | 区间 {start} ~ {end}")
    print(f"总耗时 {total:.1f}s | 平均 {avg:.2f}s/只 | 中位 {per_stock[len(per_stock)//2]:.2f}s | P95 {p95:.2f}s")

    for workers in (1, 4, 8):
        est_min = 5500 * avg / workers / 60
        print(f"  并发 {workers} → 全市场 5500 只预计 {est_min:.0f} 分钟（{est_min/60:.1f} 小时）")

    print("\n提示：并行会加大数据源压力，建议 4~8；单进程最稳，适合放着慢慢跑。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
