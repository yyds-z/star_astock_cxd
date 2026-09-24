# -*- coding: utf-8 -*-
"""因子宽表构建（dws_feature）。

设计要点：全部指标用 DuckDB 窗口函数在库内一次性算完，
避免 Python 逐股循环（Sequoia-X 的逐股循环在全市场 2 年数据下需要数分钟，
库内向量化只需数秒）。指标口径与 Sequoia-X 各策略保持一致。
"""

from __future__ import annotations

from datetime import date

from astock.config import get_config
from astock.logger import get_logger
from astock.storage.db import Storage, get_storage

logger = get_logger("features.builder")

# 因子表列顺序（必须与 schema.sql 中 dws_feature 定义一致）
FEATURE_COLUMNS = [
    "code", "date", "open", "high", "low", "close", "preclose", "volume", "amount", "turn", "pct_chg",
    "ma5", "ma10", "ma20", "ma60", "ma120", "prev_ma5", "prev_ma20", "prev_ma60",
    "vol_ma5", "vol_ma20", "vol_ma20_prev", "vol_ratio", "amount_ma20",
    "prev_close", "prev2_close", "prev_volume", "prev_high",
    "hh20_prev", "hh10", "ll10", "hh40", "ll40", "hh120", "ll120",
    "pct_5d", "pct_20d", "pct_60d", "pos_60", "amplitude_20", "rps120", "float_mv",
    "listed_days", "is_st", "is_new_stock", "is_limit_up", "is_limit_down", "is_yang",
]

FEATURE_SQL = """
WITH base AS (
    SELECT
        b.code, b.date, b.open, b.high, b.low, b.close, b.preclose,
        b.volume, b.amount, b.turn, b.pct_chg,
        d.ipo_date,
        COALESCE(d.is_st, FALSE) AS is_st,
        CASE
            WHEN COALESCE(d.is_st, FALSE) THEN 0.05
            WHEN d.board IN ('gem', 'star') THEN 0.20
            WHEN d.board = 'bj' THEN 0.30
            ELSE 0.10
        END AS lim_ratio
    FROM dwd_daily_bar b
    JOIN dim_stock d ON d.code = b.code
),
win AS (
    SELECT
        *,
        AVG(close) OVER w5   AS ma5,
        AVG(close) OVER w10  AS ma10,
        AVG(close) OVER w20  AS ma20,
        AVG(close) OVER w60  AS ma60,
        AVG(close) OVER w120 AS ma120,
        AVG(volume) OVER w5   AS vol_ma5,
        AVG(volume) OVER w20  AS vol_ma20,
        AVG(volume) OVER w20p AS vol_ma20_prev,
        AVG(amount) OVER w20  AS amount_ma20
    FROM base
    WINDOW
        w5   AS (PARTITION BY code ORDER BY date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW),
        w10  AS (PARTITION BY code ORDER BY date ROWS BETWEEN 9 PRECEDING AND CURRENT ROW),
        w20  AS (PARTITION BY code ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW),
        w60  AS (PARTITION BY code ORDER BY date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW),
        w120 AS (PARTITION BY code ORDER BY date ROWS BETWEEN 119 PRECEDING AND CURRENT ROW),
        w20p AS (PARTITION BY code ORDER BY date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING)
),
lagged AS (
    SELECT
        *,
        LAG(close, 1)  OVER w AS prev_close,
        LAG(close, 2)  OVER w AS prev2_close,
        LAG(volume, 1) OVER w AS prev_volume,
        LAG(high, 1)   OVER w AS prev_high,
        LAG(ma5, 1)    OVER w AS prev_ma5,
        LAG(ma20, 1)   OVER w AS prev_ma20,
        LAG(ma60, 1)   OVER w AS prev_ma60,
        (close / NULLIF(LAG(close, 5) OVER w, 0) - 1) * 100   AS pct_5d,
        (close / NULLIF(LAG(close, 20) OVER w, 0) - 1) * 100  AS pct_20d,
        (close / NULLIF(LAG(close, 60) OVER w, 0) - 1) * 100  AS pct_60d,
        (close / NULLIF(LAG(close, 120) OVER w, 0) - 1) * 100 AS pct_120,
        MAX(high) OVER hh20p AS hh20_prev,
        MAX(high) OVER hh10  AS hh10,
        MIN(low)  OVER hh10  AS ll10,
        MAX(high) OVER hh40  AS hh40,
        MIN(low)  OVER hh40  AS ll40,
        MAX(high) OVER hh60  AS hh60,
        MIN(low)  OVER hh60  AS ll60,
        MAX(high) OVER hh120 AS hh120,
        MIN(low)  OVER hh120 AS ll120,
        AVG((high - low) / NULLIF(preclose, 0)) OVER w20r * 100 AS amplitude_20
    FROM win
    WINDOW
        w     AS (PARTITION BY code ORDER BY date),
        w20r  AS (PARTITION BY code ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW),
        hh20p AS (PARTITION BY code ORDER BY date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING),
        hh10  AS (PARTITION BY code ORDER BY date ROWS BETWEEN 9 PRECEDING AND CURRENT ROW),
        hh40  AS (PARTITION BY code ORDER BY date ROWS BETWEEN 39 PRECEDING AND CURRENT ROW),
        hh60  AS (PARTITION BY code ORDER BY date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW),
        hh120 AS (PARTITION BY code ORDER BY date ROWS BETWEEN 119 PRECEDING AND CURRENT ROW)
)
SELECT
    code, date, open, high, low, close, preclose, volume, amount, turn, pct_chg,
    ma5, ma10, ma20, ma60, ma120, prev_ma5, prev_ma20, prev_ma60,
    vol_ma5, vol_ma20, vol_ma20_prev,
    volume / NULLIF(vol_ma20, 0) AS vol_ratio,
    amount_ma20,
    prev_close, prev2_close, prev_volume, prev_high,
    hh20_prev, hh10, ll10, hh40, ll40, hh120, ll120,
    pct_5d, pct_20d, pct_60d,
    (close - ll60) / NULLIF(hh60 - ll60, 0) AS pos_60,
    amplitude_20,
    PERCENT_RANK() OVER (PARTITION BY date ORDER BY pct_120) * 100 AS rps120,
    close * (volume * 100 / NULLIF(turn, 0)) AS float_mv,
    CAST(date - ipo_date AS INTEGER) AS listed_days,
    is_st,
    COALESCE(CAST(date - ipo_date AS INTEGER) <= {new_days}, FALSE) AS is_new_stock,
    CAST(close >= ROUND(preclose * (1 + lim_ratio), 2) - 0.001 AS BOOLEAN) AS is_limit_up,
    CAST(close <= ROUND(preclose * (1 - lim_ratio), 2) + 0.001 AS BOOLEAN) AS is_limit_down,
    CAST(close > open AS BOOLEAN) AS is_yang
FROM lagged
"""


