# -*- coding: utf-8 -*-
"""波段档策略（3~20 日）。

其中 5 个策略的判定条件直接移植自 Sequoia-X（已实盘验证过的条件组合），
但做了两处增强：
1. 评估方式由「逐股 Python 循环」改为「全市场向量化」，速度提升一个数量级；
2. 输出带 0~100 强度分与推荐理由，而非裸代码列表。
"""

from __future__ import annotations

import pandas as pd

from astock.strategy.base import BaseStrategy, num, pct, scale, yi


class TurtleTradeStrategy(BaseStrategy):
    """海龟突破（移植 Sequoia-X TurtleTradeStrategy）。

    条件：20 日新高突破 + 成交额过亿 + 实体阳线防诱多（真涨）。
    """

    name = "turtle_trade"
    label = "海龟突破"
    tier = "swing"
    require_col = "hh20_prev"

    def evaluate(self, df: pd.DataFrame) -> pd.DataFrame:
        window_amount = float(self.param("min_amount", 100_000_000))
        require_yang = bool(self.param("require_yang", True))

        breakout = df["close"] > df["hh20_prev"]
        liquid = df["amount"] > window_amount
        is_up = df["close"] > df["prev_close"]
        mask = breakout & liquid & is_up
        if require_yang:
            mask &= df["is_yang"].fillna(False)

        breakout_pct = (df["close"] / df["hh20_prev"] - 1) * 100
        score = (
            55
            + 20 * scale(breakout_pct, 0, 5)
            + 15 * scale(df["amount"], window_amount, window_amount * 5)
            + 10 * df["is_yang"].fillna(False).astype(float)
        )
        reasons = [
            [
                f"突破20日新高 {pct(breakout_pct.iloc[i])}",
                f"成交额 {yi(df['amount'].iloc[i])}",
                "实体阳线",
            ]
            for i in range(len(df))
        ]
        return self.build_result(df, mask, score, reasons)


class MaVolumeStrategy(BaseStrategy):
    """均线放量金叉（移植 Sequoia-X MaVolumeStrategy）。

    条件：MA5 上穿 MA20（金叉）+ 当日成交量 > 20 日均量的 1.5 倍。
    """

    name = "ma_volume"
    label = "均线放量金叉"
    tier = "swing"
    require_col = "prev_ma20"

    def evaluate(self, df: pd.DataFrame) -> pd.DataFrame:
        vol_ratio_th = float(self.param("vol_ratio", 1.5))

        golden = (df["prev_ma5"] < df["prev_ma20"]) & (df["ma5"] > df["ma20"])
        surge = df["vol_ratio"] > vol_ratio_th
        mask = golden & surge & (df["ma20"] > 0)

        score = (
            50
            + 22 * scale(df["vol_ratio"], vol_ratio_th, vol_ratio_th * 2)
            + 18 * scale((df["ma5"] / df["ma20"] - 1) * 100, 0, 3)
            + 10 * scale(df["pct_chg"], 0, 6)
        )
        reasons = [
            [
                "MA5 上穿 MA20 金叉",
                f"量比 {num(df['vol_ratio'].iloc[i])}",
                f"当日涨幅 {pct(df['pct_chg'].iloc[i])}",
            ]
            for i in range(len(df))
        ]
        return self.build_result(df, mask, score, reasons)


class RpsBreakoutStrategy(BaseStrategy):
    """RPS 相对强度突破（移植 Sequoia-X RpsBreakoutStrategy）。

    条件：120 日 RPS ≥ 90（全市场前 10% 强度）+ 价格接近 120 日高点。
    """

    name = "rps_breakout"
    label = "RPS相对强度突破"
    tier = "swing"
    require_col = "hh120"

    def evaluate(self, df: pd.DataFrame) -> pd.DataFrame:
        rps_th = float(self.param("rps_threshold", 90))
        near = float(self.param("near_high_ratio", 0.90))

        mask = (df["rps120"] >= rps_th) & (df["close"] >= df["hh120"] * near)

        near_ratio = df["close"] / df["hh120"]
        score = (
            50
            + 25 * scale(df["rps120"], rps_th, 99)
            + 25 * scale(near_ratio, near, 1.0)
        )
        reasons = [
            [
                f"RPS120 = {num(df['rps120'].iloc[i], 1)}（全市场前列）",
                f"距120日高点 {pct((near_ratio.iloc[i] - 1) * 100)}",
                f"20日涨幅 {pct(df['pct_20d'].iloc[i])}",
            ]
            for i in range(len(df))
        ]
        return self.build_result(df, mask, score, reasons)


