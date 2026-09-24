# -*- coding: utf-8 -*-
"""指标工具函数：涨跌幅限制、分位数标准化、安全除法等策略层公共依赖。"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

# 各板块涨跌幅限制
LIMIT_RATIO = {"main": 0.10, "gem": 0.20, "star": 0.20, "bj": 0.30}
ST_LIMIT_RATIO = 0.05


def limit_ratio(board: str | None, is_st: bool = False) -> float:
    """返回涨跌幅限制比例。ST 优先。"""
    if is_st:
        return ST_LIMIT_RATIO
    return LIMIT_RATIO.get(str(board or "main"), 0.10)


def safe_div(numerator: pd.Series, denominator: pd.Series, default: float = 0.0) -> pd.Series:
    """安全除法，分母为 0 或空时返回默认值。"""
    num = pd.to_numeric(numerator, errors="coerce")
    den = pd.to_numeric(denominator, errors="coerce")
    out = num / den.replace(0, np.nan)
    return out.fillna(default)


def percentile_score(series: pd.Series, ascending: bool = True) -> pd.Series:
    """把数值列转换为 0~100 的分位数得分（横截面标准化）。

    ascending=True 表示数值越大得分越高。
    """
    s = pd.to_numeric(series, errors="coerce")
    if s.notna().sum() <= 1:
        return pd.Series(50.0, index=series.index)
    return s.rank(pct=True, ascending=ascending).fillna(0.5) * 100


def clip_score(series: pd.Series, low: float = 0.0, high: float = 100.0) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").fillna(0.0).clip(low, high)


def ma(series: pd.Series, window: int) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").rolling(window, min_periods=max(2, window // 2)).mean()


# ---------------- 类型安全取值（DuckDB/pandas 会产生 None / NaN / pd.NA 三种空值）----------------


def to_float(value: Any, default: float = 0.0) -> float:
    """把任意值安全转成 float。

    必须处理 pd.NA / NaN / None 三种空值：直接写 `value or 0` 会因为
    `bool(pd.NA)` 抛 TypeError，且 `bool(nan)` 为 True 导致 `nan or 0` 返回 nan。
    """
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def to_int(value: Any, default: int = 0) -> int:
    return int(to_float(value, float(default)))


def to_bool(value: Any, default: bool = False) -> bool:
    """把任意值安全转成 bool，空值返回默认值。"""
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        return default
    try:
        return bool(value)
    except (TypeError, ValueError):
        return default


def to_list(value: Any) -> list:
    """把可能为空的列值安全转成 list。"""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    try:
        if pd.isna(value):
            return []
    except (TypeError, ValueError):
        pass
    return []


def to_str(value: Any, default: str = "") -> str:
    """把任意值安全转成字符串，空值返回默认值（避免出现 'nan' / 'None' 字面量）。"""
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        pass
    text = str(value)
    return default if text in ("nan", "NaT", "None") else text