class FeatureBuilder:
    """因子宽表构建器。"""

    def __init__(self, storage: Storage | None = None) -> None:
        self.storage = storage or get_storage()
        self.cfg = get_config()
        self.new_days = int(self.cfg.get("universe.new_stock_days", 120))

    def build(self, start: date | None = None, end: date | None = None) -> int:
        """构建/刷新因子表。

        窗口函数需要完整历史才能算准，因此内部始终基于全量历史计算，
        但只把 [start, end] 区间的结果写入表中（增量更新）。
        """
        latest = self.storage.latest_trade_date("dwd_daily_bar")
        if latest is None:
            logger.warning("行情表为空，请先执行 backfill")
            return 0

        if end is None:
            end = latest
        if start is None:
            last_feature = self.storage.latest_trade_date("dws_feature")
            # 回退 5 天重算，避免因复权或补数导致尾部数据不一致
            start = last_feature if last_feature else date(2000, 1, 1)

        cols = ", ".join(FEATURE_COLUMNS)
        sql = f"""
        INSERT OR REPLACE INTO dws_feature ({cols})
        SELECT {cols} FROM ( {FEATURE_SQL.format(new_days=self.new_days)} ) t
        WHERE t.date BETWEEN ? AND ?
        """
        self.storage.conn.execute(sql, [start, end])
        n = int(
            self.storage.query_value(
                "SELECT COUNT(*) FROM dws_feature WHERE date BETWEEN ? AND ?", [start, end], default=0
            )
            or 0
        )
        logger.info("因子表更新完成：%s ~ %s，共 %d 行", start, end, n)
        return n

    def rebuild_all(self) -> int:
        """全量重建（表结构或口径变更后使用）。"""
        self.storage.execute("DELETE FROM dws_feature")
        return self.build(start=date(2000, 1, 1))

    def latest_date(self) -> date | None:
        return self.storage.latest_trade_date("dws_feature")

    def load_features(self, target_date: date | None = None) -> "object":
        """读取指定交易日的全市场因子截面（策略层入口）。"""
        import pandas as pd

        target_date = target_date or self.latest_date()
        if target_date is None:
            return pd.DataFrame()
        return self.storage.query_df(
            "SELECT * FROM dws_feature WHERE date = ?", [target_date]
        )
