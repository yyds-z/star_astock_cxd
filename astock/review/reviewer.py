# -*- coding: utf-8 -*-
"""自动复盘（模块 7）。

复盘口径（已按「人工交易 + 盘后选股」的现实场景确定）：
- 推荐在 data_date 盘后生成，计划在 plan_date（次一交易日）**开盘买入**
- 当日收益 = plan_date 收盘 / plan_date 开盘 − 1
- 最大涨幅 / 最大回撤以开盘买入价为基准
- 持有 3 日收益 = 计划买入后第 3 个交易日收盘 / 开盘买入价 − 1

结果分为 win / loss / flat / pending，供策略迭代统计胜率使用。
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from astock.logger import get_logger
from astock.storage.db import Storage, get_storage

logger = get_logger("review.reviewer")

FLAT_THRESHOLD = 0.3  # 当日收益绝对值小于该值视为持平


class Reviewer:
    """推荐结果复盘器。"""

    def __init__(self, storage: Storage | None = None) -> None:
        self.storage = storage or get_storage()

    # ---------------- 主流程 ----------------
    def review(self, limit_records: int = 500) -> dict:
        """回填尚未复盘的推荐记录。可重复执行（幂等）。"""
        pending = self.storage.query_df(
            """
            SELECT r.rec_id, r.code, r.tier, r.strategy, r.rec_date, r.trade_date
            FROM ads_recommend r
            LEFT JOIN ads_review v ON v.rec_id = r.rec_id
            WHERE v.rec_id IS NULL
            ORDER BY r.rec_date DESC
            LIMIT ?
            """,
            [limit_records],
        )
        if pending.empty:
            logger.info("没有待复盘的推荐记录")
            return {"pending": 0, "reviewed": 0, "waiting": 0}

        latest_bar = self.storage.latest_trade_date()
        rows: list[dict] = []
        waiting = 0
        invalid = 0

        for _, rec in pending.iterrows():
            plan_date = rec["trade_date"]
            if hasattr(plan_date, "date"):
                plan_date = plan_date.date()
            rec_date = rec["rec_date"]
            if hasattr(rec_date, "date"):
                rec_date = rec_date.date()

            # 数据完整性守卫：计划买入日不可能等于或早于推荐日。
            # 实测踩过：交易日历缺未来日期时，旧的落库逻辑把 trade_date 兜底成
            # 「数据日」，于是推荐日=买入日，这里又用同一天的 K 线结算
            # —— 等于用当天收盘信息回测当天开盘买入，会系统性高估策略。
            # 这类记录永远不该被结算，宁可跳过并告警。
            if plan_date is not None and rec_date is not None and plan_date <= rec_date:
                invalid += 1
                logger.warning(
                    "[%s] 跳过无效复盘：计划买入日 %s 不晚于推荐日 %s"
                    "（交易日历可能缺少未来日期）",
                    rec["code"], plan_date, rec_date,
                )
                continue

            if latest_bar is None or plan_date is None or plan_date > latest_bar:
                waiting += 1  # 计划交易日还没到，等下次复盘
                continue

            bar = self._bar_on(rec["code"], plan_date)
            if bar is None:
                waiting += 1
                continue

            buy_price = float(bar["open"] or 0)
            if buy_price <= 0:
                waiting += 1
                continue

            close = float(bar["close"])
            high = float(bar["high"])
            low = float(bar["low"])
            preclose = float(bar["preclose"] or 0)

            day_return = (close / buy_price - 1) * 100
            hold3 = self._hold_return(rec["code"], plan_date, buy_price)

            if day_return > FLAT_THRESHOLD:
                result = "win"
            elif day_return < -FLAT_THRESHOLD:
                result = "loss"
            else:
                result = "flat"

            rows.append(
                {
                    "rec_id": rec["rec_id"],
                    "rec_date": rec["rec_date"],
                    "code": rec["code"],
                    "tier": rec["tier"],
                    "strategy": rec["strategy"],
                    "next_date": plan_date,
                    "next_open": buy_price,
                    "next_close": close,
                    "next_pct_chg": round((close / preclose - 1) * 100, 2) if preclose else None,
                    "next_high_pct": round((high / buy_price - 1) * 100, 2),
                    "next_low_pct": round((low / buy_price - 1) * 100, 2),
                    "hold3_pct": round(hold3, 2) if hold3 is not None else None,
                    "result": result,
                    "updated_at": datetime.now(),
                }
            )

        if rows:
            self.storage.upsert_df(pd.DataFrame(rows), "ads_review")

        stats = {
            "pending": len(pending),
            "reviewed": len(rows),
            "waiting": waiting,
            "invalid": invalid,
        }
        logger.info(
            "复盘完成：处理 %d 条，成功 %d 条，等待 %d 条，跳过无效 %d 条",
            stats["pending"], stats["reviewed"], stats["waiting"], stats["invalid"],
        )
        return stats

    # ---------------- 明细查询 ----------------
    def _bar_on(self, code: str, day: date) -> pd.Series | None:
        df = self.storage.query_df(
            "SELECT * FROM dwd_daily_bar WHERE code = ? AND date = ?", [code, day]
        )
        return None if df.empty else df.iloc[0]

    def _hold_return(self, code: str, plan_date: date, buy_price: float) -> float | None:
        """持有 3 个交易日（含买入日）的收益。"""
        df = self.storage.query_df(
            """
            SELECT close FROM (
                SELECT close, ROW_NUMBER() OVER (ORDER BY date) AS rn
                FROM dwd_daily_bar WHERE code = ? AND date >= ?
            ) t ORDER BY rn LIMIT 3
            """,
            [code, plan_date],
        )
        if df.empty or len(df) < 3:
            return None
        return (float(df["close"].iloc[-1]) / buy_price - 1) * 100

    # ---------------- 统计 ----------------
    def summary(self, days: int = 60) -> dict:
        """复盘统计：胜率、平均收益等，用于策略迭代。"""
        since = date.today() - pd.Timedelta(days=days)
        df = self.storage.query_df(
            """
            SELECT v.*, r.strategy FROM ads_review v
            JOIN ads_recommend r ON r.rec_id = v.rec_id
            WHERE v.next_date >= ?
            """,
            [since.date() if hasattr(since, "date") else since],
        )
        return self.summary_from_frame(df)

    @staticmethod
    def summary_from_frame(df: pd.DataFrame) -> dict:
        """基于给定的复盘明细 DataFrame 计算统计（供 API 直接使用快照数据）。"""
        if df is None or df.empty:
            return {"count": 0}

        total = len(df)
        wins = int((df["result"] == "win").sum())
        losses = int((df["result"] == "loss").sum())
        flats = int((df["result"] == "flat").sum())

        def _avg(series: pd.Series) -> float | None:
            s = pd.to_numeric(series, errors="coerce").dropna()
            return round(float(s.mean()), 2) if len(s) else None

        by_tier = {}
        for tier, g in df.groupby("tier"):
            by_tier[str(tier)] = {
                "count": len(g),
                "win_rate": round(float((g["result"] == "win").mean() * 100), 1),
                "avg_return": _avg(g["next_pct_chg"]),
            }

        by_strategy = {}
        for strat, g in df.groupby("strategy"):
            by_strategy[str(strat)] = {
                "count": len(g),
                "win_rate": round(float((g["result"] == "win").mean() * 100), 1),
                "avg_return": _avg(g["next_pct_chg"]),
            }

        return {
            "count": total,
            "wins": wins,
            "losses": losses,
            "flats": flats,
            "win_rate": round(wins / total * 100, 1) if total else None,
            "avg_return": _avg(df["next_pct_chg"]),
            "avg_max_gain": _avg(df["next_high_pct"]),
            "avg_max_drawdown": _avg(df["next_low_pct"]),
            "avg_hold3": _avg(df["hold3_pct"]),
            "by_tier": by_tier,
            "by_strategy": by_strategy,
        }

    def review_history(self, days: int = 60) -> pd.DataFrame:
        since = date.today() - pd.Timedelta(days=days)
        return self.storage.query_df(
            "SELECT * FROM ads_review WHERE next_date >= ? ORDER BY next_date DESC",
            [since.date() if hasattr(since, "date") else since],
        )
