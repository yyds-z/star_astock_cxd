# -*- coding: utf-8 -*-
"""数据源连通性诊断：验证 baostock / akshare / adata 是否可用。

用法（在项目根目录执行）：
    python scripts/probe_source.py            # 全部探测
    python scripts/probe_source.py baostock   # 只探测指定源
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from astock.config import get_config  # noqa: E402
from astock.logger import get_logger, setup_logging  # noqa: E402


def probe_baostock() -> None:
    from astock.data.sources.baostock_source import BaostockSource

    src = BaostockSource()
    with src:
        stocks = src.list_stocks()
        print(f"[baostock] 股票列表: {len(stocks)} 只")
        if not stocks.empty:
            print(stocks.head(3).to_string(index=False))

        end = date.today()
        start = end - timedelta(days=30)
        df = src.fetch_daily("600519", start, end)
        print(f"[baostock] 600519 近30天日线: {len(df)} 行")
        if not df.empty:
            print(df.tail(3).to_string(index=False))

        cal = src.list_trade_dates(end - timedelta(days=10), end)
        print(f"[baostock] 交易日历(近10天): {len(cal)} 行")

        idx = src.fetch_index_daily("sh.000001", start, end)
        print(f"[baostock] 上证指数: {len(idx)} 行")


def probe_adata() -> None:
    from astock.data.sources.adata_source import AdataSource

    src = AdataSource()
    with src:
        stocks = src.list_stocks()
        print(f"[adata] 股票列表: {len(stocks)} 只")
        df = src.fetch_daily("600519", date.today() - timedelta(days=30), date.today())
        print(f"[adata] 600519 日线: {len(df)} 行")
        if not df.empty:
            print(df.tail(2).to_string(index=False))


def probe_akshare() -> None:
    from astock.data.sources.akshare_source import AkshareSource

    src = AkshareSource()
    with src:
        cal = src.list_trade_dates(date.today() - timedelta(days=10), date.today())
        print(f"[akshare] 交易日历: {len(cal)} 行")
        df = src.fetch_daily("600519", date.today() - timedelta(days=30), date.today())
        print(f"[akshare] 600519 日线: {len(df)} 行")


def main() -> int:
    cfg = get_config()
    setup_logging(cfg.log_dir)

    targets = sys.argv[1:] or ["baostock", "adata", "akshare"]
    for name in targets:
        print(f"\n===== 探测 {name} =====")
        try:
            {"baostock": probe_baostock, "adata": probe_adata, "akshare": probe_akshare}[name]()
        except Exception as exc:  # noqa: BLE001
            print(f"[{name}] 探测失败: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    _ = get_logger
    sys.exit(main())
