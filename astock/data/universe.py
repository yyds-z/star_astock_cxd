# -*- coding: utf-8 -*-
"""股票池管理与基础过滤。

已确认的需求：全 A 股 + 基础风险过滤，**保留次新股**。
因此这里只做 ST / 退市 / 停牌 / 低流动性 过滤，次新股仅做标记不做剔除。
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from astock.config import get_config
from astock.logger import get_logger
from astock.storage.db import Storage, get_storage

logger = get_logger("data.universe")

# 板块涨跌幅限制（用于涨停/跌停判定）
LIMIT_RATIO = {
    "main": 0.10,
    "gem": 0.20,
    "star": 0.20,
    "bj": 0.30,
}
ST_LIMIT_RATIO = 0.05


class Universe:
    """股票池：从 dim_stock 出发，按配置过滤出可交易标的。"""

    def __init__(self, storage: Storage | None = None) -> None:
        self.storage = storage or get_storage()
        self.cfg = get_config()

    def all_codes(self) -> list[str]:
        df = self.storage.query_df("SELECT code FROM dim_stock ORDER BY code")
        return df["code"].tolist() if not df.empty else []
    # filtered_codes 已于 2026-10-08 删除：它按 `dws_feature` 当日截面过滤可交易池，
    # 而该因子宽表（254.5 万行）随主链路一并删除；其唯一调用方（推荐引擎与
    # 回测引擎）也已删除 —— 属于零调用方的死代码。采集侧只用 all_codes()，
    # 它只读 dim_stock（股票列表与退市日不受本次删除影响）。

    def limit_ratio(board: str, is_st: bool = False) -> float:
        """返回该标的的涨跌幅限制比例。"""
        if is_st:
            return ST_LIMIT_RATIO
        return LIMIT_RATIO.get(board, 0.10)