class HighTightFlagStrategy(BaseStrategy):
    """高而窄的旗形整理（移植 Sequoia-X HighTightFlagStrategy）。

    条件：40 日强动量（涨幅>60%）+ 近 10 日极度收敛（振幅<15%）+ 高位抗跌 + 缩量。
    """

    name = "high_tight_flag"
    label = "高窄旗形整理"
    tier = "swing"
    require_col = "ll40"

    def evaluate(self, df: pd.DataFrame) -> pd.DataFrame:
        momentum_th = float(self.param("momentum_ratio", 1.6))
        tight_th = float(self.param("tight_ratio", 1.15))
        shrink_th = float(self.param("shrink_ratio", 0.6))

        valid = (df["ll40"] > 0) & (df["ll10"] > 0)
        momentum = df["hh40"] / df["ll40"]
        tight = df["hh10"] / df["ll10"]
        high_level = df["ll10"] >= df["hh40"] * 0.8
        shrink = df["volume"] < df["vol_ma20_prev"] * shrink_th

        mask = valid & (momentum > momentum_th) & (tight < tight_th) & high_level & shrink

        score = (
            50
            + 22 * scale(momentum, momentum_th, momentum_th * 1.6)
            + 18 * (1 - scale(tight, 1.0, tight_th))
            + 10 * (1 - scale(df["vol_ratio"], 0, shrink_th))
        )
        reasons = [
            [
                f"40日涨幅 {pct((momentum.iloc[i] - 1) * 100, 0)}",
                f"近10日振幅仅 {pct((tight.iloc[i] - 1) * 100)}",
                "缩量整理待突破",
            ]
            for i in range(len(df))
        ]
        return self.build_result(df, mask, score, reasons)


class UptrendLimitDownStrategy(BaseStrategy):
    """上升趋势中的放量跌停错杀（移植 Sequoia-X UptrendLimitDownStrategy）。

    条件：中期上升趋势（MA20 > MA60）+ 今日放量跌停。
    """

    name = "uptrend_limit_down"
    label = "上升趋势跌停反包"
    tier = "swing"
    require_col = "prev_ma60"

    def evaluate(self, df: pd.DataFrame) -> pd.DataFrame:
        ld_ratio = float(self.param("limit_down_ratio", 0.905))
        vol_ratio_th = float(self.param("vol_ratio", 2.0))

        uptrend = df["prev_ma20"] > df["prev_ma60"]
        limit_down = df["close"] <= df["prev_close"] * ld_ratio
        surge = df["vol_ratio"] > vol_ratio_th
        mask = uptrend & limit_down & surge

        score = (
            50
            + 22 * scale(df["vol_ratio"], vol_ratio_th, vol_ratio_th * 2.5)
            + 18 * scale((df["prev_ma20"] / df["prev_ma60"] - 1) * 100, 0, 8)
            + 10 * scale(-df["pct_chg"], 9, 10)
        )
        reasons = [
            [
                "中期均线多头（MA20>MA60）",
                f"放量跌停 {pct(df['pct_chg'].iloc[i])}",
                f"量比 {num(df['vol_ratio'].iloc[i])}，疑似错杀",
            ]
            for i in range(len(df))
        ]
        return self.build_result(df, mask, score, reasons)


SWING_STRATEGIES = [
    TurtleTradeStrategy,
    MaVolumeStrategy,
    RpsBreakoutStrategy,
    HighTightFlagStrategy,
    UptrendLimitDownStrategy,
]
