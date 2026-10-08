# -*- coding: utf-8 -*-
"""市场状态识别（模块 2）。

移植 market-breadth 的「宽度」思想：用「价格站上均线的股票占比」衡量市场内部强弱，
再叠加涨停生态、量能、指数动量，判定当前属于哪种市场环境，
并输出三档（短线/波段/价值）的权重，驱动策略选择与评分。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pandas as pd

from astock.config import get_config
from astock.features.indicators import to_float, to_int
from astock.logger import get_logger
from astock.storage.db import Storage, get_storage

logger = get_logger("market.regime")

STATE_LABEL = {
    "trend": "趋势行情",
    "euphoria": "情绪高潮",
    "recession": "情绪退潮",
    "range": "震荡行情",
    "event": "事件驱动",
    "unknown": "状态不明",
}

# 计算每日市场指标的 SQL（全部在库内向量化完成）
#
# 2026-10-08：**不再依赖因子宽表 dws_feature**（该表随主链路一并删除）。
# dws_feature 本来就完全由 `dwd_daily_bar + dim_stock` 派生，所以这里直接读日线，
# 并用**与原来逐字相同**的窗口定义现算 ma20/ma60：
#   w20 = ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
#   w60 = ROWS BETWEEN 59 PRECEDING AND CURRENT ROW
# 这样 breadth_ma20/ma60 与删除前逐位一致（已实测比对）。涨停/跌停的判定表达式
# 也与 FeatureBuilder 原来用的一致，故 up_count/limit_up_count 等同样不变。
METRIC_SQL = """
WITH f AS (
    SELECT
        t.*,
        CAST(close >= ROUND(preclose * (1 + lim_ratio), 2) - 0.001 AS BOOLEAN) AS is_limit_up,
        CAST(close <= ROUND(preclose * (1 - lim_ratio), 2) + 0.001 AS BOOLEAN) AS is_limit_down
    FROM (
        SELECT
            b.date,
            b.code,
            b.close,
            b.preclose,
            b.high,
            b.amount,
            b.pct_chg,
            AVG(b.close) OVER (
                PARTITION BY b.code ORDER BY b.date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
            ) AS ma20,
            AVG(b.close) OVER (
                PARTITION BY b.code ORDER BY b.date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW
            ) AS ma60,
            CASE
                WHEN COALESCE(d.is_st, FALSE) THEN 0.05
                WHEN d.board IN ('gem', 'star') THEN 0.20
                WHEN d.board = 'bj' THEN 0.30
                ELSE 0.10
            END AS lim_ratio
        FROM dwd_daily_bar b
        JOIN dim_stock d ON d.code = b.code
    ) t
),
daily AS (
    SELECT
        date,
        COUNT(*) FILTER (WHERE pct_chg > 0) AS up_count,
        COUNT(*) FILTER (WHERE pct_chg < 0) AS down_count,
        COUNT(*) FILTER (WHERE is_limit_up) AS limit_up_count,
        COUNT(*) FILTER (WHERE is_limit_down) AS limit_down_count,
        COUNT(*) FILTER (WHERE high >= ROUND(preclose * (1 + lim_ratio), 2) - 0.001) AS touch_count,
        SUM(amount) AS total_amount,
        AVG(CASE WHEN ma20 IS NOT NULL THEN CAST(close > ma20 AS INTEGER) END) * 100 AS breadth_ma20,
        AVG(CASE WHEN ma60 IS NOT NULL THEN CAST(close > ma60 AS INTEGER) END) * 100 AS breadth_ma60
    FROM f
    GROUP BY date
),
rolled AS (
    SELECT
        *,
        AVG(total_amount) OVER (ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS amount_ma20,
        LAG(limit_up_count, 1) OVER (ORDER BY date) AS prev_limit_up
    FROM daily
),
-- 上游涨停/炸板家数（同花顺）。**只用于交叉校验与可选切换**，不默认参与判定：
-- 上游仅覆盖最近一年，而回测区间是两年；若在同一个序列里混用两个口径，
-- 会在上游数据起始日产生系统性跳变（实测口径差约 7.5%），
-- 被误读成「市场状态切换」，进而悄悄改变档位权重 —— 且不报任何错。
up AS (
    SELECT
        u.date,
        u.n AS up_count,
        COALESCE(b.n, 0) AS break_count
    FROM (SELECT date, COUNT(*) AS n FROM dwd_limit_up GROUP BY date) u
    LEFT JOIN (SELECT date, COUNT(*) AS n FROM dwd_limit_break GROUP BY date) b
           ON b.date = u.date
),
idx AS (
    SELECT
        date,
        close AS index_close,
        (close / NULLIF(LAG(close, 5) OVER (ORDER BY date), 0) - 1) * 100  AS index_pct_5d,
        (close / NULLIF(LAG(close, 20) OVER (ORDER BY date), 0) - 1) * 100 AS index_pct_20d
    FROM dwd_index_bar
    WHERE code = 'sh.000001'
)
SELECT
    r.date,
    r.up_count,
    r.down_count,
    r.limit_up_count,
    r.limit_down_count,
    r.touch_count,
    r.prev_limit_up,
    r.total_amount,
    CASE WHEN r.amount_ma20 > 0 THEN r.total_amount / r.amount_ma20 ELSE NULL END AS amount_ratio,
    r.breadth_ma20,
    r.breadth_ma60,
    i.index_pct_5d,
    i.index_pct_20d,
    p.up_count AS up_limit_up,
    p.break_count AS up_break
FROM rolled r
LEFT JOIN idx i ON i.date = r.date
LEFT JOIN up p ON p.date = r.date
ORDER BY r.date
"""


class MarketRegime:
    """市场状态识别器。"""

    def __init__(self, storage: Storage | None = None) -> None:
        self.storage = storage or get_storage()
        self.cfg = get_config()

    # ---------------- 计算 ----------------
    def compute(self, start: date | None = None, end: date | None = None) -> int:
        """计算并落库市场状态。"""
        df = self.storage.query_df(METRIC_SQL)
        if df.empty:
            logger.warning("无因子数据，无法计算市场状态（请先执行 factor 构建）")
            return 0

        df["date"] = pd.to_datetime(df["date"]).dt.date
        if start is not None:
            df = df[df["date"] >= start]
        if end is not None:
            df = df[df["date"] <= end]
        if df.empty:
            return 0

        # 一次性预取每日最强板块，避免逐日查库（每日一次查询会拖慢数百倍）
        sectors = self._load_top_sectors(df["date"].min(), df["date"].max())
        rows = [self._classify(r, sectors.get(r["date"])) for _, r in df.iterrows()]
        out = pd.DataFrame(rows)
        n = self.storage.upsert_df(out, "dws_market_regime")
        logger.info("市场状态计算完成：%s ~ %s，共 %d 个交易日", df["date"].min(), df["date"].max(), n)
        return n

    def _load_top_sectors(self, start: date, end: date) -> dict[date, dict[str, Any]]:
        """取区间内每日最强板块及其涨停占比。

        板块强度缺失时返回空字典 —— 市场状态计算不应因为板块数据没跑而失败，
        `top_sector` 留空即可（前端与报告已做空值处理）。
        """
        source = str(self.cfg.get("sector.primary_source", "sw") or "sw")
        try:
            df = self.storage.query_df(
                """
                WITH ranked AS (
                    SELECT
                        date, industry_name, limit_up_count, strength_score,
                        ROW_NUMBER() OVER (PARTITION BY date ORDER BY strength_score DESC) AS rn,
                        SUM(limit_up_count) OVER (PARTITION BY date) AS total_lu
                    FROM dws_sector_strength
                    WHERE source = ? AND date BETWEEN ? AND ?
                )
                SELECT date, industry_name, limit_up_count, total_lu, strength_score
                FROM ranked WHERE rn = 1
                """,
                [source, start, end],
            )
        except Exception as exc:  # noqa: BLE001 - 表不存在或结构不符时降级
            logger.warning("板块强度读取失败，本次不填充 top_sector：%s", str(exc)[:100])
            return {}

        out: dict[date, dict[str, Any]] = {}
        for _, r in df.iterrows():
            total = int(r["total_lu"] or 0)
            lu = int(r["limit_up_count"] or 0)
            # 键必须统一成 datetime.date：DuckDB 的 DATE 列经 pandas 读出来是
            # Timestamp，而调用方用的 key 是 `.dt.date` 得到的 date。
            # 两者不相等会导致字典查找全部落空 —— 表现为 top_sector 恒为 None，
            # 而且不报任何错，非常难查。
            key = pd.to_datetime(r["date"]).date()
            out[key] = {
                "name": str(r["industry_name"]),
                "share": round(lu / total * 100, 1) if total > 0 else None,
            }
        logger.info("板块强度载入：%d 个交易日的 top_sector", len(out))
        return out

    @staticmethod
    def _detail_text(
        recession_reasons: list[str],
        state: str,
        sector_name: str | None,
        sector_share: float | None,
    ) -> str:
        """判定依据的可读说明，落库到 detail 字段供报告与排查使用。"""
        parts = list(recession_reasons)
        if sector_name:
            share = f"，占全市场涨停 {sector_share}%" if sector_share is not None else ""
            parts.append(f"最强板块 {sector_name}{share}")
        if state == "event":
            parts.append("单一板块虹吸，主线明确")
        return "；".join(parts)

    # ---------------- 判定 ----------------
    def _classify(self, row: pd.Series, top_sector: dict[str, Any] | None = None) -> dict[str, Any]:
        c = self.cfg.section("market_regime")
        trend_cfg = c.get("trend", {})
        euph_cfg = c.get("euphoria", {})
        rec_cfg = c.get("recession", {})
        range_cfg = c.get("range", {})
        event_cfg = c.get("event", {})
        # 配置开关：event 触发完全依赖未验证的 dws_sector_strength（见
        # settings.yaml market_regime.event.enabled 的注释链），默认关闭。
        # 关闭时该状态永不触发，这些交易日会落到 trend/range/unknown ——
        # 这是"用不到的状态不如不给"的处置，不是删代码（验证通过可随时打开）。
        event_enabled = bool(event_cfg.get("enabled", True))

        limit_up = to_int(row.get("limit_up_count"))
        limit_down = to_int(row.get("limit_down_count"))
        touch = to_int(row.get("touch_count"))
        prev_limit_up = to_float(row.get("prev_limit_up"))
        breadth20 = to_float(row.get("breadth_ma20"))
        breadth60 = to_float(row.get("breadth_ma60"))
        idx20 = to_float(row.get("index_pct_20d"))

        broken_rate = ((touch - limit_up) / touch * 100) if touch > 0 else 0.0

        # ---- 口径选择（涨停家数与炸板率必须同源）----
        # 两个口径实测平均差 7.5%（242 天中仅 15 天完全一致，最大差 46 家），
        # 足以让阈值附近（如 euphoria 的「涨停≥80」）的状态判定翻转。
        # 默认用自算口径：它能覆盖**整个回测历史**，序列内部一致；
        # 上游口径更权威，但目前只有一年，切过去会造成历史断层，
        # 因此做成配置项，等上游积累满回测区间长度后再切换。
        # 校验工具：python scripts\check_caliber.py
        limit_up_source = str(c.get("limit_up_source", "selfcalc") or "selfcalc")
        used_source = "selfcalc"
        up_lu = row.get("up_limit_up")
        if limit_up_source == "upstream" and up_lu is not None and not pd.isna(up_lu):
            up_lu = int(up_lu)
            if up_lu > 0:
                # 炸板率也换成上游口径，避免「家数用上游、炸板率用自算」的混算
                up_break = to_int(row.get("up_break"))
                total_touch = up_lu + up_break
                limit_up = up_lu
                broken_rate = 100.0 * up_break / total_touch if total_touch > 0 else 0.0
                used_source = "upstream"
        drop_ratio = max(0.0, 1 - limit_up / prev_limit_up) if prev_limit_up > 0 else 0.0

        sector_name = (top_sector or {}).get("name")
        sector_share = (top_sector or {}).get("share")

        # ---- 状态判定（优先级：退潮 > 高潮 > 事件驱动 > 趋势 > 震荡 > 不明）----
        # 事件驱动排在趋势/震荡之前：它描述的是「资金往哪儿去」，
        # 比「大盘涨不涨」更有交易指导意义 —— 哪怕指数横盘，
        # 单一板块虹吸也意味着明确的主线。
        state = "unknown"
        confidence = 0.5

        recession_reasons = []
        if limit_down >= float(rec_cfg.get("min_limit_down", 30)):
            recession_reasons.append(f"跌停 {limit_down} 家")
        if broken_rate >= float(rec_cfg.get("max_broken_rate", 45)):
            recession_reasons.append(f"炸板率 {broken_rate:.1f}%")
        if drop_ratio >= float(rec_cfg.get("limit_up_drop_ratio", 0.4)) and breadth20 < 45:
            recession_reasons.append(f"涨停家数萎缩 {drop_ratio * 100:.0f}%")

        # 事件驱动的两个条件都要满足：涨停家数够多（否则是安静的假信号），
        # 且集中在单一板块。只满足后者不构成事件驱动。
        event_share_th = float(event_cfg.get("sector_limit_up_share", 30.0))
        event_min_lu = to_int(event_cfg.get("min_limit_up", 40))
        is_event = (
            event_enabled
            and sector_name is not None
            and sector_share is not None
            and sector_share >= event_share_th
            and limit_up >= event_min_lu
        )

        if recession_reasons:
            state = "recession"
            confidence = min(0.95, 0.65 + 0.1 * len(recession_reasons))
        elif limit_up >= float(euph_cfg.get("min_limit_up", 80)) and broken_rate <= float(
            euph_cfg.get("max_broken_rate", 20)
        ):
            state = "euphoria"
            confidence = min(0.95, 0.6 + min(limit_up, 150) / 150 * 0.35)
        elif is_event:
            state = "event"
            # 虹吸越集中，置信度越高（30% → 0.65，60% 以上 → 0.95）
            confidence = min(0.95, 0.65 + (sector_share - event_share_th) / 30 * 0.3)
        elif breadth60 >= float(trend_cfg.get("min_breadth_ma60", 55)) and idx20 >= float(
            trend_cfg.get("min_index_pct_20d", 0)
        ):
            state = "trend"
            confidence = min(0.95, 0.5 + (breadth60 - 55) / 100 * 1.5)
        elif float(range_cfg.get("breadth_min", 35)) <= breadth20 <= float(
            range_cfg.get("breadth_max", 55)
        ):
            state = "range"
            confidence = 0.6

        # 2026-10-08：三档权重（short/swing/value）随主链路配额制一并移除。
        # 市场状态现在**只用于展示**（市场宽度/涨停家数/炸板率），不再影响任何选股。
        return {
            "date": row["date"],
            "up_count": to_int(row.get("up_count")),
            "down_count": to_int(row.get("down_count")),
            "limit_up_count": limit_up,
            "limit_down_count": limit_down,
            "broken_rate": round(broken_rate, 2),
            "total_amount": to_float(row.get("total_amount")),
            "amount_ratio": None if pd.isna(row.get("amount_ratio")) else to_float(row.get("amount_ratio")),
            "breadth_ma20": round(breadth20, 2),
            "breadth_ma60": round(breadth60, 2),
            "index_code": "sh.000001",
            "index_pct_5d": None if pd.isna(row.get("index_pct_5d")) else to_float(row.get("index_pct_5d")),
            "index_pct_20d": idx20,
            "limit_up_source": used_source,
            "top_sector": sector_name,
            "top_sector_share": sector_share,
            "state": state,
            "state_label": STATE_LABEL.get(state, state),
            "confidence": round(confidence, 3),
            "detail": self._detail_text(recession_reasons, state, sector_name, sector_share),
        }

    # ---------------- 查询 ----------------
    def latest(self) -> dict[str, Any] | None:
        df = self.storage.query_df(
            "SELECT * FROM dws_market_regime ORDER BY date DESC LIMIT 1"
        )
        if df.empty:
            return None
        return df.iloc[0].to_dict()

    def history(self, days: int = 120) -> pd.DataFrame:
        return self.storage.query_df(
            "SELECT * FROM dws_market_regime ORDER BY date DESC LIMIT ?", [days]
        ).sort_values("date")

    def get(self, target: date) -> dict[str, Any] | None:
        """取指定交易日的市场状态。"""
        df = self.storage.query_df("SELECT * FROM dws_market_regime WHERE date = ?", [target])
        return None if df.empty else df.iloc[0].to_dict()

    @staticmethod
    def now() -> datetime:
        return datetime.now()
