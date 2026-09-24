# -*- coding: utf-8 -*-
"""板块强度：由本地日线自行计算，可回填历史。

**为什么不用接口的板块行情**：
免费板块接口（东财/申万/同花顺的快照接口）只给**当日**数据，没有历史。
而「板块效应是否真的有效」是个必须用历史验证的问题 —— 没有历史就无法回测，
无法回测的参数调整就是拍脑袋。本地自算还顺带解决了接口不稳定和覆盖率的问题。

强度分口径（横截面百分位，同一天内各板块可比，0~100）：

    0.5 × 板块平均涨跌幅排名
  + 0.3 × 板块内涨停家数排名
  + 0.2 × 板块成交额排名

涨停判定按板块差异化阈值：ST 4.8%、创业板/科创板 19.5%、其余 9.8%，
避免把 20% 涨停的科创板股票错误计入或漏计。
"""

from __future__ import annotations

from datetime import date, timedelta

from astock.config import get_config
from astock.logger import get_logger
from astock.storage.db import Storage, get_storage

logger = get_logger("features.sector")

STRENGTH_COLUMNS = [
    "date", "source", "industry_name", "member_count", "up_count", "down_count",
    "limit_up_count", "avg_pct_chg", "median_pct_chg", "total_amount",
    "amount_ratio", "strength_score",
]

# 关键设计：**不把股票的标签二选一**，而是按来源分别聚合。
# 一只股票若同时有申万与新浪标签，就同时贡献给两套体系各自的板块。
# 否则被选中的那一套会把另一套的分类拆散，导致其板块成分数严重失真。
# 排名也只在**同一 (date, source) 内**做，保证强度分可比。
STRENGTH_SQL = """
WITH base AS (
    SELECT
        b.date,
        m.source,
        m.industry_name,
        b.pct_chg,
        b.amount,
        CASE
            WHEN COALESCE(s.is_st, FALSE)   THEN 4.8
            WHEN s.board IN ('gem', 'star') THEN 19.5
            ELSE 9.8
        END AS limit_th
    FROM dwd_daily_bar b
    JOIN dim_stock_industry m ON m.code = b.code
    LEFT JOIN dim_stock s     ON s.code = b.code
    WHERE b.date <= ?
),
daily AS (
    SELECT
        date,
        source,
        industry_name,
        COUNT(*)                                             AS member_count,
        SUM(CASE WHEN pct_chg > 0 THEN 1 ELSE 0 END)         AS up_count,
        SUM(CASE WHEN pct_chg < 0 THEN 1 ELSE 0 END)         AS down_count,
        SUM(CASE WHEN pct_chg >= limit_th THEN 1 ELSE 0 END) AS limit_up_count,
        AVG(pct_chg)                                         AS avg_pct_chg,
        MEDIAN(pct_chg)                                      AS median_pct_chg,
        SUM(amount)                                          AS total_amount
    FROM base
    GROUP BY 1, 2, 3
),
rolled AS (
    SELECT
        *,
        AVG(total_amount) OVER (
            PARTITION BY source, industry_name ORDER BY date
            ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING
        ) AS amount_ma20
    FROM daily
),
scored AS (
    SELECT
        date, source, industry_name, member_count, up_count, down_count,
        limit_up_count, avg_pct_chg, median_pct_chg, total_amount,
        CASE WHEN amount_ma20 > 0 THEN total_amount / amount_ma20 END AS amount_ratio,
        (
            0.5 * PERCENT_RANK() OVER (PARTITION BY date, source ORDER BY avg_pct_chg)
          + 0.3 * PERCENT_RANK() OVER (PARTITION BY date, source ORDER BY limit_up_count)
          + 0.2 * PERCENT_RANK() OVER (PARTITION BY date, source ORDER BY total_amount)
        ) * 100 AS strength_score
    FROM rolled
)
SELECT {cols} FROM scored WHERE date BETWEEN ? AND ?
"""


