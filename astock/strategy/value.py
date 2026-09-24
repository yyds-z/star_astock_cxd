# -*- coding: utf-8 -*-
"""价值档策略（1 月~1 年）。

Phase 1 用「长期均线多头趋势 + 回踩不破」作为趋势代理。
Phase 2 接入 adata 的财务数据后，将补充 ROE / 营收利润增速 / 估值分位等
真正的基本面因子（参考 UZI-Skill 的多维分析框架）。
"""

from __future__ import annotations

import pandas as pd

from astock.strategy.base import BaseStrategy, num, pct, scale


class MaMultiTrendStrategy(BaseStrategy):
    """长期均线多头排列 + 回踩不破（价值档趋势代理）。

    条件：MA20 > MA60 > MA120（长期多头）+ 价格未有效跌破 MA20。
    """

    name = "ma_multi_trend"
    label = "长期均线多头"
    tier = "value"
    require_col = "ma120"

    def evaluate(self, df: pd.DataFrame) -> pd.DataFrame:
        max_pullback = float(self.param("max_pullback", 0.92))

        bullish = (df["ma20"] > df["ma60"]) & (df["ma60"] > df["ma120"])
        hold = df["close"] >= df["ma20"] * max_pullback
        mask = bullish & hold

        spread = (df["ma20"] / df["ma120"] - 1) * 100
        score = (
            50
            + 22 * scale(spread, 0, 25)
            + 18 * scale(df["rps120"], 50, 95)
            + 10 * scale(df["pct_60d"], 0, 30)
        )
        reasons = [
            [
                "长期均线多头（MA20>MA60>MA120）",
                f"MA20 高于 MA120 {pct(spread.iloc[i])}",
                f"60日涨幅 {pct(df['pct_60d'].iloc[i])}，RPS120 {num(df['rps120'].iloc[i], 1)}",
            ]
            for i in range(len(df))
        ]
        return self.build_result(df, mask, score, reasons)


VALUE_STRATEGIES = [
    MaMultiTrendStrategy,
]
