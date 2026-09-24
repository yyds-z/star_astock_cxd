# -*- coding: utf-8 -*-
"""价值档的基本面因子（数据源：`dws_finance_metrics`）。

------------------------------------------------------------------
解决什么
------------------------------------------------------------------
价值档原先只有 `ma_multi_trend` 一个策略，且是**纯技术面趋势代理**
（ma20 > ma60 > ma120），不含任何基本面 —— 实测它在所有市场状态下都是最弱档。
本模块把已采集的财务指标接入价值档评分。

------------------------------------------------------------------
两个必须守住的约束
------------------------------------------------------------------
1) **防前视**：财报有披露滞后，回测到 date 时只能用**已公开**的财报。
   `dws_finance_metrics` 里存了 `report_date`（实际披露日），直接按它过滤即可，
   与 `report_builder._load_fundamentals` 口径一致。
   ⚠️ 不要用 `period_end` 过滤，也不要用「期末 + N 天」估算：
   报告期末只是会计区间终点，财报实际公开晚 1~4 个月；估算值对年报偏差可达一个月，
   且对不同报告期系统性出错。按 period_end 取数等于用了当时还不存在的信息。

2) **不用百分位，改用绝对阈值**：百分位依赖**完整横截面**，而财务是分批采集的
   （全市场回填约 17 小时，见 `finance --all`）。若只采了 300 只就用百分位，
   这 300 只的排名与全市场不可比，会系统性扭曲分数 —— 采得越少越失真。
   绝对阈值在任何覆盖度下都可比，代价是粗糙（不随市场整体盈利水平自适应）。

3) **同比只用 `*_yoy` 列**：`roe` / `revenue` 这类流量指标是累计口径
   （Q1=3个月、Q4=12个月），跨报告期不可比。同比列已在**同 fiscal_period 内**
   计算过，可直接使用；`debt_ratio` 是时点指标，也可直接比。
"""

from __future__ import annotations

import pandas as pd

from astock.logger import get_logger

logger = get_logger("features.fundamentals")

# 绝对阈值映射：(取值, 得分)，线性插值，越界截断。
# 分档点依据：A 股非金融类公司的常见分布（ROE 中位数约 6~8%，优秀 >15%）。
ROE_BANDS: list[tuple[float, float]] = [(0.0, 30.0), (8.0, 60.0), (15.0, 85.0), (25.0, 100.0)]
PROFIT_YOY_BANDS: list[tuple[float, float]] = [
    (-30.0, 20.0), (0.0, 50.0), (20.0, 75.0), (60.0, 100.0)
]
REVENUE_YOY_BANDS: list[tuple[float, float]] = [
    (-20.0, 20.0), (0.0, 50.0), (15.0, 75.0), (40.0, 100.0)
]
# 资产负债率**越低越好**，故 y 值随 x 递减
DEBT_RATIO_BANDS: list[tuple[float, float]] = [
    (20.0, 100.0), (45.0, 75.0), (65.0, 45.0), (85.0, 10.0)
]

WEIGHTS: dict[str, float] = {
    "roe": 0.35,          # 盈利能力（核心）
    "profit_yoy": 0.25,   # 成长性（利润）
    "revenue_yoy": 0.20,  # 成长性（收入）
    "debt_ratio": 0.20,   # 财务安全
}


def _interp(value, bands: list[tuple[float, float]]) -> float | None:
    """在分档点上线性插值；不可用时返回 None。`bands` 需按 x 升序。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(v):
        return None
    if v <= bands[0][0]:
        return bands[0][1]
    if v >= bands[-1][0]:
        return bands[-1][1]
    for (x0, y0), (x1, y1) in zip(bands, bands[1:]):
        if x0 <= v <= x1:
            return y1 if x1 == x0 else y0 + (y1 - y0) * (v - x0) / (x1 - x0)
    return None


def fundamental_score(row) -> float | None:
    """加权求基本面分（0~100）；一个字段都不可用时返回 None。

    只按**实际可得**字段归一化：缺项不等于 0 分，
    否则「数据没采到」会被误判成「基本面差」。
    """
    total, weight_sum = 0.0, 0.0
    for key, bands in (
        ("roe", ROE_BANDS),
        ("profit_yoy", PROFIT_YOY_BANDS),
        ("revenue_yoy", REVENUE_YOY_BANDS),
        ("debt_ratio", DEBT_RATIO_BANDS),
    ):
        s = _interp(row.get(key) if hasattr(row, "get") else None, bands)
        if s is None:
            continue
        total += s * WEIGHTS[key]
        weight_sum += WEIGHTS[key]
    return round(total / weight_sum, 2) if weight_sum else None


def load_asof(storage, as_of, codes: list[str]) -> pd.DataFrame:
    """取截至 `as_of` **已披露**的每只股票最新一期财务指标（防前视）。

    用 `report_date`（**实际披露日**）过滤，与 `report_builder._load_fundamentals`
    保持同一口径。表里就带着披露日，所以不需要「期末 + N 天」这类估算——
    那种估算对年报偏差可达一个月，而且会对不同报告期系统性出错。

    排序用 `report_date DESC, period_end DESC`：上游存在披露日错填的情况
    （实测 2025 中报的 report_date 被写成与 2026 中报同值），
    只按 report_date 排序会退化成「任意取一条」，可能取到一年前的旧数据。
    """
    codes = [c for c in dict.fromkeys(codes) if c]
    if not codes or as_of is None:
        return pd.DataFrame()
    placeholders = ", ".join("?" for _ in codes)
    return storage.query_df(
        f"""
        SELECT code, period_end, report_date, fiscal_period,
               roe, profit_yoy, revenue_yoy, debt_ratio, eps
        FROM (
            SELECT *, ROW_NUMBER() OVER (
                       PARTITION BY code ORDER BY report_date DESC, period_end DESC
                   ) AS rn
            FROM dws_finance_metrics
            WHERE report_date IS NOT NULL AND report_date <= ? AND code IN ({placeholders})
        ) t
        WHERE rn = 1
        """,
        [pd.to_datetime(as_of).date(), *codes],
    )


def score_map(storage, as_of, codes: list[str]) -> dict[str, float]:
    """{code: 基本面分}。无数据时返回空字典（调用方应回退为纯技术面）。"""
    df = load_asof(storage, as_of, codes)
    if df is None or df.empty:
        return {}
    out: dict[str, float] = {}
    for _, r in df.iterrows():
        s = fundamental_score(r)
        if s is not None:
            out[str(r["code"])] = s
    return out
