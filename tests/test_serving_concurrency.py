# -*- coding: utf-8 -*-
"""展示层快照读取器的并发回归测试。

背景（真实故障）：
FastAPI 会把同步接口丢进线程池，因此浏览器一次刷新就会并发打到多个接口。
早期 `ServingReader` 用「一个连接 + 一个跨线程共享的 _views 集合」，导致：

  1. 两个线程同时看到 `_con is None`，各建一个内存连接，后建的覆盖前一个；
  2. 线程 A 在连接 1 上建好视图并写入共享的 `_views`；
  3. 线程 B 看到 `_views` 已含该表 → 跳过建视图 → 但它的连接是连接 2；
  4. 报错：Catalog Error: Table with name xxx does not exist

本脚本用多线程并发查询多张快照表来复现该场景，验证修复后不再出错。

用法：
    python tests/test_serving_concurrency.py   # 或 scripts\\run_tests.bat 一键全跑
"""

from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from astock.storage.serving import get_reader, serving_dir  # noqa: E402

# 每个「表 -> 查询」都要覆盖到，才能真正触发懒加载视图的竞态
CASES = [
    ("market_regime", "SELECT * FROM market_regime ORDER BY date DESC LIMIT 1"),
    ("market_regime", "SELECT * FROM market_regime ORDER BY date DESC LIMIT 120"),
    ("shadow", "SELECT DISTINCT date FROM shadow ORDER BY date DESC LIMIT 1"),
    ("shadow_review", "SELECT * FROM shadow_review LIMIT 10"),
    ("dim_stock", "SELECT * FROM dim_stock LIMIT 10"),
    ("intraday", "SELECT * FROM intraday LIMIT 10"),
    ("stock_bars", "SELECT * FROM stock_bars ORDER BY date DESC LIMIT 50"),
    ("sector_strength", "SELECT * FROM sector_strength LIMIT 10"),
]


def main() -> int:
    reader = get_reader()
    if not reader.available():
        print("展示层快照不存在，请先执行：python -m astock.cli export")
        return 1

    print("=" * 66)
    print("  展示层并发读取回归测试")
    print(f"  快照目录：{serving_dir()}")
    print(f"  参与线程：{len(CASES)}　每轮并发请求：{len(CASES)}")
    print("=" * 66)

    errors: list[str] = []
    lock = threading.Lock()
    # 用事件制造「同时起跑」，最大化撞上竞态的概率
    start = threading.Event()

    def hit(case: tuple[str, str]) -> int:
        table, sql = case
        start.wait()
        try:
            df = reader.query(sql, table)
            return len(df)
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(f"[{table}] {type(exc).__name__}: {str(exc)[:120]}")
            return -1

    # 连续多轮：第一轮负责建视图（最容易出错），后续轮验证复用是否稳定
    rounds = 5
    for r in range(1, rounds + 1):
        start.clear()
        with ThreadPoolExecutor(max_workers=len(CASES)) as pool:
            futures = [pool.submit(hit, c) for c in CASES]
            start.set()
            results = [f.result() for f in futures]
        ok = sum(1 for x in results if x >= 0)
        print(f"  第 {r} 轮：成功 {ok}/{len(CASES)}　"
              f"返回行数 {[x for x in results]}")

    print("-" * 66)
    if errors:
        print(f"  ✖ 失败 {len(errors)} 次，说明并发仍不安全：")
        for e in errors[:10]:
            print(f"    {e}")
        return 1

    print("  ✔ 全部并发查询成功，读取器线程安全。")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
