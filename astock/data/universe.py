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

    def filtered_codes(
        self,
        as_of: date | None = None,
        verbose: bool = True,
        boards: list[str] | None = None,
    ) -> pd.DataFrame:
        """返回指定交易日（as_of）通过基础过滤的股票池。

        ⚠️ **适用范围：只用于「推荐选股」与「历史回测回放」**，
        不得用于市场分析 —— 市场状态 / 市场宽度 / 涨停家数 / 板块强度等
        市场级指标必须基于**全市场**（它们描述的是环境，不是可交易标的）。
        收窄到可交易子集会系统性低估情绪强度：
        实测 2026-09-23 全市场涨停 56 家，仅主板+创业板只有 51 家
        （差的 5 家来自科创板）。若误用本方法计算市场指标，会得到偏弱的状态判定。

        因此调用方只有两处：
          · astock/recommend/engine.py（选股）
          · astock/backtest/engine.py（回测）
        采集侧（collector）用的是 all_codes()，即全部板块。

        **严格按 as_of 取值**：回测时若使用「全局最新日期」会引入未来函数
        （例如用今天的成交额门槛去筛选两年前的股票池）。

        数据全部取自 `dws_feature` 的当日截面，因此既保证 as-of 正确，又比
        回扫 `dwd_daily_bar` 快很多（回测要按日循环数百次）。

        过滤规则（可配置）：
        - 排除 ST / *ST / 退市整理
        - 排除当时已退市（按 out_date 判断，而不是当前状态，避免幸存者偏差）
        - 排除 20 日均成交额低于阈值的僵尸股
        - 排除低价股、当日无成交（停牌）
        次新股：**不剔除**，只打 is_new 标记。
        """
        cfg = self.cfg
        as_of = (
            as_of
            or self.storage.latest_trade_date("dws_feature")
            or self.storage.latest_trade_date()
        )
        if as_of is None:
            return pd.DataFrame()

        # 板块白名单（**白名单而非黑名单**）：
        # 用白名单是因为「新增一个板块」比「漏掉一个不该交易的板块」后果轻得多 ——
        # 黑名单模式下，上游若新增分类（如新的北交所代码段），会被静默纳入选股，
        # 而用户可能根本开不了那个市场的账户。
        # 配置为 universe.boards，默认四类全收（保持历史行为不变）。
        # boards 参数用于单次覆盖（如回测想按全市场口径跑一遍做对比），
        # 不传则取配置值。
        boards = boards or [str(b) for b in (cfg.get("universe.boards")
                                             or ["main", "gem", "star", "bj"])]

        exclude_st = bool(cfg.get("universe.exclude_st", True))
        exclude_delisted = bool(cfg.get("universe.exclude_delisted", True))
        min_amount = float(cfg.get("universe.min_amount_avg20", 0) or 0)
        min_price = float(cfg.get("universe.min_price", 0) or 0)
        new_days = int(cfg.get("universe.new_stock_days", 120))

        sql = """
        SELECT
            f.code,
            s.name,
            s.board,
            s.exchange,
            s.ipo_date,
            s.out_date,
            COALESCE(f.is_st, FALSE)      AS is_st,
            f.close,
            f.volume,
            f.listed_days,
            COALESCE(f.amount_ma20, 0)    AS amount_avg20,
            COALESCE(f.turn, 0)           AS turn,
            (f.ma20 IS NOT NULL)          AS enough_bars
        FROM dws_feature f
        JOIN dim_stock s ON s.code = f.code
        WHERE f.date = ?
          AND s.board IN (%s)
        """ % ", ".join("?" for _ in boards)
        df = self.storage.query_df(sql, [as_of, *boards])
        if df.empty:
            return df

        before = len(df)
        # 次新股标记（listed_days 由因子层按 as_of 计算，天然无未来函数）
        listed = pd.to_numeric(df["listed_days"], errors="coerce")
        df["is_new"] = listed.fillna(99999) <= new_days

        if exclude_st:
            df = df[~df["is_st"].fillna(False).astype(bool)]
        if exclude_delisted:
            out_date = pd.to_datetime(df["out_date"], errors="coerce")
            df = df[out_date.isna() | (out_date > pd.Timestamp(as_of))]
        if min_amount > 0:
            df = df[df["amount_avg20"] >= min_amount]
        if min_price > 0:
            df = df[df["close"] >= min_price]
        df = df[df["volume"].fillna(0) > 0]

        if verbose:
            # 必须把板块白名单打进日志：否则"股票池为什么变小了"无从解释，
            # 而这是一个会改变所有选股结果与历史回测口径的开关。
            # 文案必须写明"仅用于推荐/回测"：否则看到"股票池 4593 → 3606 只"
            # 会误以为市场分析也被收窄了（真实发生过这次误解）。
            logger.info(
                "[%s] 选股池过滤（仅用于推荐/回测，市场分析仍用全市场）："
                "%d → %d 只（板块 %s，保留次新 %d 只）",
                as_of, before, len(df), "/".join(boards),
                int(df["is_new"].sum()) if not df.empty else 0,
            )
        return df.reset_index(drop=True)

    @staticmethod
    def limit_ratio(board: str, is_st: bool = False) -> float:
        """返回该标的的涨跌幅限制比例。"""
        if is_st:
            return ST_LIMIT_RATIO
        return LIMIT_RATIO.get(board, 0.10)
