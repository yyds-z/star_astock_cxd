# -*- coding: utf-8 -*-
"""短线档策略（1~3 日）。

由于运行环境不保证开机、免费源分钟数据不稳定，短线档定位为
「盘后生成次日观察池」，不做盘中实时信号。
"""

from __future__ import annotations

import pandas as pd

from astock.features.indicators import to_int
from astock.strategy.base import BaseStrategy, num, pct, scale


class LimitUpShakeoutStrategy(BaseStrategy):
    """涨停洗盘回踩确认（移植 Sequoia-X LimitUpShakeoutStrategy）。

    条件：昨日涨停 + 今日放量收阴 + 不破昨收（洗盘而非出货）。
    """

    name = "limit_up_shakeout"
    label = "涨停洗盘回踩"
    tier = "short"
    require_col = "prev2_close"

    def evaluate(self, df: pd.DataFrame) -> pd.DataFrame:
        limit_ratio = float(self.param("limit_ratio", 1.095))
        vol_ratio_th = float(self.param("vol_ratio", 2.0))

        limit_up_yesterday = df["prev_close"] >= df["prev2_close"] * limit_ratio
        bearish_today = df["close"] < df["open"]
        volume_surge = df["volume"] > df["prev_volume"] * vol_ratio_th
        support_hold = df["low"] >= df["prev_close"]
        mask = limit_up_yesterday & bearish_today & volume_surge & support_hold

        vol_multiple = df["volume"] / df["prev_volume"].replace(0, pd.NA)
        # 收盘越靠近昨收（洗盘越浅）得分越高
        hold_strength = (df["close"] / df["prev_close"] - 1) * 100
        score = (
            55
            + 20 * scale(vol_multiple, vol_ratio_th, vol_ratio_th * 2.5)
            + 15 * (1 - scale(-hold_strength, 0, 5))
            + 10 * scale(df["turn"], 5, 25)
        )
        reasons = [
            [
                "昨日涨停，今日洗盘回踩",
                # 必须用 num：vol_multiple 在 prev_volume 为 0/空时是 pd.NA，
                # 直接 float(pd.NA) 会抛 TypeError，把整个策略打断
                f"放量 {num(vol_multiple.iloc[i], 1)} 倍",
                f"收盘守住昨收（{pct(hold_strength.iloc[i])}）",
            ]
            for i in range(len(df))
        ]
        return self.build_result(df, mask, score, reasons)


class NewStockBurstStrategy(BaseStrategy):
    """次新股异动（新增，呼应「保留次新用于情绪博弈」的需求）。

    条件：上市未满 N 日 + 高换手 + 显著放量 + 当日强势上涨。
    由于次新股历史 K 线不足，均线类指标不参与判定，只做数据降级而非剔除。
    """

    name = "new_stock_burst"
    label = "次新股异动"
    tier = "short"
    require_col = "vol_ma20"

    def evaluate(self, df: pd.DataFrame) -> pd.DataFrame:
        max_days = int(self.param("max_listed_days", 120))
        min_turn = float(self.param("min_turn", 15.0))
        vol_ratio_th = float(self.param("vol_ratio", 2.0))
        min_pct = float(self.param("min_pct_chg", 3.0))

        is_new = df["is_new_stock"].fillna(False).astype(bool)
        listed = pd.to_numeric(df["listed_days"], errors="coerce")
        mask = (
            is_new
            & (listed <= max_days)
            & (df["turn"] >= min_turn)
            & (df["vol_ratio"] > vol_ratio_th)
            & (df["pct_chg"] >= min_pct)
        )

        score = (
            50
            + 20 * scale(df["turn"], min_turn, 40)
            + 18 * scale(df["vol_ratio"], vol_ratio_th, vol_ratio_th * 3)
            + 12 * scale(df["pct_chg"], min_pct, 9)
        )
        reasons = [
            [
                f"上市 {to_int(listed.iloc[i])} 日的次新股" if pd.notna(listed.iloc[i]) else "次新股",
                f"换手率 {pct(df['turn'].iloc[i])}",
                f"放量 {num(df['vol_ratio'].iloc[i], 1)} 倍，涨幅 {pct(df['pct_chg'].iloc[i])}",
            ]
            for i in range(len(df))
        ]
        return self.build_result(df, mask, score, reasons)


SHORT_STRATEGIES = [
    LimitUpShakeoutStrategy,
    NewStockBurstStrategy,
]