class SectorStrengthBuilder:
    """板块强度构建器。"""

    def __init__(self, storage: Storage | None = None) -> None:
        self.storage = storage or get_storage()
        self.cfg = get_config()

    def build(self, start: date | None = None, end: date | None = None) -> int:
        """增量计算板块强度。

        默认从「已有结果的最新日期前推 35 天」开始重算：
        `amount_ratio` 依赖 20 日均量，而均量会因补数/复权变化，
        尾部必须重算才能保持一致。写入用 INSERT OR REPLACE，重复跑不会脏。
        """
        latest = self.storage.latest_trade_date("dwd_daily_bar")
        if latest is None:
            logger.warning("行情表为空，请先执行 backfill")
            return 0

        mapped = int(
            self.storage.query_value(
                "SELECT COUNT(*) FROM dim_stock_industry", default=0
            ) or 0
        )
        if mapped == 0:
            logger.warning("行业映射为空，请先执行：python -m astock.cli sector")
            return 0

        end = end or latest
        if start is None:
            last = self.storage.latest_trade_date("dws_sector_strength")
            start = (last - timedelta(days=35)) if last else date(2000, 1, 1)

        cols = ", ".join(STRENGTH_COLUMNS)
        sql = f"INSERT OR REPLACE INTO dws_sector_strength ({cols}) " + STRENGTH_SQL.format(
            cols=cols
        )
        # 三个占位符：base 的历史上界、最终写入区间
        self.storage.conn.execute(sql, [end, start, end])

        # 报实际写入的区间而非请求区间：全量重建时请求 `2000-01-01`，
        # 但库里最早只有 2024-09 的行情，照请求区间打印会误导。
        row = self.storage.query_one(
            "SELECT MIN(date), MAX(date), COUNT(*) FROM dws_sector_strength "
            "WHERE date BETWEEN ? AND ?",
            [start, end],
        )
        n = int(row[2] or 0) if row else 0
        if row and row[0] is not None:
            logger.info("板块强度更新完成：%s ~ %s，共 %d 行", row[0], row[1], n)
        else:
            logger.info("板块强度更新完成：本次无数据写入（区间 %s ~ %s）", start, end)
        return n

    def rebuild_all(self) -> int:
        """全量重建（口径变更后使用）。"""
        self.storage.execute("DELETE FROM dws_sector_strength")
        return self.build(start=date(2000, 1, 1))

    def latest_date(self) -> date | None:
        return self.storage.latest_trade_date("dws_sector_strength")

    # ---------------- 查询 ----------------
    def primary_source(self) -> str:
        """主用分类体系（报告与市场状态都用它）。"""
        return str(self.cfg.get("sector.primary_source", "sw") or "sw")

    def top(self, target: date | None = None, n: int = 10, source: str | None = None) -> list[dict]:
        """当日强度前 N 的板块（默认只用主用分类体系）。"""
        target = target or self.latest_date()
        if target is None:
            return []
        src = source or self.primary_source()
        df = self.storage.query_df(
            "SELECT industry_name, strength_score, avg_pct_chg, limit_up_count, "
            "member_count, amount_ratio FROM dws_sector_strength "
            "WHERE date = ? AND source = ? ORDER BY strength_score DESC LIMIT ?",
            [target, src, n],
        )
        return [] if df.empty else df.to_dict(orient="records")

    def of(self, industry: str, target: date | None = None, source: str | None = None) -> dict | None:
        """某个板块在某日的强度。"""
        target = target or self.latest_date()
        if target is None:
            return None
        df = self.storage.query_df(
            "SELECT * FROM dws_sector_strength "
            "WHERE date = ? AND source = ? AND industry_name = ?",
            [target, source or self.primary_source(), industry],
        )
        return None if df.empty else df.iloc[0].to_dict()

    def top_sector(self, target: date, source: str | None = None) -> tuple[str | None, float | None]:
        """当日最强板块及其涨停占比。

        `top_sector_share` = 该板块涨停家数 / 该体系内全市场涨停家数，
        用于判断是否出现「单一板块虹吸」的事件驱动行情（阈值见配置）。
        """
        src = source or self.primary_source()
        df = self.storage.query_df(
            "SELECT industry_name, limit_up_count FROM dws_sector_strength "
            "WHERE date = ? AND source = ? ORDER BY strength_score DESC LIMIT 1",
            [target, src],
        )
        if df.empty:
            return None, None
        name = str(df.iloc[0]["industry_name"])
        sector_lu = int(df.iloc[0]["limit_up_count"] or 0)
        total_lu = int(
            self.storage.query_value(
                "SELECT SUM(limit_up_count) FROM dws_sector_strength "
                "WHERE date = ? AND source = ?",
                [target, src],
                default=0,
            )
            or 0
        )
        share = round(sector_lu / total_lu * 100, 1) if total_lu > 0 else None
        return name, share
